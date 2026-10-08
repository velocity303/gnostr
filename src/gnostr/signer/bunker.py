"""NIP-46 bunker client (BunkerBackend).

Implements the client side of NIP-46 remote signing:
- parse `bunker://<remote-signer-pubkey>?relay=...&secret=...`
- disposable client keypair (generated here, deleted on disconnect)
- JSON-RPC requests as kind-24133 events, NIP-44 v2 encrypted to the
  remote-signer pubkey (reuses gnostr.nip44 — no second crypto copy)
- responses decrypted from kind-24133 events authored by the signer and
  p-tagged to our client pubkey
- per-sign-request approval callback + sliding-window rate guard
- `get_public_key` is re-called after `connect` per current NIP-46

Transport: this module NEVER opens a WebSocket. It publishes through the
injected NostrClient (`publish` / `request_once` / `add_relay`) and receives
responses via `handle_event`, which NostrClient._handle_event routes for
kind 24133. That keeps client.py's sole-relay-ownership contract intact.
"""

import json
import secrets
import threading
import time
from collections import deque
from urllib.parse import parse_qs, urlparse

from gnostr import nip44, nostr_utils

from .backend import SignerBackend

KIND_NIP46 = 24133

# Client metadata sent with `connect` so the signer can label the session.
CLIENT_METADATA = {"name": "gnostr", "url": "https://livingonlinux.tech"}


class BunkerError(Exception):
    """Any protocol-level failure in the NIP-46 exchange."""


def parse_bunker_uri(uri):
    """Parse `bunker://<signer-pubkey>?relay=<r>&relay=<r>&secret=<s>`.

    Returns (signer_pubkey_hex, [relay_urls], secret_or_None).
    Raises ValueError on a malformed URI (bad scheme, bad key, no relay).
    """
    uri = (uri or "").strip()
    if not uri.startswith("bunker://"):
        raise ValueError("not a bunker:// URI")
    parsed = urlparse(uri)
    signer = (parsed.netloc or parsed.path).strip("/").strip()
    if not nostr_utils.is_valid_hex_key(signer):
        raise ValueError("remote-signer pubkey is not 64-char hex")
    qs = parse_qs(parsed.query)
    relays = [r for r in qs.get("relay", []) if r.startswith("ws")]
    if not relays:
        raise ValueError("bunker URI has no relay= parameter")
    secret = qs.get("secret", [None])[0]
    return signer, relays, secret


class _PendingRequest:
    __slots__ = ("done", "result", "error")

    def __init__(self):
        self.done = threading.Event()
        self.result = None
        self.error = None


class BunkerBackend(SignerBackend):
    """NIP-46 remote-signing backend.

    `client` is the NostrClient used purely as transport (publish/request_once/
    add_relay). `on_sign_request(kind, preview) -> bool` is consulted before
    every sign_event RPC; returning False rejects the request locally.
    `rate_limit`/`rate_window` cap sign requests per window (sliding deque) so
    a compromised or spammy signer cannot hammer the user with prompts.
    """

    is_local = False

    def __init__(
        self,
        client,
        bunker_uri,
        on_sign_request=None,
        rate_limit=5,
        rate_window=10.0,
        timeout=30.0,
    ):
        self._client = client
        self.signer_pubkey, self.relays, self.secret = parse_bunker_uri(bunker_uri)
        # Disposable client keypair — NIP-46 §1. Never the user's key.
        self.client_priv, self.client_pub = nostr_utils.generate_keypair()
        self._conv = nip44.get_conversation_key(
            bytes.fromhex(self.client_priv), bytes.fromhex(self.signer_pubkey)
        )
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._user_pubkey = None
        self.connected = False
        self.on_sign_request = on_sign_request
        self._rate_limit = rate_limit
        self._rate_window = rate_window
        self._sign_times = deque()
        self._timeout = timeout

    # -- SignerBackend interface -------------------------------------------

    def get_public_key(self):
        return self._user_pubkey

    def sign_event(self, event):
        """Remote sign_event RPC. Returns the signed event dict or None when
        the user denies, the rate guard trips, or the RPC fails."""
        if not self.connected:
            return None
        if not self._rate_ok():
            return None
        if self.on_sign_request is not None:
            preview = (event.get("content") or "")[:140]
            try:
                if not self.on_sign_request(event.get("kind"), preview):
                    return None
            except Exception:
                return None
        try:
            result = self._rpc(
                "sign_event",
                [json.dumps({
                    "kind": event.get("kind"),
                    "content": event.get("content", ""),
                    "tags": event.get("tags", []),
                    "created_at": event.get("created_at", int(time.time())),
                })],
            )
        except BunkerError:
            return None
        if not isinstance(result, str):
            return None
        try:
            signed = json.loads(result)
        except ValueError:
            return None
        # The signer must answer with an event for OUR user, correctly hashed.
        if not isinstance(signed, dict):
            return None
        if signed.get("pubkey") != self._user_pubkey:
            return None
        if signed.get("id") != nostr_utils.compute_event_id(signed):
            return None
        return signed

    # -- connection lifecycle ----------------------------------------------

    def connect(self):
        """Send `connect`, validate the echoed secret, then re-call
        get_public_key (current NIP-46). Returns the user pubkey hex."""
        for url in self.relays:
            self._client.add_relay(url)
        # Listen for signer responses BEFORE sending connect so a fast signer
        # cannot win the race. request_once (not subscribe) so we never CLOSE
        # the feed's live subscription — relays keep streaming a REQ until it
        # is closed or the connection drops.
        self._client.request_once(
            f"nip46_{self.client_pub[:8]}",
            {
                "kinds": [KIND_NIP46],
                "authors": [self.signer_pubkey],
                "#p": [self.client_pub],
                "since": int(time.time()) - 5,
            },
        )
        result = self._rpc(
            "connect",
            [self.signer_pubkey, self.secret or "", "", json.dumps(CLIENT_METADATA)],
        )
        if self.secret and result != self.secret:
            raise BunkerError("connect secret mismatch — possible spoof")
        pk = self._rpc("get_public_key", [])
        if not pk or not nostr_utils.is_valid_hex_key(pk):
            raise BunkerError("signer returned an invalid user pubkey")
        self._user_pubkey = pk
        self.connected = True
        return pk

    def disconnect(self):
        """Courtesy `logout` + destroy the disposable client keypair."""
        if self.connected:
            try:
                self._rpc("logout", [], timeout=5.0)
            except BunkerError:
                pass
        self.connected = False
        self._user_pubkey = None
        self.client_priv = None
        self._conv = None

    # -- inbound routing (called from NostrClient._handle_event) ------------

    def handle_event(self, ev):
        """Decrypt a kind-24133 response from our signer and resolve the
        matching pending request. Foreign authors/clients are ignored."""
        if self._conv is None:
            return
        if ev.get("pubkey") != self.signer_pubkey:
            return
        p_tags = [t[1] for t in ev.get("tags", []) if len(t) > 1 and t[0] == "p"]
        if self.client_pub not in p_tags:
            return
        try:
            resp = json.loads(nip44.decrypt(ev["content"], self._conv))
        except Exception:
            return  # foreign/garbage box — never fatal
        with self._pending_lock:
            slot = self._pending.get(resp.get("id"))
        if slot is None:
            return
        if resp.get("error"):
            slot.error = str(resp["error"])
        else:
            slot.result = resp.get("result")
        slot.done.set()

    # -- RPC plumbing --------------------------------------------------------

    def _rate_ok(self):
        """Sliding-window guard: at most rate_limit sign requests per window."""
        now = time.monotonic()
        while self._sign_times and now - self._sign_times[0] > self._rate_window:
            self._sign_times.popleft()
        if len(self._sign_times) >= self._rate_limit:
            return False
        self._sign_times.append(now)
        return True

    def _rpc(self, method, params, timeout=None):
        """Send one NIP-46 request, block for the matching response.
        Returns the `result` string; raises BunkerError on error/timeout."""
        conv = self._conv
        if self.client_priv is None or conv is None:
            raise BunkerError("backend disconnected")
        req_id = secrets.token_hex(8)
        slot = _PendingRequest()
        with self._pending_lock:
            self._pending[req_id] = slot
        try:
            payload = {"id": req_id, "method": method, "params": params}
            content = nip44.encrypt(json.dumps(payload), conv)
            # The CLIENT keypair signs NIP-46 transport events (NIP-46 §
            # Request Events) — this is local signing of our disposable key,
            # not the user's, so nostr_utils.sign_event is correct here.
            event = nostr_utils.build_event(
                self.client_pub, KIND_NIP46, content, [["p", self.signer_pubkey]]
            )
            signed = nostr_utils.sign_event(event, self.client_priv)
            if not signed:
                raise BunkerError("failed to sign NIP-46 request")
            self._client.publish(signed)
            if not slot.done.wait(timeout or self._timeout):
                raise BunkerError(f"{method}: timed out waiting for signer")
            if slot.error:
                raise BunkerError(f"{method}: {slot.error}")
            return slot.result
        finally:
            with self._pending_lock:
                self._pending.pop(req_id, None)
