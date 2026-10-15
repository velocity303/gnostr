"""G15: NIP-17 DM-relay targeting + unreachable toast (spec req 4).

Spec: docs/specs/nip44-nip17-dms.md 'Relay targeting' step 3 + test plan item 4.

Required behaviour under test:
1. No-10050 peer -> toast 'Recipient is not reachable for DMs' and ABORT:
   ZERO publish calls (the publish mock is never called).
2. Peer WITH 10050 -> the DM is published ONLY to those relays (relay set).
3. TTL cache: a second send to the same peer does not re-fetch kind-10050.
4. Own kind-10050 default derived from the user's write relays (spec step 4).

The 10050 lookup is async: Send must not block the GTK main loop, so the
tests drive the resolution explicitly (relay EVENT arrival via _handle_event,
or the timeout tick) instead of sleeping.

gi is mocked per tests/AGENTS.md; NostrClient/MainWindow are Mock bases under
conftest, so the real methods are extracted from source via ast and bound to
a lightweight fake self (same pattern as test_dm_ui_wiring.py /
test_follow_sync.py). GLib.idle_add is a direct call in this harness, so a
resolution fires its callbacks synchronously — deterministic, no main loop.
"""

import ast
import collections
import os
import time
import types
from unittest.mock import MagicMock

import pytest

_CLIENT_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", "gnostr", "client.py"
    )
)
_MAIN_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", "gnostr", "main.py"
    )
)

PEER = "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"  # real curve point
PEER2 = "d" * 64
DM_RELAY_A = "wss://dm-relay-a.example"
DM_RELAY_B = "wss://dm-relay-b.example"
OTHER_RELAY = "wss://feed-only.example"


def _client_globals():
    """Exec client.py once so extracted methods resolve module-level names
    (json/time/threading/gnostr/DM_RELAY_TTL...). GLib.idle_add is a direct
    call: callbacks run on the calling thread, which is what the tests assert
    on (no real main loop exists headless). NOTE: client.py's own
    `from gi.repository import GLib` rebinds the name to conftest's Mock
    during exec, so the stub must be installed AFTER the exec."""
    src = open(_CLIENT_PATH, encoding="utf-8").read()
    ns = {"__name__": "gnostr.client", "__package__": "gnostr"}
    exec(compile(ast.parse(src), _CLIENT_PATH, "exec"), ns)
    ns["GLib"] = types.SimpleNamespace(
        idle_add=lambda fn, *a: fn(*a),
        timeout_add=lambda ms, fn, *a: None,  # tests drive the tick directly
        get_user_config_dir=lambda: "/tmp/gnostr-test",
    )
    return ns


_NS = _client_globals()


def _unbound_client(method_name):
    src = open(_CLIENT_PATH, encoding="utf-8").read()
    cls = next(
        n
        for n in ast.parse(src).body
        if isinstance(n, ast.ClassDef) and n.name == "NostrClient"
    )
    fn = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name
    )
    ns = dict(_NS)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), _CLIENT_PATH, "exec"), ns)
    return ns[method_name]


class FakeRelay:
    """Stand-in for NostrRelay: records publishes, never opens a socket."""

    def __init__(self, url):
        self.url = url
        self.published = []
        self.requests = []

    def publish(self, event):
        self.published.append(event)
        return True

    def request_once(self, sub_id, filters):
        self.requests.append((sub_id, filters))


class FakeClient:
    """Minimal self for NostrClient relay-targeting methods. Unimplemented
    names auto-bind from client.py source (FakeClient pattern)."""

    def __init__(self):
        self.db = MagicMock()
        self.my_pubkey = "a" * 64
        self.my_privkey = "b" * 64
        self.seen_events = set()
        self.metrics = {}
        self.subscribed = []
        self.published = []  # client-level publishes (own 10050 etc.)
        self.emitted = []
        self.tracked = []
        self.relay_log = collections.deque(maxlen=200)
        self.relay_urls = {DM_RELAY_A, DM_RELAY_B, OTHER_RELAY}  # write relays
        self.active_relays = {
            url: FakeRelay(url) for url in (DM_RELAY_A, DM_RELAY_B, OTHER_RELAY)
        }
        self.dm_relay_cache = {}
        self._dm_relay_waiters = {}
        # P4/P5 state (mirrors NostrClient.__init__ so the ast-extracted
        # methods find their attributes instead of hitting __getattr__)
        self._dm_status_batches = {}
        self._dm_status_by_sub = {}
        self._dm_status_seq = 0
        self._dm_initiation_times = collections.deque()
        self.requests = []  # client-level request_once calls
        self.dm_service = _Svc(self)

    def _ensure_dm_service(self):
        pass

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        try:
            fn = _unbound_client(name)
        except StopIteration:
            raise AttributeError(name)
        bound = fn.__get__(self)
        object.__setattr__(self, name, bound)
        return bound

    def emit(self, *a):
        self.emitted.append(a)

    def _merge_relays(self, urls):
        pass

    def subscribe(self, sub_id, filters, snapshot=False):
        self.subscribed.append((sub_id, filters))

    def publish(self, event):
        self.published.append(event)

    def _track_publish(self, event, label):
        self.tracked.append((event["id"], label))

    def request_once(self, sub_id, filters):
        self.requests.append((sub_id, filters))


class _Svc:
    """Real NIP-44 encrypt path (gi mocks make the real DMService a Mock)."""

    def __init__(self, client):
        self._client = client

    def encrypt(self, plaintext, peer_pubkey):
        import gnostr.nip44 as nip44

        conv = nip44.get_conversation_key(
            bytes.fromhex(self._client.my_privkey), bytes.fromhex(peer_pubkey)
        )
        return nip44.encrypt(plaintext, conv)


@pytest.fixture
def client():
    return FakeClient()


def _dmrelays_event(peer, urls, eid="f" * 64):
    """A peer's kind-10050 DM-relay list as it arrives from a relay."""
    return {
        "id": eid,
        "pubkey": peer,
        "kind": 10050,
        "created_at": int(time.time()),
        "content": "",
        "tags": [["r", u] for u in urls],
        "sig": "0" * 128,
    }


def _relay_requests(client):
    """kind-10050 REQs the client fired (one request_once call = one REQ
    frame, which the real client fans out to every connected relay)."""
    return [f for (_, f) in client.requests if f.get("kinds") == [10050]]


def _relay_publishes(client):
    """(url, event) pairs actually sent to a relay."""
    return [(url, ev) for url, r in client.active_relays.items() for ev in r.published]


# ---------------------------------------------------------------------------
# 1. No-10050 peer -> toast + abort, ZERO publish calls
# ---------------------------------------------------------------------------


def test_no_10050_peer_aborts_with_zero_publishes(client):
    signed = client.publish_dm(PEER, "hello")
    assert signed is not None  # optimistic bubble is keyed by this id

    # The lookup is open, nothing published yet — Send never blocks.
    assert _relay_publishes(client) == []
    assert PEER in client._dm_relay_waiters

    # No relay answers the kind-10050 REQ -> the timeout tick is the verdict.
    client._dm_relay_timeout(PEER)

    assert ("dm-unreachable", PEER, signed["id"]) in client.emitted
    # THE hard assertion of spec test-plan item 4: no publish was attempted.
    assert _relay_publishes(client) == []
    assert client.published == []
    assert client.tracked == []
    # A negative lookup is NOT cached: a peer who later sets a 10050 must
    # become reachable without waiting out a TTL.
    assert PEER not in client.dm_relay_cache


def test_unreachable_toast_text_goes_through_add_toast():
    """MainWindow surfaces dm-unreachable via add_toast() with the exact spec
    string — never a modal dialog."""
    src = open(_MAIN_PATH, encoding="utf-8").read()
    cls = next(
        n
        for n in ast.parse(src).body
        if isinstance(n, ast.ClassDef) and n.name == "MainWindow"
    )
    fn = next(
        n
        for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "on_dm_unreachable"
    )
    ns = {
        "Adw": types.SimpleNamespace(
            Toast=lambda title: types.SimpleNamespace(title=title)
        )
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), _MAIN_PATH, "exec"), ns)

    win = MagicMock()
    ns["on_dm_unreachable"](win, MagicMock(), PEER, "e" * 64)
    win.add_toast.assert_called_once()
    toast = win.add_toast.call_args[0][0]
    assert toast.title == "Recipient is not reachable for DMs"
    # spec: toast, not dialog — MainWindow.add_toast is the only route
    win.add_toast.assert_called_once_with(toast)
    assert not win.present.called


def test_unreachable_fails_the_optimistic_bubble():
    """The thread view turns dm-unreachable into the standard failed row
    (inline Retry), same as any rejected publish."""
    src = open(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..",
            "src",
            "gnostr",
            "ui",
            "dm_thread_view.py",
        ),
        encoding="utf-8",
    ).read()
    cls = next(
        n
        for n in ast.parse(src).body
        if isinstance(n, ast.ClassDef) and n.name == "DMThreadView"
    )
    fn = next(
        n
        for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "_on_dm_unreachable"
    )
    ns = {"Gtk": MagicMock(), "Adw": MagicMock()}
    exec(
        compile(ast.Module(body=[fn], type_ignores=[]), "dm_thread_view.py", "exec"),
        ns,
    )

    eid = "e" * 64
    row = MagicMock()
    msg = {"id": eid, "sender_pk": "a" * 64, "content": "hi", "rumor_ts": 1}
    view = MagicMock()
    view.peer_pubkey = PEER
    view._pending = {eid: {"row": row, "msg": msg}}
    view._seen_ids = {eid}

    ns["_on_dm_unreachable"](view, MagicMock(), PEER, eid)
    view.bubble_box.remove.assert_called_once_with(row)
    assert eid not in view._seen_ids
    view._show_failed.assert_called_once_with(msg)

    # A different peer's unreachable must not touch this thread
    view._pending[eid] = {"row": row, "msg": msg}
    ns["_on_dm_unreachable"](view, MagicMock(), PEER2, eid)
    assert eid in view._pending


# ---------------------------------------------------------------------------
# 2. Peer WITH 10050 -> published ONLY to those relays
# ---------------------------------------------------------------------------


def test_publish_targets_only_the_peers_10050_relays(client):
    signed = client.publish_dm(PEER, "targeted hello")

    # The peer's kind-10050 arrives from a relay and settles the lookup.
    client._handle_event(_dmrelays_event(PEER, [DM_RELAY_A, DM_RELAY_B]))

    pairs = _relay_publishes(client)
    urls = {url for url, _ in pairs}
    assert urls == {DM_RELAY_A, DM_RELAY_B}
    assert OTHER_RELAY not in urls  # NOT flooded to every connected relay
    assert [ev for _, ev in pairs][0]["id"] == signed["id"]
    # OK-ack tracking still runs so the bubble resolves
    assert (signed["id"], "DM") in client.tracked


def test_targeting_survives_10050_with_unconnected_relay(client):
    """A 10050 naming a relay we are not connected to: publish only to the
    intersection, never to the feed-only relays."""
    client.publish_dm(PEER, "hi")
    client._handle_event(_dmrelays_event(PEER, [DM_RELAY_A, "wss://not-connected"]))
    urls = {url for url, _ in _relay_publishes(client)}
    assert urls == {DM_RELAY_A}


def test_empty_10050_list_counts_as_unreachable(client):
    """A 10050 with no usable r tags is the same as none: abort, toast."""
    signed = client.publish_dm(PEER, "hi")
    client._handle_event(_dmrelays_event(PEER, []))
    assert ("dm-unreachable", PEER, signed["id"]) in client.emitted
    assert _relay_publishes(client) == []


# ---------------------------------------------------------------------------
# 3. TTL cache: second send does not re-fetch
# ---------------------------------------------------------------------------


def test_second_send_to_same_peer_uses_cache_no_refetch(client):
    client.publish_dm(PEER, "one")
    client._handle_event(_dmrelays_event(PEER, [DM_RELAY_A]))
    assert len(_relay_requests(client)) == 1

    client.publish_dm(PEER, "two")
    # No new kind-10050 REQ — the TTL cache answered it.
    assert len(_relay_requests(client)) == 1
    # And it still published, to the cached relay set only.
    urls = {url for url, _ in _relay_publishes(client)}
    assert urls == {DM_RELAY_A}
    assert len(client.active_relays[DM_RELAY_A].published) == 2


def test_expired_ttl_refetches(client):
    src_ts = time.time() - 10 * 3600  # far past DM_RELAY_TTL
    client.dm_relay_cache[PEER] = (src_ts, [DM_RELAY_A])
    client.publish_dm(PEER, "stale")
    assert len(_relay_requests(client)) == 1  # re-fetched


def test_concurrent_sends_share_one_lookup(client):
    """Two sends to the same peer while the lookup is open join one waiter
    list — one REQ, both delivered when it resolves."""
    s1 = client.publish_dm(PEER, "one")
    s2 = client.publish_dm(PEER, "two")
    assert len(_relay_requests(client)) == 1
    client._handle_event(_dmrelays_event(PEER, [DM_RELAY_A]))
    ids = {ev["id"] for _, ev in _relay_publishes(client)}
    assert ids == {s1["id"], s2["id"]}


# ---------------------------------------------------------------------------
# 4. Own kind-10050 default derived from write relays
# ---------------------------------------------------------------------------


def test_own_10050_defaults_to_two_write_relays(client):
    urls = client.dm_relay_defaults()
    assert len(urls) == 2
    assert set(urls) <= client.relay_urls  # derived from the user's relays


def test_publish_dm_relay_list_publishes_kind_10050(client):
    assert client.publish_dm_relay_list() is True
    ev = client.published[-1]
    assert ev["kind"] == 10050
    assert ev["pubkey"] == client.my_pubkey
    assert {t[1] for t in ev["tags"] if t[0] == "r"} == set(client.dm_relay_defaults())
    assert (ev["id"], "DM Relays") in client.tracked


def test_ensure_dm_maintains_own_10050(client):
    """Login hook publishes our 10050 (spec step 4) alongside the DM sub."""
    client.ensure_dm()
    subs = dict(client.subscribed)
    assert "dm_sub" in subs
    kinds = [ev["kind"] for ev in client.published]
    assert 10050 in kinds


def test_publish_dm_relay_list_without_keys_is_noop(client):
    client.my_privkey = None
    assert client.publish_dm_relay_list() is False
    assert client.published == []
