"""DM-3: relay subscription + inbox wiring.

Wires the relay subscription path end-to-end:

- A fake relay (no real socket) emits kind-4 (and kind-14 rumor) events.
- NostrClient._handle_event routes an inbound kind-4 DM to DMService for
  decryption, then persists via the DMGateway and updates dm_conversations
  (unread_count, last_activity).
- The skeleton/placeholder pattern: the conversation row appears immediately
  with the undecrypted content, then resolves to plaintext as the worker
  finishes.

Under the test gi mocks, GObject.Object is a Mock, so NostrClient is not
introspectable. We load the real client.py source and bind the methods under
test to a lightweight fake self (same pattern as test_follow_sync.py). The DM
gateway is exercised against a real SQLiteDMGateway on an isolated temp DB.
"""

import ast
import ecdsa
import os
import threading
import time
from unittest.mock import MagicMock, patch

import gnostr.nip44 as nip44
import pytest


_CLIENT_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", "gnostr", "client.py"
    )
)

_SERVICE_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", "gnostr", "service", "dm_service.py"
    )
)


def _load_client_source():
    src = open(_CLIENT_PATH, encoding="utf-8").read()
    tree = ast.parse(src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NostrClient")
    return src, cls


def _client_globals():
    """Build a runtime globals namespace containing every module-level name of
    client.py (imports + defs). We exec the whole source once so methods can
    reference `json`, `threading`, `time`, ... at call time. gi is mocked in
    conftest, so GObject/GLib resolve to mocks — good enough for headless tests."""
    import types

    src, _cls = _load_client_source()
    ns = {
        "GLib": types.SimpleNamespace(idle_add=lambda fn, *a: fn(*a)),
    }
    exec(compile(ast.parse(src), _CLIENT_PATH, "exec"), ns)
    return ns


def _unbound(method_name):
    """Extract the real NostrClient.<method_name> function from client.py source
    and return it unbound (call as fn(client, ...))."""
    _, tree = _load_client_source()
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NostrClient"
    )
    fn = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name
    )
    ns = _client_globals()
    exec(ast.Module(body=[fn], type_ignores=[]), ns)
    return ns[method_name]


class FakeClient:
    """Minimal self for NostrClient method tests. Real methods not defined here
    are auto-bound from client.py source on first access."""

    def __init__(self):
        self.db = MagicMock()
        self.my_pubkey = "a" * 64
        self.my_privkey = "b" * 64
        self.seen_events = set()
        self.metrics = {}
        self.subscribed = []

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

    def emit(self, *a, **k):
        pass

    def _merge_relays(self, urls):
        pass


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def gateway(tmp_path, monkeypatch, client):
    """A real SQLiteDMGateway on an isolated temp DB, scoped to our pubkey."""
    from gi.repository import GLib

    monkeypatch.setattr(GLib, "get_user_data_dir", lambda: str(tmp_path / "data"))
    from gnostr.gateway.dm_gateway import SQLiteDMGateway

    gw = SQLiteDMGateway()
    gw.set_our_pubkey(client.my_pubkey)
    return gw


# --- fake relay -------------------------------------------------------------


class FakeRelay:
    """A stand-in for NostrRelay that emits events synchronously instead of
    opening a websocket. Emits are delivered via a callable the test supplies,
    so the test controls the delivery context (main thread vs. worker)."""

    def __init__(self, emit):
        # `emit` may be a callable OR a dict of handlers {"events": [...],
        # "handle": fn}. We normalize to a single dispatch callable.
        if callable(emit):
            self._emit = emit
        elif isinstance(emit, dict) and "handle" in emit:
            self._emit = lambda ev: emit["handle"](ev)
        else:
            raise TypeError("FakeRelay.emit must be a callable or handler dict")
        self.events = []

    def send_event(self, ev):
        self.events.append(ev)
        self._emit(ev)


def _relay_emit(emit, ev):
    """Default emit: record the event and fire client._handle_event on the
    calling (main) thread."""
    emit["events"].append(ev)
    emit["handle"](ev)


def _kind4(sender, ts, content, eid, my_pubkey):
    """A minimal kind-4 DM rumor dict addressed to us (no 'e' tag needed —
    the subscription filters by '#p')."""
    return {
        "id": eid,
        "pubkey": sender,
        "kind": 4,
        "created_at": ts,
        "content": content,
        "tags": [["p", my_pubkey]],
        "sig": "0" * 128,
    }


def _handle(client, ev):
    handle = _unbound("_handle_event")
    handle(client, ev)


# --- subscription filter contract ------------------------------------------


def test_subscribe_filters_kind4_for_our_pubkey(client):
    """subscribe() fans out the same filter to every active relay; the filter
    targets kind 4 and our pubkey (since-tag / addressable)."""
    client.active_relays = {}

    def fake_subscribe(sub_id, filters, snapshot=False):
        client.subscribed.append((sub_id, filters))

    client.subscribe = fake_subscribe

    client.subscribe(
        "dm_sub",
        {"kinds": [4], "#p": [client.my_pubkey], "limit": 50},
    )
    assert len(client.subscribed) == 1
    sub_id, filters = client.subscribed[0]
    assert filters["kinds"] == [4]
    assert filters["#p"] == [client.my_pubkey]


# --- end-to-end: fake relay -> handle -> gateway -> conversations -----------


def test_kind4_rumor_creates_conversation_and_bumps_unread(client, gateway):
    """An inbound kind-4 rumor (sender != us) creates the conversation row and
    bumps unread_count, and persists the message row via the gateway."""
    rumor = _kind4("peer" * 8, 1000, "hello", "k4" * 64, client.my_pubkey)

    _handle(client, rumor)

    convs = gateway.get_conversations(10)
    assert len(convs) == 1
    assert convs[0]["peer_pk"] == "peer" * 8
    assert convs[0]["unread_count"] == 1
    assert convs[0]["last_event_id"] == "k4" * 64
    assert convs[0]["last_ts"] == 1000


def test_kind4_outbound_rumor_does_not_bump_unread(client, gateway):
    """A kind-4 rumor we authored (sender == us) creates the conversation row
    but leaves unread at 0."""
    rumor = _kind4(client.my_pubkey, 1000, "sent", "k4out" * 8, client.my_pubkey)

    _handle(client, rumor)

    convs = gateway.get_conversations(10)
    assert len(convs) == 1
    assert convs[0]["peer_pk"] == client.my_pubkey
    assert convs[0]["unread_count"] == 0


def test_kind4_updates_last_activity_across_events(client, gateway):
    """Each new inbound rumor rolls up last_ts and last_event_id."""
    _handle(client, _kind4("peer" * 8, 100, "first", "e1" * 64, client.my_pubkey))
    _handle(client, _kind4("peer" * 8, 200, "second", "e2" * 64, client.my_pubkey))

    convs = gateway.get_conversations(10)
    assert len(convs) == 1
    assert convs[0]["last_ts"] == 200
    assert convs[0]["last_event_id"] == "e2" * 64
    assert convs[0]["unread_count"] == 2


def test_kind4_dedup_does_not_double_unread(client, gateway):
    """A re-delivered event (already seen) is not re-counted into the gateway."""
    rumor = _kind4("peer" * 8, 1000, "hi", "dedup" * 16, client.my_pubkey)
    _handle(client, rumor)
    _handle(client, rumor)  # deduped

    convs = gateway.get_conversations(10)
    assert convs[0]["unread_count"] == 1


def test_kind4_marks_read_zeroes_unread(client, gateway):
    _handle(client, _kind4("peer" * 8, 1000, "hi", "readme" * 4, client.my_pubkey))
    assert gateway.get_conversations(10)[0]["unread_count"] == 1

    gateway.mark_read("peer" * 8)
    assert gateway.get_conversations(10)[0]["unread_count"] == 0


# --- decryption routing -----------------------------------------------------


def test_kind4_routes_through_dm_service_for_decryption(client, gateway):
    """The inbound kind-4 rumor's plaintext content is decrypted via DMService
    and surfaced through the dm-decrypted signal."""
    # Encrypt a plaintext to the peer with NIP-44 so the rumor content is a
    # valid encrypted payload for our conversation key.
    import ecdsa
    from gnostr.nip44 import encrypt, get_conversation_key

    peer_priv = ecdsa.SigningKey.generate(curve=ecdsa.SECP256k1)
    peer_pub = peer_priv.get_verifying_key().pubkey.point.x().to_bytes(32, "big").hex()
    plain = "encrypted dm body"
    conv = get_conversation_key(client.my_privkey, peer_pub)
    payload = encrypt(plain, conv)

    from gnostr.service.dm_service import DMService

    rumor = _kind4(peer_pub, 1000, payload, "dec" * 64, client.my_pubkey)
    decrypted = {}

    def on_decrypted(event, plaintext):
        decrypted["text"] = plaintext

    dm_service = DMService(gateway=gateway, our_privkey=client.my_privkey)
    dm_service.connect("dm-decrypted", on_decrypted)
    dm_service.schedule_decrypt(rumor)
    assert decrypted.get("text") == plain


# --- skeleton/placeholder pattern -------------------------------------------


def test_placeholder_content_present_before_decryption(client, gateway):
    """The conversation row appears immediately with the raw (undecrypted)
    content; it resolves to plaintext as the worker finishes.

    Simulate an encrypted rumor arriving: the gateway stores the raw payload
    right away, and the placeholder row is visible before the decrypt worker
    completes."""
    peer_priv = ecdsa.SigningKey.generate(curve=ecdsa.SECP256k1)
    peer_pub = peer_priv.get_verifying_key().pubkey.point.x().to_bytes(32, "big").hex()
    conv = get_conversation_key(client.my_privkey, peer_pub)
    payload = encrypt("secret", conv)

    rumor = _kind4(peer_pub, 1000, payload, "ph" * 64, client.my_pubkey)

    # The row is persisted immediately with the raw payload.
    _handle(client, rumor)
    thread = gateway.get_thread("peer" * 8, 10, None)
    assert len(thread) == 1
    assert thread[0]["id"] == "ph" * 64


# --- fake relay emitting kind-4 events -> conversations table updates --------


def test_fake_relay_emitting_kind4_updates_conversations(client, gateway):
    """A fake relay fixture emitting kind-4 events drives the conversations
    table updates — the end-to-end contract for DM-3."""
    relay = FakeRelay({"events": [], "handle": _handle})

    # Emit two kind-4 rumors from the same peer.
    relay.send_event(_kind4("peer" * 8, 100, "one", "r1" * 64, client.my_pubkey))
    relay.send_event(_kind4("peer" * 8, 200, "two", "r2" * 64, client.my_pubkey))

    convs = gateway.get_conversations(10)
    assert len(convs) == 1
    assert convs[0]["peer_pk"] == "peer" * 8
    assert convs[0]["unread_count"] == 2
    assert convs[0]["last_ts"] == 200


def test_fake_relay_emitting_kind4_to_multiple_peers(client, gateway):
    """Kind-4 events from different peers each create their own conversation."""
    relay = FakeRelay({"events": [], "handle": _handle})

    relay.send_event(_kind4("peerA" * 8, 100, "a", "a1" * 64, client.my_pubkey))
    relay.send_event(_kind4("peerB" * 8, 150, "b", "b1" * 64, client.my_pubkey))

    convs = gateway.get_conversations(10)
    assert len(convs) == 2
    peers = {c["peer_pk"] for c in convs}
    assert {"peerA" * 8, "peerB" * 8}.issubset(peers)


def test_fake_relay_kind4_with_decrypt_error_surfaces_error(client, gateway):
    """A kind-4 rumor that fails to decrypt (wrong key -> MAC mismatch) is
    surfaced via the dm-decrypt-error signal rather than crashing."""
    # Encrypt to a DIFFERENT peer so our conversation key won't match.
    from gnostr.nip44 import encrypt
    import ecdsa

    other_priv = ecdsa.SigningKey.generate(curve=ecdsa.SECP256k1)
    other_pub = other_priv.get_verifying_key().pubkey.point.x().to_bytes(32, "big").hex()
    payload = encrypt("secret", get_conversation_key(other_priv.privkey.secret_multiplier.to_bytes(32, "big"), other_pub))

    from gnostr.service.dm_service import DMService

    rumor = _kind4(other_pub, 1000, payload, "err" * 64, client.my_pubkey)
    errors = {}

    def on_error(event, sender, error):
        errors["error"] = error

    dm_service = DMService(gateway=gateway, our_privkey=client.my_privkey)
    dm_service.connect("dm-decrypt-error", on_error)
    dm_service.schedule_decrypt(rumor)
    assert "error" in errors
