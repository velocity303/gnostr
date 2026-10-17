"""DM-3: resilient publish — retry on the next relay when one rejects a write.

NIP-01 OK accepted=false on one relay must NOT fail the send while other
candidate relays remain: the signed event is retried on every untried
candidate (fallback relays are connected on demand), publish-result fires
only after all candidates fail, and the first OK accepted=true settles the
publish (dedupe — later acks are ignored, no duplicate delivery).

Under the test gi mocks, GObject.Object is a Mock, so `class
NostrClient(GObject.Object)` evaluates to a Mock and its methods are not
introspectable. These tests load the real client.py source and bind the
methods under test to a lightweight fake self (FakeClient pattern from
test_follow_sync.py) with fake relays standing in for NostrRelay.
"""

import ast
import collections
import os
import types
from unittest.mock import MagicMock

import pytest

_CLIENT_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", "gnostr", "client.py"
    )
)

_client_src = open(_CLIENT_PATH, encoding="utf-8").read()


def _unbound(method_name):
    """Extract the real function object for NostrClient.<method_name> from
    client.py source without importing it under the mocked GObject."""
    import json as _json
    import time as _time

    import gnostr.nostr_utils as _nu  # noqa: F401

    tree = ast.parse(_client_src)
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NostrClient"
    )
    fn = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name
    )
    mod = ast.Module(body=[fn], type_ignores=[])
    ns = {
        "json": _json,
        "time": _time,
        "gnostr": __import__("gnostr"),
        "GLib": types.SimpleNamespace(idle_add=lambda fn, *a: fn(*a)),
    }
    exec(compile(mod, _CLIENT_PATH, "exec"), ns)
    return ns[method_name]


RELAY_A = "wss://relay-a.example"
RELAY_B = "wss://relay-b.example"
RELAY_C = "wss://relay-c.example"


class FakeRelay:
    """Stand-in for NostrRelay: records publishes, never opens a socket.
    `connected=False` mimics a fallback relay still dialing."""

    def __init__(self, url, connected=True):
        self.url = url
        self.is_connected = connected
        self.published = []
        self.publish_queue = []

    def publish(self, event):
        if not self.is_connected:
            return False
        self.published.append(event)
        return True

    def _flush_publish_queue(self):
        # Mirrors NostrRelay._flush_publish_queue: socket opened, drain queue.
        self.published.extend(self.publish_queue)
        self.publish_queue.clear()


class FakeClient:
    """Minimal self for NostrClient publish-resilience methods. Unimplemented
    names auto-bind from client.py source (FakeClient pattern)."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        try:
            fn = _unbound(name)
        except StopIteration:
            raise AttributeError(name)
        bound = fn.__get__(self)
        object.__setattr__(self, name, bound)
        return bound

    def __init__(self, relay_urls=(RELAY_A, RELAY_B), connected=(RELAY_A, RELAY_B)):
        self.db = MagicMock()
        self.my_pubkey = "a" * 64
        self.my_privkey = "b" * 64
        self.relay_urls = set(relay_urls)
        self.active_relays = {u: FakeRelay(u) for u in connected}
        self.pending_publishes = {}
        self.relay_log = collections.deque(maxlen=200)
        self.emitted = []  # ("publish-result", eid, accepted, msg, url, label)
        self.connected_log = []  # add_relay_connection calls (fallback connects)

    def emit(self, *a):
        self.emitted.append(a)

    def add_relay_connection(self, url):
        # Real client starts a websocket here; the fake just records the
        # connect attempt and installs a still-dialing relay.
        self.connected_log.append(url)
        self.active_relays.setdefault(url, FakeRelay(url, connected=False))


@pytest.fixture
def client():
    return FakeClient()


def _event(eid="e" * 64):
    return {"id": eid, "pubkey": "a" * 64, "kind": 4, "content": "x", "tags": []}


def _results(client):
    return [e for e in client.emitted if e[0] == "publish-result"]


# ---------------------------------------------------------------------------
# 1. First relay rejects, second accepts -> success, retried on the second
# ---------------------------------------------------------------------------


def test_rejection_retries_next_relay_and_accepts(client):
    """DM-style: event went out on A only (targeted), B is an untried
    candidate. A rejects -> retry to B -> B accepts -> one accepted result."""
    ev = _event()
    client.publish_to_relays(ev, [RELAY_A])
    client._track_publish(ev, "DM", sent_urls={RELAY_A}, candidates=[RELAY_A, RELAY_B])

    client._handle_ok(ev["id"], False, "rate-limited", RELAY_A)
    # Retry fired: B received the event, nothing settled yet.
    assert client.active_relays[RELAY_B].published == [ev]
    assert _results(client) == []

    client._handle_ok(ev["id"], True, "", RELAY_B)
    results = _results(client)
    assert len(results) == 1
    _, eid, accepted, _, url, label = results[0]
    assert (eid, accepted, url, label) == (ev["id"], True, RELAY_B, "DM")
    assert ev["id"] not in client.pending_publishes


# ---------------------------------------------------------------------------
# 2. All relays reject -> exactly ONE failure result naming the rejecting relay
# ---------------------------------------------------------------------------


def test_all_rejections_settle_single_failure(client):
    ev = _event("d" * 64)
    client.publish_to_relays(ev, [RELAY_A, RELAY_B])
    client._track_publish(
        ev, "DM", sent_urls={RELAY_A, RELAY_B}, candidates=[RELAY_A, RELAY_B]
    )

    client._handle_ok(ev["id"], False, "blocked", RELAY_A)
    assert _results(client) == []  # B hasn't answered yet — stay silent
    client._handle_ok(ev["id"], False, "blocked", RELAY_B)

    results = _results(client)
    assert len(results) == 1
    _, eid, accepted, message, url, label = results[0]
    assert (eid, accepted, label) == (ev["id"], False, "DM")
    assert "blocked" in message
    assert url == RELAY_B  # rejecting relay surfaced in the result
    # Rejecting relay surfaced in the relay log too.
    assert any("REJECTED" in line and RELAY_A in line for line in client.relay_log)
    assert ev["id"] not in client.pending_publishes


# ---------------------------------------------------------------------------
# 3. Dedupe: first OK accepted=true settles; later acks are ignored
# ---------------------------------------------------------------------------


def test_first_accept_settles_and_later_rejection_ignored(client):
    ev = _event("a" * 64)
    client.publish(ev)
    client._track_publish(ev, "Post", sent_urls={RELAY_A, RELAY_B})

    client._handle_ok(ev["id"], True, "", RELAY_A)
    client._handle_ok(ev["id"], False, "later noise", RELAY_B)

    results = _results(client)
    assert len(results) == 1
    assert results[0][2] is True
    # No retry was attempted after the acceptance.
    assert client.active_relays[RELAY_B].published == [ev]  # only the original


def test_accept_after_retry_only_settles_once(client):
    ev = _event("b" * 64)
    client.publish_to_relays(ev, [RELAY_A])
    client._track_publish(ev, "DM", sent_urls={RELAY_A}, candidates=[RELAY_A, RELAY_B])
    client._handle_ok(ev["id"], False, "rate-limited", RELAY_A)
    client._handle_ok(ev["id"], True, "", RELAY_B)
    client._handle_ok(ev["id"], False, "late reject", RELAY_A)
    results = _results(client)
    assert len(results) == 1 and results[0][2] is True


# ---------------------------------------------------------------------------
# 4. Fallback relay: not connected yet -> connected on demand, event queued
# ---------------------------------------------------------------------------


def test_retry_connects_fallback_relay_and_flushes(client):
    """Candidate B is configured but not active: rejection on A must connect
    B and hand it the event (queued until its socket opens)."""
    client.relay_urls = {RELAY_A, RELAY_C}
    client.active_relays = {RELAY_A: FakeRelay(RELAY_A)}
    ev = _event("c" * 64)
    client.publish_to_relays(ev, [RELAY_A])
    client._track_publish(ev, "DM", sent_urls={RELAY_A}, candidates=[RELAY_A, RELAY_C])

    client._handle_ok(ev["id"], False, "blocked", RELAY_A)
    assert RELAY_C in client.connected_log  # fallback connected on demand
    fallback = client.active_relays[RELAY_C]
    assert ev in fallback.publish_queue  # queued while still dialing
    assert _results(client) == []

    # Socket opens: queued EVENT flushes, then the relay accepts.
    fallback.is_connected = True
    fallback._flush_publish_queue()
    assert fallback.published == [ev]
    client._handle_ok(ev["id"], True, "", RELAY_C)
    results = _results(client)
    assert len(results) == 1 and results[0][2] is True


# ---------------------------------------------------------------------------
# 5. No-ack timeout: sweep retries untried candidates before failing
# ---------------------------------------------------------------------------


def test_sweep_retries_untried_then_second_round_settles(client):
    ev = _event("f" * 64)
    client.publish_to_relays(ev, [RELAY_A])
    client._track_publish(ev, "DM", sent_urls={RELAY_A}, candidates=[RELAY_A, RELAY_B])
    client.pending_publishes[ev["id"]]["expires"] = 0  # 30s window elapsed

    client.sweep_pending_publishes()
    # Sweep retried B instead of failing; window re-armed.
    assert client.active_relays[RELAY_B].published == [ev]
    assert _results(client) == []
    assert client.pending_publishes[ev["id"]]["expires"] > 0

    # B also rejects. A never acked (that's the no-ack scenario), so the
    # entry waits one more window for A's OK before settling as failed.
    client._handle_ok(ev["id"], False, "blocked", RELAY_B)
    assert _results(client) == []

    client.pending_publishes[ev["id"]]["expires"] = 0
    client.sweep_pending_publishes()
    results = _results(client)
    assert len(results) == 1 and results[0][2] is False
    assert "blocked" in results[0][3]


def test_sweep_no_candidates_no_ack_keeps_legacy_message(client):
    """No untried candidates and no OK at all: the original 'not confirmed'
    result is preserved (posts/reactions with every relay silent)."""
    ev = _event("9" * 64)
    client.publish(ev)
    client._track_publish(ev, "Post", sent_urls={RELAY_A, RELAY_B})
    client.pending_publishes[ev["id"]]["expires"] = 0

    client.sweep_pending_publishes()
    results = _results(client)
    assert len(results) == 1
    assert results[0][2] is False
    assert "no relay acknowledged within 30s" in results[0][3]


# ---------------------------------------------------------------------------
# 6. Shared dispatch path: posts/reactions benefit via publish() sent_urls
# ---------------------------------------------------------------------------


def test_general_publish_rejects_all_then_single_failure(client):
    """publish() fans out to every active relay; both reject -> one failure
    result, no retry (all candidates already tried)."""
    ev = _event("1" * 64)
    sent = client.publish(ev)
    assert sent == {RELAY_A, RELAY_B}
    client._track_publish(ev, "Like", sent_urls=sent)

    client._handle_ok(ev["id"], False, "rate-limited", RELAY_A)
    assert _results(client) == []
    client._handle_ok(ev["id"], False, "rate-limited", RELAY_B)
    results = _results(client)
    assert len(results) == 1
    _, _, accepted, message, _, label = results[0]
    assert accepted is False
    assert "rate-limited" in message
    assert label == "Like"
