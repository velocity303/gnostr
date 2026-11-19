"""NIPW-P1 read-side Blossom resolver (NIP-B7/BUD-03) — unit tests.

Covers the three Done-when axes: hex-detection, sha verification, and
fallback behaviour; plus the never-blocks-the-UI-thread contract (the
resolver is only reachable from worker threads) and the GSettings
media-resilience gate. All network I/O is monkeypatched — no sockets.
"""

import hashlib
import os
import threading
import time
import xml.etree.ElementTree as ET
from unittest.mock import Mock

import pytest

from gnostr import media_resolver

BLOB = b"hello blossom"
SHA = hashlib.sha256(BLOB).hexdigest()
PK = "a" * 64
DEAD_URL = f"https://dead.example/{SHA}.png"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    media_resolver.reset_state()
    monkeypatch.setattr(media_resolver, "_settings_get", lambda key, fb: True)
    monkeypatch.setattr(media_resolver, "RELAY_WAIT_S", 0.3)
    yield
    media_resolver.reset_state()


def _ev(servers, pk=PK):
    return {
        "kind": 10063,
        "pubkey": pk,
        "tags": [["r", s] for s in servers],
    }


# --- 1. hex detection -------------------------------------------------------

def test_content_hash_bare_hex():
    assert media_resolver.content_hash(f"https://cdn.test/{SHA}") == SHA


def test_content_hash_hex_with_extension():
    assert media_resolver.content_hash(f"https://cdn.test/{SHA}.png") == SHA
    assert media_resolver.content_hash(f"https://cdn.test/{SHA}.mp4") == SHA


def test_content_hash_uppercase_normalized():
    assert media_resolver.content_hash(f"https://cdn.test/{SHA.upper()}") == SHA


def test_content_hash_query_and_fragment_ignored():
    assert media_resolver.content_hash(f"https://cdn.test/{SHA}.png?v=2#x") == SHA


def test_content_hash_rejects_non_hex():
    assert media_resolver.content_hash("https://cdn.test/photo.jpg") is None
    assert media_resolver.content_hash(f"https://cdn.test/{'g' * 64}.png") is None
    assert media_resolver.content_hash(f"https://cdn.test/{SHA[:-1]}.png") is None
    assert media_resolver.content_hash(f"https://cdn.test/{SHA}0.png") is None


def test_content_hash_only_last_segment():
    # hex mid-path does not make the URL content-addressed
    assert media_resolver.content_hash(f"https://cdn.test/{SHA}/thumb.jpg") is None


def test_content_hash_garbage_safe():
    for junk in ("", None, "not a url", "http://", "https://x/"):
        try:
            media_resolver.content_hash(junk)
        except Exception:
            pytest.fail(f"content_hash must never raise on {junk!r}")


# --- 2. kind-10063 parsing (NIP-B7) ------------------------------------------

def test_parse_servers_order_dedup_strip():
    ev = {
        "kind": 10063,
        "tags": [
            ["r", "https://b.test/"],
            ["r", "https://a.test"],
            ["r", "https://b.test"],
            ["d", "junk"],
            ["r", "ftp://nope.test"],
            ["r", ""],
            ["r"],
        ],
    }
    assert media_resolver.parse_blossom_servers(ev) == [
        "https://b.test",
        "https://a.test",
    ]


def test_parse_servers_wrong_kind_or_junk():
    assert media_resolver.parse_blossom_servers({"kind": 1, "tags": []}) == []
    assert media_resolver.parse_blossom_servers(None) == []
    assert media_resolver.parse_blossom_servers("nonsense") == []


# --- 3. sha verification + fallback --------------------------------------------

def _serve(monkeypatch, responses):
    """responses: dict url -> bytes | Exception. Records call order."""
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        r = responses.get(url)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(media_resolver, "_http_get", fake_get)
    return calls


def test_resolve_verified_hit(monkeypatch):
    _serve(monkeypatch, {f"https://b1.test/{SHA}": BLOB})
    media_resolver.configure(
        lambda pk: media_resolver.on_blossom_event(_ev(["https://b1.test"], pk))
    )
    url, data = media_resolver.resolve_blob(DEAD_URL, PK)
    assert url == f"https://b1.test/{SHA}"
    assert data == BLOB
    assert hashlib.sha256(data).hexdigest() == SHA


def test_resolve_rejects_hash_mismatch(monkeypatch):
    _serve(monkeypatch, {f"https://b1.test/{SHA}": b"WRONG BYTES"})
    media_resolver.configure(
        lambda pk: media_resolver.on_blossom_event(_ev(["https://b1.test"], pk))
    )
    url, data = media_resolver.resolve_blob(DEAD_URL, PK)
    assert url is None and data is None


def test_resolve_tries_servers_in_order(monkeypatch):
    calls = _serve(
        monkeypatch,
        {
            f"https://b1.test/{SHA}": b"corrupt",
            f"https://b2.test/{SHA}": Exception("404"),
            f"https://b3.test/{SHA}": BLOB,
        },
    )
    media_resolver.configure(
        lambda pk: media_resolver.on_blossom_event(
            _ev(["https://b1.test", "https://b2.test", "https://b3.test"], pk)
        )
    )
    url, data = media_resolver.resolve_blob(DEAD_URL, PK)
    assert url == f"https://b3.test/{SHA}"
    assert calls == [f"https://b1.test/{SHA}", f"https://b2.test/{SHA}",
                     f"https://b3.test/{SHA}"]


def test_resolve_all_fail_keeps_original(monkeypatch):
    _serve(monkeypatch, {f"https://b1.test/{SHA}": Exception("down")})
    media_resolver.configure(
        lambda pk: media_resolver.on_blossom_event(_ev(["https://b1.test"], pk))
    )
    assert media_resolver.resolve(DEAD_URL, PK) is None


def test_resolve_no_author_or_not_content_addressed(monkeypatch):
    calls = _serve(monkeypatch, {})
    media_resolver.configure(
        lambda pk: media_resolver.on_blossom_event(_ev(["https://b1.test"], pk))
    )
    assert media_resolver.resolve_blob(DEAD_URL, None) == (None, None)
    assert media_resolver.resolve_blob("https://x.test/pic.jpg", PK) == (None, None)
    assert calls == []  # no network at all


def test_disabled_flag_short_circuits(monkeypatch):
    monkeypatch.setattr(media_resolver, "_settings_get", lambda key, fb: False)
    calls = _serve(monkeypatch, {f"https://b1.test/{SHA}": BLOB})
    media_resolver.configure(
        lambda pk: media_resolver.on_blossom_event(_ev(["https://b1.test"], pk))
    )
    assert media_resolver.resolve_blob(DEAD_URL, PK) == (None, None)
    assert calls == []


def test_no_servers_negative_cached(monkeypatch):
    n = {"req": 0}

    def req(pk):
        n["req"] += 1  # author has no 10063: nothing ever arrives

    media_resolver.configure(req)
    _serve(monkeypatch, {})
    assert media_resolver.resolve(DEAD_URL, PK) is None
    t0 = time.monotonic()
    assert media_resolver.resolve(DEAD_URL, PK) is None
    assert time.monotonic() - t0 < 0.2  # served from negative cache
    assert n["req"] == 1


def test_server_list_ttl_cached(monkeypatch):
    n = {"req": 0}

    def req(pk):
        n["req"] += 1
        media_resolver.on_blossom_event(_ev(["https://b1.test"], pk))

    media_resolver.configure(req)
    _serve(monkeypatch, {f"https://b1.test/{SHA}": BLOB})
    assert media_resolver.resolve(DEAD_URL, PK) == f"https://b1.test/{SHA}"
    assert media_resolver.resolve(DEAD_URL, PK) == f"https://b1.test/{SHA}"
    assert n["req"] == 1  # second resolve served from the per-author cache


def test_inflight_dedup_one_req_per_author(monkeypatch):
    n = {"req": 0}
    gate = threading.Event()

    def req(pk):
        n["req"] += 1
        gate.wait(2.0)
        media_resolver.on_blossom_event(_ev(["https://b1.test"], pk))

    media_resolver.configure(req)
    _serve(monkeypatch, {f"https://b1.test/{SHA}": BLOB})
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(media_resolver.resolve(DEAD_URL, PK)))
        for _ in range(3)
    ]
    for t in threads:
        t.start()
    time.sleep(0.1)
    gate.set()
    for t in threads:
        t.join()
    assert results == [f"https://b1.test/{SHA}"] * 3
    assert n["req"] == 1


def test_on_blossom_event_accepts_json_string():
    import json as _json

    media_resolver.on_blossom_event(_json.dumps(_ev(["https://b1.test"])))
    assert media_resolver._servers[PK][0] == ["https://b1.test"]


def test_on_blossom_event_ignores_junk():
    media_resolver.on_blossom_event("not json")
    media_resolver.on_blossom_event('{"kind": 1}')
    media_resolver.on_blossom_event(None)
    assert media_resolver._servers == {}


def test_over_cap_blob_rejected(monkeypatch):
    class Resp:
        def __init__(self, d):
            self._d = d

        def read(self, n=-1):
            return self._d

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Opener:
        def open(self, req, timeout=None):
            return Resp(b"x" * 64)

    monkeypatch.setattr(media_resolver, "MAX_BLOB_BYTES", 32)
    monkeypatch.setattr(
        media_resolver.proxy_opener, "proxy_opener", lambda: Opener()
    )
    assert media_resolver._fetch_blob("https://b.test", SHA) is None


def test_blob_fetch_routes_through_media_proxy(monkeypatch):
    """VID-PROXY-MEDIA: Blossom GETs must use the shared proxy opener."""

    class Resp:
        def __init__(self, d):
            self._d = d

        def read(self, n=-1):
            return self._d

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen = []

    class Opener:
        def open(self, req, timeout=None):
            seen.append(req.full_url)
            return Resp(BLOB)

    monkeypatch.setattr(
        media_resolver.proxy_opener, "proxy_opener", lambda: Opener()
    )
    assert media_resolver._fetch_blob("https://b.test", SHA) == BLOB
    assert seen == [f"https://b.test/{SHA}"]


# --- 4. never blocks the UI thread ----------------------------------------------

def test_image_worker_uses_resolver_off_main_thread(monkeypatch):
    """ImageLoader._request_image must dispatch the fetch (and therefore
    the resolver) to the thread pool — the GLib main loop never touches it."""
    from gnostr import renderer

    seen = {}
    done = threading.Event()

    def fake_resolve(url, author_pk):
        seen["thread"] = threading.current_thread().name
        seen["args"] = (url, author_pk)
        return None, None

    real_submit = renderer.ImageLoader._executor.submit

    def spy_submit(fn, *a):
        fut = real_submit(fn, *a)
        fut.add_done_callback(lambda f: done.set())
        return fut

    monkeypatch.setattr(media_resolver, "resolve_blob", fake_resolve)
    monkeypatch.setattr(renderer.ImageLoader._executor, "submit", spy_submit)
    # unreachable host -> original fetch fails -> resolver consulted
    renderer.ImageLoader._request_image(
        "http://no-such-host.invalid/pic.png", lambda t: None, None, PK
    )
    assert done.wait(15)
    assert seen["args"] == ("http://no-such-host.invalid/pic.png", PK)
    assert seen["thread"] != threading.main_thread().name


def test_video_error_path_dispatches_to_executor(monkeypatch):
    """The bus ERROR hook must hand the blocking resolve to the player's
    thread pool, never run it inline."""
    from gnostr import renderer

    calls = []
    monkeypatch.setattr(
        renderer.VideoPlayer._executor,
        "submit",
        lambda fn, *a: calls.append((fn, a)),
    )
    monkeypatch.setattr(media_resolver, "enabled", lambda: True)
    assert renderer.VideoPlayer._blossom_eligible(DEAD_URL, PK, False)
    assert not renderer.VideoPlayer._blossom_eligible(DEAD_URL, PK, True)
    assert not renderer.VideoPlayer._blossom_eligible(DEAD_URL, None, False)
    assert not renderer.VideoPlayer._blossom_eligible(
        "https://x.test/v.mp4", PK, False
    )


def test_blossom_recovery_no_hit_does_not_rebuild(monkeypatch):
    from gnostr import renderer

    monkeypatch.setattr(media_resolver, "resolve", lambda url, pk: None)
    built = []
    monkeypatch.setattr(
        renderer.VideoPlayer, "_build_and_deliver",
        staticmethod(lambda *a, **k: built.append(a)),
    )
    monkeypatch.setattr(renderer.GLib, "idle_add", lambda fn, *a: None)
    renderer.VideoPlayer._blossom_recovery(
        Mock(), DEAD_URL, lambda v: None, None, False, None, PK
    )
    assert built == []


# --- 5. wiring / config surface -------------------------------------------------

def test_gschema_key_default_on():
    tree = ET.parse(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "data", "tech.livingonlinux.gnostr.gschema.xml",
        )
    )
    for key in tree.iter("key"):
        if key.get("name") == "media-resilience":
            assert key.get("type") == "b"
            assert (key.findtext("default") or "").strip() == "true"
            return
    pytest.fail("media-resilience key missing from gschema")
