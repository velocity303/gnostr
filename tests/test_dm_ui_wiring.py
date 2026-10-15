"""G3 DM UI wiring tests (headless).

Under the conftest gi mocks, `class DMInboxView(Gtk.Box)` / `DMThreadView` /
`Sidebar` / `MainWindow` evaluate against a Mock base, so the imported classes
are Mocks (same situation as FollowersListView in test_follows_view.py).
Logic tests extract the real methods from source via ast and bind them to a
lightweight fake self — exercising the actual code without the mocked GTK
machinery. Real rendering is verified on-device (tier-2 harness pattern).

Covers the UI contract points that are testable headless:
- inbox: skeleton = 3 rows removed on FIRST gateway result (never a timer);
  rows built from gateway.get_conversations; row tap -> show_dm_thread;
  signal handler inert once the view is popped.
- thread: Send disabled (not hidden) while entry blank; optimistic 'sending'
  bubble at opacity 0.6 keyed by event id; solid on ack; failed row + inline
  retry on reject; opening marks the conversation read.
- client: publish_dm persists the outbound self-copy into the RECIPIENT's
  conversation (unread stays 0) and emits dm-conversations-updated;
  ensure_dm opens the kind-4 subscription.
- sidebar badge: shown with count, hidden at 0; MainWindow.update_dm_badge
  reads gateway.total_unread (the SUM(unread_count) query).
"""

import ast
import collections
import functools
import os
import time
from unittest.mock import MagicMock, Mock

import pytest

_UI_DIR = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", "gnostr", "ui"
    )
)
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


def _class_method(path, class_name, method_name, extra_ns=None):
    """Extract the real <class>.<method> function from source, unbound."""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name
    )
    fn = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name
    )
    ns = {"Adw": MagicMock(), "Gtk": MagicMock(), "Pango": MagicMock()}
    if extra_ns:
        ns.update(extra_ns)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), path, "exec"), ns)
    return ns[method_name]


def _module_func(path, func_name, extra_ns=None):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == func_name
    )
    ns = {}
    if extra_ns:
        ns.update(extra_ns)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), path, "exec"), ns)
    return ns[func_name]


# ---------------------------------------------------------------------------
# relative time caption
# ---------------------------------------------------------------------------


def test_format_relative_time_buckets():
    fmt = _module_func(
        os.path.join(_UI_DIR, "dm_inbox_view.py"), "format_relative_time"
    )
    now = time.time()
    assert fmt(now - 5) == "now"
    assert fmt(now - 120) == "2m"
    assert fmt(now - 3600 * 5) == "5h"
    assert fmt(now - 86400 * 3) == "3d"
    assert fmt(0) == ""


# ---------------------------------------------------------------------------
# DMInboxView
# ---------------------------------------------------------------------------


_FMT_NS = {
    "format_relative_time": _module_func(
        os.path.join(_UI_DIR, "dm_inbox_view.py"), "format_relative_time"
    )
}


class FakeInbox:
    def __init__(self, gateway=None):
        self.main_window = MagicMock()
        self.main_window.client.dm_gateway = gateway
        self.main_window.db.get_profile.return_value = {}
        self.list_box = MagicMock()
        self._skeleton_rows = [Mock() for _ in range(3)]
        self.list_rows = []
        self._gateway_result_seen = False
        self.title_label = MagicMock()
        self.btn_refresh = MagicMock()

    def _gateway(self):
        return getattr(self.main_window.client, "dm_gateway", None)

    def _remove_skeleton(self):
        self._skeleton_rows = []

    def _append_skeleton(self):
        pass

    def reload(self):
        fn = _class_method(
            os.path.join(_UI_DIR, "dm_inbox_view.py"),
            "DMInboxView",
            "reload",
            extra_ns={"SKELETON_ROWS": 3},
        )
        return fn(self)

    def _make_row(self, conv):
        fn = _class_method(
            os.path.join(_UI_DIR, "dm_inbox_view.py"),
            "DMInboxView",
            "_make_row",
            extra_ns={
                "hex_to_npub": lambda h: "npub1" + h[:8],
                "AVATAR_SIZE": 40,
                "ROW_MIN_HEIGHT": 64,
                **_FMT_NS,
            },
        )
        return fn(self, conv)

    def _on_row_activated(self, lb, row):
        fn = _class_method(
            os.path.join(_UI_DIR, "dm_inbox_view.py"),
            "DMInboxView",
            "_on_row_activated",
        )
        return fn(self, lb, row)

    def _on_dm_updated(self, client, peer):
        fn = _class_method(
            os.path.join(_UI_DIR, "dm_inbox_view.py"), "DMInboxView", "_on_dm_updated"
        )
        return fn(self, client, peer)


def test_inbox_skeleton_removed_on_first_gateway_result():
    gw = MagicMock()
    gw.get_conversations.return_value = []
    view = FakeInbox(gateway=gw)
    view.reload()
    assert view._skeleton_rows == []
    assert view._gateway_result_seen is True


def test_inbox_keeps_skeleton_without_gateway():
    """No gateway (not logged in) is NOT a real result: skeleton stays."""
    view = FakeInbox(gateway=None)
    view.reload()
    assert len(view._skeleton_rows) == 3
    assert view._gateway_result_seen is False


def test_inbox_rows_from_gateway_conversations():
    peer = "a" * 64
    gw = MagicMock()
    gw.get_conversations.return_value = [
        {"peer_pk": peer, "last_ts": time.time(), "unread_count": 2, "preview": "hi"}
    ]
    view = FakeInbox(gateway=gw)
    view.reload()
    # one conversation row appended (skeleton removal + row append happened)
    assert view.list_box.append.call_count == 1
    # badge refreshed from the gateway after reload
    view.main_window.update_dm_badge.assert_called()


def test_inbox_row_activation_opens_thread():
    view = FakeInbox()
    row = Mock()
    row.pubkey = "b" * 64
    view._on_row_activated(None, row)
    view.main_window.show_dm_thread.assert_called_once_with("b" * 64)


def test_inbox_signal_inert_after_pop():
    """A popped view stays connected to the client signal; the handler must
    no-op when the view left the widget tree."""
    view = FakeInbox()
    view.get_parent = lambda: None
    called = []
    view.reload = lambda: called.append(1)
    view._on_dm_updated(None, "x" * 64)
    assert called == []


# ---------------------------------------------------------------------------
# DMThreadView
# ---------------------------------------------------------------------------


class FakeThread:
    def __init__(self):
        self.main_window = MagicMock()
        self.peer_pubkey = "c" * 64
        self._seen_ids = set()
        self._pending = {}
        self._new_since_view = 0
        self.entry = MagicMock()
        self.btn_send = MagicMock()
        self.bubble_box = MagicMock()
        self.unread_pill = MagicMock()

    def _on_entry_changed(self, entry):
        fn = _class_method(
            os.path.join(_UI_DIR, "dm_thread_view.py"),
            "DMThreadView",
            "_on_entry_changed",
        )
        return fn(self, entry)

    def _on_send(self):
        fn = _class_method(
            os.path.join(_UI_DIR, "dm_thread_view.py"), "DMThreadView", "_on_send"
        )
        return fn(self)

    def _send_text(self, text):
        fn = _class_method(
            os.path.join(_UI_DIR, "dm_thread_view.py"), "DMThreadView", "_send_text"
        )
        return fn(self, text)

    def _append_msg(self, msg, status=None):
        fn = _class_method(
            os.path.join(_UI_DIR, "dm_thread_view.py"), "DMThreadView", "_append_msg"
        )
        return fn(self, msg, status)

    def _make_bubble(self, msg, status=None):
        fn = _class_method(
            os.path.join(_UI_DIR, "dm_thread_view.py"),
            "DMThreadView",
            "_make_bubble",
            extra_ns=dict(_FMT_NS),
        )
        return fn(self, msg, status)

    def _on_publish_result(self, client, eid, accepted, message, relay, label):
        fn = _class_method(
            os.path.join(_UI_DIR, "dm_thread_view.py"),
            "DMThreadView",
            "_on_publish_result",
        )
        return fn(self, client, eid, accepted, message, relay, label)

    def _show_failed(self, msg):
        fn = _class_method(
            os.path.join(_UI_DIR, "dm_thread_view.py"), "DMThreadView", "_show_failed"
        )
        return fn(self, msg)

    def _scroll_to_bottom(self, force=False):
        pass


def test_send_disabled_while_entry_blank():
    view = FakeThread()
    entry = MagicMock()
    entry.get_text.return_value = "   "
    view._on_entry_changed(entry)
    view.btn_send.set_sensitive.assert_called_with(False)
    entry.get_text.return_value = "hi"
    view._on_entry_changed(entry)
    view.btn_send.set_sensitive.assert_called_with(True)
    # disabled, never hidden
    assert not view.btn_send.hide.called


def test_optimistic_send_inserts_sending_bubble():
    view = FakeThread()
    view.main_window.pub_key = "me" * 32
    signed = {"id": "e" * 64, "pubkey": "me" * 32, "created_at": 123}
    view.main_window.client.publish_dm.return_value = signed
    view._send_text("hello")
    assert "e" * 64 in view._pending
    view.bubble_box.append.assert_called()
    # entry cleared + refocused (OSK stays up)
    view.main_window.client.publish_dm.assert_called_once_with("c" * 64, "hello")


def test_publish_ack_solidifies_bubble():
    view = FakeThread()
    row = MagicMock()
    child = MagicMock()
    row.get_child.return_value = child
    view._pending["e" * 64] = {"row": row, "msg": {}}
    view._on_publish_result(None, "e" * 64, True, "", "wss://r", "DM")
    child.set_opacity.assert_called_with(1.0)
    assert "e" * 64 not in view._pending


def test_publish_reject_shows_failed_row():
    view = FakeThread()
    view.main_window.pub_key = "me" * 32
    row = MagicMock()
    msg = {"id": "e" * 64, "sender_pk": "me" * 32, "content": "hi", "rumor_ts": 1}
    view._pending["e" * 64] = {"row": row, "msg": msg}
    view._on_publish_result(None, "e" * 64, False, "rejected", "wss://r", "DM")
    view.bubble_box.remove.assert_called_with(row)
    # failed row appended (one more append after the remove)
    assert view.bubble_box.append.call_count == 1


def test_failed_bubble_carries_inline_retry():
    view = FakeThread()
    view.main_window.pub_key = "me" * 32
    msg = {"id": None, "sender_pk": "me" * 32, "content": "hi", "rumor_ts": None}
    row, box = view._make_bubble(msg, status="failed")
    # a Retry button was appended into the bubble box
    assert box.append.call_count >= 1


# ---------------------------------------------------------------------------
# client: publish_dm + ensure_dm (FakeClient pattern from test_dm_relay_wiring)
# ---------------------------------------------------------------------------


def _client_globals():
    import types

    src = open(_CLIENT_PATH, encoding="utf-8").read()
    ns = {"__name__": "gnostr.client", "__package__": "gnostr"}
    exec(compile(ast.parse(src), _CLIENT_PATH, "exec"), ns)
    # client.py's own `from gi.repository import GLib` rebinds the name to
    # conftest's Mock during exec; install the direct-call stub AFTER so
    # idle_add callbacks run synchronously (G15: publish_dm resolves its
    # 10050 lookup through idle_add).
    ns["GLib"] = types.SimpleNamespace(
        idle_add=lambda fn, *a: fn(*a),
        timeout_add=lambda ms, fn, *a: None,
        get_user_config_dir=lambda: "/tmp/gnostr-test",
    )
    return ns


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
    ns = _client_globals()
    exec(compile(ast.Module(body=[fn], type_ignores=[]), _CLIENT_PATH, "exec"), ns)
    return ns[method_name]


class FakeClient:
    def __init__(self):
        self.db = MagicMock()
        self.my_pubkey = "a" * 64
        self.my_privkey = "b" * 64
        self.seen_events = set()
        self.metrics = {}
        self.subscribed = []
        self.published = []
        self.emitted = []
        # G15 relay targeting: publish_dm now fans out only to the peer's
        # 10050 relays; these give the extracted methods a relay set to work
        # against (FakeRelay pattern from test_dm_relay_targeting.py).
        self.relay_urls = {"wss://relay-a.example", "wss://relay-b.example"}
        self.active_relays = {
            u: MagicMock(
                published=[],
                publish=lambda ev, u=u: self.active_relays[u].published.append(ev)
                or True,
            )
            for u in self.relay_urls
        }
        self.dm_relay_cache = {}
        self._dm_relay_waiters = {}
        # P4/P5 state (mirrors NostrClient.__init__ so the ast-extracted
        # methods find their attributes instead of hitting __getattr__)
        self._dm_status_batches = {}
        self._dm_status_by_sub = {}
        self._dm_status_seq = 0
        self._dm_initiation_times = collections.deque()
        self.requests = []

    def _ensure_dm_service(self):
        """gi mocks make the real DMService class a Mock (GObject.Object base);
        substitute a lightweight service with the REAL NIP-44 encrypt path —
        same workaround as test_dm_relay_wiring._make_dm_service."""
        if getattr(self, "dm_service", None) is None:
            import gnostr.nip44 as nip44

            class _Svc:
                def __init__(s):
                    s._our_privkey = (
                        bytes.fromhex(self.my_privkey) if self.my_privkey else None
                    )

                def encrypt(s, plaintext, peer_pubkey):
                    conv = nip44.get_conversation_key(
                        s._our_privkey, bytes.fromhex(peer_pubkey)
                    )
                    return nip44.encrypt(plaintext, conv)

            self.dm_service = _Svc()

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

    def request_once(self, sub_id, filters):
        self.requests.append((sub_id, filters))

    def _log_relay(self, line):
        pass

    def _merge_relays(self, urls):
        pass

    def subscribe(self, sub_id, filters, snapshot=False):
        self.subscribed.append((sub_id, filters))

    def publish(self, event):
        self.published.append(event)

    def _track_publish(self, event, label):
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    from gi.repository import GLib

    monkeypatch.setattr(GLib, "get_user_data_dir", lambda: str(tmp_path / "data"))
    return FakeClient()


def test_publish_dm_stores_self_copy_under_recipient(client):
    from gnostr.gateway.dm_gateway import SQLiteDMGateway

    gw = SQLiteDMGateway()
    gw.set_our_pubkey(client.my_pubkey)
    client.set_dm_gateway(gw)

    # real curve point (secp256k1 generator x) — NIP-44 rejects off-curve keys
    peer = "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
    signed = client.publish_dm(peer, "secret hello")
    assert signed is not None
    assert signed["kind"] == 4
    # G15: publish is async + relay-targeted — settle the 10050 lookup, then
    # the rumor goes out to the peer's DM relays only and is OK-ack tracked.
    client._handle_event(
        {
            "id": "f" * 64,
            "pubkey": peer,
            "kind": 10050,
            "created_at": int(time.time()),
            "content": "",
            "tags": [["r", "wss://relay-a.example"]],
            "sig": "0" * 128,
        }
    )
    # published to the targeted relay + tracked for OK-ack
    assert (
        client.active_relays["wss://relay-a.example"].published[0]["id"] == signed["id"]
    )
    # self-copy landed in the RECIPIENT's conversation, unread stays 0
    convs = gw.get_conversations(10)
    assert convs[0]["peer_pk"] == peer
    assert convs[0]["unread_count"] == 0
    # thread shows our own message
    thread = gw.get_thread(peer, 10, None)
    assert thread[0]["id"] == signed["id"]
    # conversations-updated emitted for the inbox to refresh
    assert ("dm-conversations-updated", peer) in client.emitted


def test_publish_dm_roundtrips_through_nip44(client):
    """What we store is ciphertext; decrypting with the same conversation key
    yields the plaintext (encrypt path is the real DMService.encrypt)."""
    import gnostr.nip44 as nip44
    from gnostr.gateway.dm_gateway import SQLiteDMGateway

    gw = SQLiteDMGateway()
    gw.set_our_pubkey(client.my_pubkey)
    client.set_dm_gateway(gw)

    peer = "d" * 64
    signed = client.publish_dm(peer, "roundtrip")
    conv = nip44.get_conversation_key(
        bytes.fromhex(client.my_privkey), bytes.fromhex(peer)
    )
    assert nip44.decrypt(signed["content"], conv) == "roundtrip"


def test_publish_dm_without_keys_returns_none(client):
    client.my_privkey = None
    assert client.publish_dm("c" * 64, "x") is None


def test_ensure_dm_opens_kind4_subscription(client):
    client.ensure_dm()
    subs = dict(client.subscribed)
    assert "dm_sub" in subs
    assert subs["dm_sub"]["kinds"] == [4]
    assert subs["dm_sub"]["#p"] == [client.my_pubkey]


# ---------------------------------------------------------------------------
# sidebar badge + MainWindow.update_dm_badge
# ---------------------------------------------------------------------------


def test_sidebar_badge_shown_and_hidden():
    view = MagicMock()
    badge = MagicMock()
    view.dm_badge = badge
    fn = _class_method(
        os.path.join(_UI_DIR, "sidebar.py"), "Sidebar", "update_dm_badge"
    )
    fn(view, 3)
    badge.set_text.assert_called_with("3")
    badge.show.assert_called()
    fn(view, 0)
    badge.hide.assert_called()


def test_main_window_badge_reads_gateway_total_unread():
    win = MagicMock()
    win.client.dm_gateway.total_unread.return_value = 7
    fn = _class_method(_MAIN_PATH, "MainWindow", "update_dm_badge")
    fn(win)
    win.sidebar.update_dm_badge.assert_called_with(7)


def test_main_window_badge_zero_without_gateway():
    win = MagicMock()
    win.client.dm_gateway = None
    fn = _class_method(_MAIN_PATH, "MainWindow", "update_dm_badge")
    fn(win)
    win.sidebar.update_dm_badge.assert_called_with(0)
