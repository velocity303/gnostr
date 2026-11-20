"""Read-side Blossom resolver — NIPW-P1 'Unbreakable Media' (NIP-B7 / BUD-03).

A media URL whose last path segment is a 64-hex sha256 is content-addressed:
even when the original host dies, the exact blob can be re-fetched from the
media author's own Blossom servers (kind-10063 list, NIP-B7) via
BUD-03 ``GET <server>/<sha256>``. This module performs that recovery:

  * ``content_hash(url)`` — pure detection: last path segment is 64-hex
    (bare, or with a short file extension such as ``<sha>.png``).
  * ``resolve(url, author_pk)`` / ``resolve_blob(url, author_pk)`` — fetch
    the author's kind-10063 server list (relay REQ injected by main.py via
    ``configure``), try each server in listed order, and return the
    recovered URL only after the downloaded blob's sha256 matches the hash
    embedded in the original URL. On mismatch or all-servers-fail the
    caller keeps the original URL and the existing broken-media UX —
    wrong bytes are NEVER displayed.

Threading contract: ``resolve``/``resolve_blob`` block (relay REQ wait +
HTTP GET) and must run on a worker thread only — callers are
``ImageLoader._worker_fetch`` and ``VideoPlayer._executor`` jobs, never
the GLib main loop. The per-author server cache is TTL'd (1 h) with
in-flight dedup so a burst of dead media from one author costs a single
relay REQ.

Egress reuses the shared media proxy path (``proxy_opener.proxy_opener``,
VID-PROXY-MEDIA): with the SOCKS5/tor proxy on, Blossom fetches route
through it exactly like every other media fetch.

GSettings ``media-resilience`` (bool, default true) gates the whole
feature — read-side only, this module never publishes or writes events.
"""

import hashlib
import json
import re
import threading
import time
import urllib.request
from urllib.parse import urlparse

from . import proxy_opener

# --- tunables -------------------------------------------------------------
SERVER_TTL_S = 3600.0     # per-author kind-10063 cache lifetime
RELAY_WAIT_S = 6.0        # max block waiting for the 10063 answer
BLOB_TIMEOUT_S = 15.0     # per Blossom GET
MAX_BLOB_BYTES = 64 * 1024 * 1024  # refuse absurd blobs (memory bound)

# Last path segment: bare 64-hex, or 64-hex with a short extension
# (hosts like cdn.nostr.build name files '<sha256>.jpg').
_HEX_SEG = re.compile(r"^([0-9a-f]{64})(?:\.[a-z0-9]{1,5})?$")

# --- module state (all guarded by _lock) -----------------------------------
_lock = threading.Lock()
_servers = {}    # author_pk -> (list[str], expires_monotonic)
_inflight = {}   # author_pk -> threading.Event (one relay REQ per author)
_request_fn = None  # injected by main.py: fn(pubkey) -> fire kind-10063 REQ


def configure(request_fn):
    """Inject the relay-REQ trigger: called as request_fn(pubkey) to ask
    the client for the author's kind-10063 list. The answer arrives via
    on_blossom_event (wired from the client's blossom-servers-updated
    signal). Without configuration the resolver simply finds no servers
    and every resolve() is a no-op fallback."""
    global _request_fn
    _request_fn = request_fn


def reset_state():
    """Drop all caches — test isolation hook only."""
    with _lock:
        _servers.clear()
        _inflight.clear()


# --- pure helpers (unit-testable, no I/O) -----------------------------------

def content_hash(url):
    """Return the lowercase 64-hex sha256 embedded in the URL's last path
    segment, or None. Accepts bare '<hex>' and '<hex>.<ext>' segments;
    query strings and fragments are ignored."""
    try:
        if not isinstance(url, str) or not url:
            return None
        path = urlparse(url).path
        seg = (path or "").rsplit("/", 1)[-1].lower()
        m = _HEX_SEG.match(seg)
        return m.group(1) if m else None
    except Exception:
        return None


def parse_blossom_servers(event):
    """NIP-B7 kind-10063 -> ordered list of server base URLs (r-tags).
    Non-http(s) entries and malformed tags are dropped; order preserved,
    duplicates removed, trailing slashes stripped."""
    servers = []
    try:
        if not isinstance(event, dict) or event.get("kind") != 10063:
            return []
        for tag in event.get("tags") or []:
            if not isinstance(tag, list) or len(tag) < 2 or tag[0] != "r":
                continue
            url = (tag[1] or "").strip().rstrip("/")
            if url.startswith("http://") or url.startswith("https://"):
                if url not in servers:
                    servers.append(url)
    except Exception:
        return []
    return servers


def enabled():
    """GSettings media-resilience (default ON). Hard-fallback to True when
    GSettings is unavailable, mirroring uploader.settings_get's contract."""
    return _settings_get("media-resilience", True)


def _settings_get(key, fallback):
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio

        return bool(Gio.Settings.new("tech.livingonlinux.gnostr").get_boolean(key))
    except Exception:
        return fallback


# --- relay round-trip (worker thread only) -----------------------------------

def on_blossom_event(_client, payload):
    """Client signal handler for 'blossom-servers-updated' (kind-10063).
    Caches the author's server list and wakes any worker waiting on it.
    Accepts the event as a JSON string (signal payload) or dict.

    GObject signal convention: PyGObject passes the EMITTING INSTANCE as
    the first handler argument — main.py connects this function directly
    to the client signal, so the signature MUST keep the leading _client
    slot (NIPW-P1-FIX Bug A: a (payload)-only signature raised TypeError
    on every kind-10063 — PyGObject swallows handler exceptions — so the
    cache never populated and every resolve() fell through to timeout).
    Direct callers (tests) pass None for _client."""
    try:
        ev = json.loads(payload) if isinstance(payload, str) else payload
        if not isinstance(ev, dict) or ev.get("kind") != 10063:
            return
        pk = ev.get("pubkey") or ""
        if not pk:
            return
        servers = parse_blossom_servers(ev)
        with _lock:
            _servers[pk] = (servers, time.monotonic() + SERVER_TTL_S)
            waiter = _inflight.pop(pk, None)
        if waiter is not None:
            waiter.set()
    except Exception:
        pass


def _request_servers(author_pk):
    """Blocking: author's Blossom servers (fresh cache hit, or one relay
    REQ shared by all concurrent waiters). Returns [] on timeout — the
    empty list is negative-cached so a burst of dead media from an author
    without a 10063 costs one REQ, not one per image."""
    now = time.monotonic()
    with _lock:
        entry = _servers.get(author_pk)
        if entry and entry[1] > now:
            return entry[0]
        waiter = _inflight.get(author_pk)
        owner = waiter is None
        if owner:
            waiter = threading.Event()
            _inflight[author_pk] = waiter
    if owner:
        try:
            if _request_fn is not None:
                _request_fn(author_pk)
        except Exception:
            pass
        waiter.wait(RELAY_WAIT_S)
        with _lock:
            entry = _servers.get(author_pk)
            if not (entry and entry[1] > time.monotonic()):
                entry = ([], time.monotonic() + SERVER_TTL_S)
                _servers[author_pk] = entry
            _inflight.pop(author_pk, None)
        waiter.set()  # release concurrent waiters with the same answer
        return entry[0]
    waiter.wait(RELAY_WAIT_S)
    with _lock:
        entry = _servers.get(author_pk)
    return entry[0] if entry and entry[1] > time.monotonic() else []


# --- blob fetch + verification -------------------------------------------------

def _http_get(url, timeout=BLOB_TIMEOUT_S):
    """GET via the shared media proxy opener (VID-PROXY-MEDIA). Returns
    bytes or None; over-cap responses are rejected."""
    req = urllib.request.Request(url, headers={"User-Agent": "Gnostr/1.0"})
    opener = proxy_opener.proxy_opener()
    with opener.open(req, timeout=timeout) as resp:
        data = resp.read(MAX_BLOB_BYTES + 1)
    if len(data) > MAX_BLOB_BYTES:
        return None
    return data


def _fetch_blob(server, sha):
    """BUD-03 GET <server>/<sha256>, verified. Returns the bytes only when
    sha256(bytes) == sha; a mismatch is a hard failure (wrong bytes must
    never reach the screen)."""
    try:
        data = _http_get(f"{server}/{sha}")
    except Exception:
        return None
    if not data:
        return None
    if hashlib.sha256(data).hexdigest() != sha:
        return None
    return data


# --- public API (worker thread only) -------------------------------------------

def resolve_blob(url, author_pk):
    """Attempt Blossom recovery for a dead content-addressed URL.
    Returns (verified_url, bytes) on success, (None, None) on any failure
    (caller keeps the original URL / broken-media UX). Blocking — call
    from a worker thread, never the GLib main loop."""
    try:
        if not author_pk or not enabled():
            return None, None
        sha = content_hash(url)
        if not sha:
            return None, None
        for server in _request_servers(author_pk):
            blob = _fetch_blob(server, sha)
            if blob is not None:
                return f"{server}/{sha}", blob
    except Exception:
        pass
    return None, None


def resolve(url, author_pk):
    """Like resolve_blob but returns only the verified URL (video path:
    playbin3 re-fetches the blob itself from the Blossom server)."""
    return resolve_blob(url, author_pk)[0]
