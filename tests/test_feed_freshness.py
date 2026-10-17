"""Startup feed-freshness (spec Phase B, docs/specs/dm-initiation-and-feed-freshness.md).

Headless coverage of the three load-bearing contracts:

- B1/B3: MainWindow._pull_feed_freshness — exactly ONE bounded REQ per
  startup ({kinds:[1], authors:contacts[:300], since, limit:50}, unique
  sub_id fresh_following_<ts>), the since-window computation (60 s
  clock-skew overlap + 7 d floor), and the contacts-arrival re-arm.
- B2a: NostrRelay.request_once registers its sub_id in snapshot_ids so the
  EOSE handler auto-CLOSEs it (drives the REAL on_msg closure from start()).
- B2b: on_event_received routes inserts to buffered_new (pill) while the
  freshness hold is active, never flush_new (no viewport jump).
- Regression (spec Evidence): FeedModel freshness-page merge + dedup on
  relay echo-back — the exact expected sequence from the spec.

Under the conftest gi mocks, MainWindow/NostrRelay evaluate against a Mock
base, so logic tests extract the real methods from source via ast and bind
them to a lightweight fake self (house pattern: test_follow_sync.py,
test_dm_ui_wiring.py). FeedModel is GTK-free and imported for real.
"""

import ast
import json
import os
import time
import types
from unittest.mock import MagicMock

import pytest

from gnostr.service.feed_model import FeedModel

_HERE = os.path.dirname(os.path.abspath(__file__))
_MAIN_PATH = os.path.normpath(os.path.join(_HERE, "..", "src", "gnostr", "main.py"))
_CLIENT_PATH = os.path.normpath(os.path.join(_HERE, "..", "src", "gnostr", "client.py"))


def _extract(path, target, method_name, extra_ns=None):
    """Extract a real function from source (class method or module-level
    function) without importing the module under the mocked GObject."""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == target:
            fn = next(
                (
                    n
                    for n in node.body
                    if isinstance(n, ast.FunctionDef) and n.name == method_name
                ),
                None,
            )
            break
        if isinstance(node, ast.FunctionDef) and node.name == method_name:
            fn = node
            break
    if fn is None:
        raise LookupError(f"{target}.{method_name} not found in {path}")
    ns = {
        "json": json,
        "time": time,
        "gnostr": __import__("gnostr"),
        "GLib": types.SimpleNamespace(idle_add=lambda fn, *a: fn(*a)),
    }
    if extra_ns:
        ns.update(extra_ns)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), path, "exec"), ns)
    return ns[method_name]


# ---------------------------------------------------------------------------
# Fake harnesses
# ---------------------------------------------------------------------------


class FakeFeedModel:
    """Stands in for FeedModel in the MainWindow tests (only the attributes
    _pull_feed_freshness / on_event_received touch)."""

    def __init__(self, top_key=None, at_top=True):
        self.top_key = top_key
        self.at_top = at_top
        self.new_ids = []

    def prepend_new(self, rows):
        inserted = [r["id"] for r in rows if r["id"] not in self.new_ids]
        self.new_ids.extend(inserted)
        return inserted


class FakeWindow:
    """Minimal MainWindow self for the extracted freshness methods."""

    def __init__(self, contacts=("b" * 64,), top_key=None):
        self.pub_key = "a" * 64
        self.db = MagicMock()
        self.db.get_following_list.return_value = list(contacts)
        self.client = MagicMock()
        self.feed_view = types.SimpleNamespace(
            feed_model=FakeFeedModel(top_key=top_key),
            buffered_new=MagicMock(),
            flush_new=MagicMock(),
        )
        self.content_nav = MagicMock()
        self._freshness_hold = 0.0
        self._freshness_pulled = False

    # extracted methods land here via __getattr__ below


def _bind(win, method_name, extra_ns=None):
    fn = _extract(_MAIN_PATH, "MainWindow", method_name, extra_ns)
    setattr(win, method_name, fn.__get__(win))
    return getattr(win, method_name)


# ---------------------------------------------------------------------------
# B1 — the bounded freshness REQ (window, filter shape, one-per-startup)
# ---------------------------------------------------------------------------


def _pull(win, now=None):
    clock = {"t": now if now is not None else 1_760_000_000}
    fake_time = types.SimpleNamespace(time=lambda: clock["t"], monotonic=time.monotonic)
    fn = _extract(_MAIN_PATH, "MainWindow", "_pull_feed_freshness", {"time": fake_time})
    bound = fn.__get__(win)
    object.__setattr__(win, "_pull_feed_freshness", bound)
    bound()
    return clock


def test_freshness_pull_sends_one_bounded_req():
    win = FakeWindow(contacts=tuple(f"{i:064x}" for i in range(1, 311)))
    _pull(win, now=1_760_000_000)
    assert win.client.request_once.call_count == 1
    sub_id, filters = win.client.request_once.call_args[0]
    assert sub_id.startswith("fresh_following_")
    assert filters["kinds"] == [1]
    assert filters["limit"] == 50
    assert filters["authors"] == [f"{i:064x}" for i in range(1, 311)][:300]
    # no cached window -> 7 d floor
    assert filters["since"] == 1_760_000_000 - 7 * 86400
    # pill-hold armed for ~5 s
    assert win._freshness_hold > time.monotonic()


def test_freshness_pull_exactly_once_per_startup():
    win = FakeWindow()
    _pull(win)
    _pull(win)  # second call (e.g. re-CONNECTED) must be a no-op
    assert win.client.request_once.call_count == 1


def test_freshness_since_uses_overlap_below_newest_cached():
    # newest cached post 1 h ago: since = newest - 60 (clock-skew overlap)
    newest = 1_760_000_000 - 3600
    win = FakeWindow(top_key=(newest, "x" * 64))
    _pull(win, now=1_760_000_000)
    _, filters = win.client.request_once.call_args[0]
    assert filters["since"] == newest - 60


def test_freshness_since_floors_at_7_days():
    # stale cache (30 d old): the 7 d floor bounds the window
    newest = 1_760_000_000 - 30 * 86400
    win = FakeWindow(top_key=(newest, "x" * 64))
    _pull(win, now=1_760_000_000)
    _, filters = win.client.request_once.call_args[0]
    assert filters["since"] == 1_760_000_000 - 7 * 86400


def test_freshness_since_future_clock_skew_overlap():
    # relay clock ahead: newest cached is 10 s in OUR future — the 60 s
    # overlap still reaches below it (since = newest - 60, not clamped to now)
    newest = 1_760_000_000 + 10
    win = FakeWindow(top_key=(newest, "x" * 64))
    _pull(win, now=1_760_000_000)
    _, filters = win.client.request_once.call_args[0]
    assert filters["since"] == newest - 60
    assert filters["since"] < 1_760_000_000


# ---------------------------------------------------------------------------
# B3 — contacts-arrival re-arm (first-ever login)
# ---------------------------------------------------------------------------


def test_contacts_arrival_rearms_pull_once():
    win = FakeWindow(contacts=())  # empty following at CONNECTED time
    on_contacts = _bind(win, "on_contacts_updated")
    _bind(win, "_pull_feed_freshness")
    win.client.request_once.assert_not_called()  # B1 deferred (no contacts)

    # own kind-3 arrives and fills the table -> re-arm fires the pull
    win.db.get_following_list.return_value = ["b" * 64]
    on_contacts("own", win.pub_key)
    assert win.client.request_once.call_count == 1

    # a later kind-3 must NOT fire a second REQ (flag-guarded)
    on_contacts("own", win.pub_key)
    assert win.client.request_once.call_count == 1


def test_contacts_arrival_ignores_foreign_owner():
    win = FakeWindow(contacts=())
    on_contacts = _bind(win, "on_contacts_updated")
    _bind(win, "_pull_feed_freshness")
    on_contacts("own", "f" * 64)  # foreign kind-3
    win.client.request_once.assert_not_called()


# ---------------------------------------------------------------------------
# B2b — freshness hold routes to the pill, never an inline flush
# ---------------------------------------------------------------------------


def _deliver(win, eid="e" * 64):
    fn = _extract(_MAIN_PATH, "MainWindow", "on_event_received")
    win.content_nav.get_visible_page.return_value = win.feed_view
    win.db.get_event_by_id.return_value = {"id": eid, "created_at": 1}
    fn(win, None, eid, "pk", "content", "[]")


def test_hold_routes_to_pill_not_flush():
    win = FakeWindow()
    win.feed_view.feed_model.at_top = True  # the viewport-jump condition
    win._freshness_hold = time.monotonic() + 5.0
    _deliver(win)
    win.feed_view.buffered_new.assert_called_once()
    win.feed_view.flush_new.assert_not_called()


def test_no_hold_at_top_flushes_inline():
    win = FakeWindow()
    win.feed_view.feed_model.at_top = True
    win._freshness_hold = 0.0
    _deliver(win)
    win.feed_view.flush_new.assert_called_once()
    win.feed_view.buffered_new.assert_not_called()


def test_expired_hold_flushes_inline():
    win = FakeWindow()
    win.feed_view.feed_model.at_top = True
    win._freshness_hold = time.monotonic() - 1.0  # hold already elapsed
    _deliver(win)
    win.feed_view.flush_new.assert_called_once()


# ---------------------------------------------------------------------------
# B2a — request_once registers its sub_id for EOSE auto-close
# ---------------------------------------------------------------------------


class _FakeWS:
    def __init__(
        self, url, on_open=None, on_message=None, on_error=None, on_close=None
    ):
        self.sent = []
        self._cb = {"open": on_open, "message": on_message, "close": on_close}

    def send(self, data):
        self.sent.append(json.loads(data))

    def run_forever(self):
        self._cb["open"](self)

    def fire_eose(self, sub_id):
        self._cb["message"](self, json.dumps(["EOSE", sub_id]))


def _fake_relay():
    """Real NostrRelay.start()/request_once()/process_queue() bound to a fake
    self, with websocket + GLib stubbed (no thread actually waits)."""
    relay = types.SimpleNamespace(
        url="wss://relay.test",
        on_event=lambda ev: None,
        on_status=lambda *a: None,
        on_ok=lambda *a: None,
        on_eose=None,
        ws=None,
        is_connected=False,
        sub_id=None,
        active_sub_ids=set(),
        request_queue=[],
        is_processing_queue=False,
        snapshot_ids=set(),
        # relay-economy fields (issue #26) the real __init__ sets
        queued_sub_ids=set(),
        _recent_reqs={},
        live_subs={},
        _idle_closed=False,
        _tokens=5.0,
        _last_refill=time.monotonic(),
        _backoff=2.0,
        _last_attempt=0.0,
        _last_traffic=time.monotonic(),
        _closed_notified=False,
    )
    ws_mod = types.SimpleNamespace(WebSocketApp=_FakeWS)
    conn_state = __import__(
        "gnostr.util.connection_state", fromlist=["ConnectionState"]
    ).ConnectionState
    for name in (
        "start",
        "request_once",
        "process_queue",
        "_enqueue",
        "_next_send_wait",
        "_refill_tokens",
    ):
        fn = _extract(
            _CLIENT_PATH,
            "NostrRelay",
            name,
            {
                "websocket": ws_mod,
                "threading": __import__("threading"),
                "ConnectionState": conn_state,
                "REQ_RATE_LIMIT": 18,
                "REQ_BURST": 5,
                "REQ_DEDUP_WINDOW": 2.0,
                "RECONNECT_BACKOFF_START": 2.0,
                "RECONNECT_BACKOFF_MAX": 60.0,
                "IDLE_RELAY_TIMEOUT": 300.0,
                "random": __import__("random"),
            },
        )
        setattr(relay, name, fn.__get__(relay))
    return relay


def test_request_once_registers_snapshot_id_and_eose_closes():
    relay = _fake_relay()
    relay.start()  # opens the fake WS, arms the real on_msg handler
    assert relay.is_connected
    relay.request_once("fresh_following_123", {"kinds": [1], "limit": 50})
    # B2a: the sub_id is registered for EOSE auto-close
    assert "fresh_following_123" in relay.snapshot_ids
    # the REQ actually went out
    reqs = [m for m in relay.ws.sent if m[0] == "REQ"]
    assert reqs and reqs[-1][1] == "fresh_following_123"
    # EOSE -> CLOSE + deregister (the existing snapshot handler)
    relay.ws.fire_eose("fresh_following_123")
    closes = [m for m in relay.ws.sent if m[0] == "CLOSE"]
    assert ["CLOSE", "fresh_following_123"] in closes
    assert "fresh_following_123" not in relay.snapshot_ids


def test_eose_does_not_close_non_snapshot_sub():
    relay = _fake_relay()
    relay.start()
    relay.request_once("live_sub", {"kinds": [1]})
    relay.snapshot_ids.discard("live_sub")  # simulate a live (non-snapshot) sub
    relay.ws.fire_eose("live_sub")
    assert ["CLOSE", "live_sub"] not in relay.ws.sent


# ---------------------------------------------------------------------------
# Regression (spec Evidence) — FeedModel freshness merge + echo-back dedup
# ---------------------------------------------------------------------------


class FakeFeedDB:
    """Database stand-in mirroring get_feed_following keyset semantics
    (created_at DESC, id DESC; before/after are (created_at, id) keys)."""

    def __init__(self, rows):
        self.rows = sorted(rows, key=lambda r: (-r["created_at"], r["id"]))

    def get_feed_following(self, owner, limit=50, before=None, after=None):
        out = []
        for r in self.rows:
            key = (r["created_at"], r["id"])
            if before is not None and key >= before:
                continue
            if after is not None and key <= after:
                continue
            out.append(r)
            if len(out) >= limit:
                break
        return out


def _row(eid, created_at):
    return {"id": eid, "created_at": created_at, "pubkey": "p", "content": eid}


def test_freshness_merge_sequence_matches_spec_evidence():
    """Headless merge per the spec B2b evidence: prepend_new dedups and
    inserts (created_at, id) DESC; relay echo-back dedups to [].

    stale cache window: ['old2','old1','old0'] at_top True
    freshness page buffered (arrives newest-first from the REQ): ['new0','new1']
    pill tap flush: ['new0','new1','old2','old1','old0']
    echo-back dedup (cached + flushed events): [] and []
    """
    cached = [
        _row("old0", 1759740800),
        _row("old1", 1759740801),
        _row("old2", 1759740802),
    ]
    fresh = [_row("new0", 1759999970), _row("new1", 1759999960)]
    db = FakeFeedDB(cached)
    m = FeedModel("owner", db, page_size=50, max_window=100)

    assert m.load_first() == ["old2", "old1", "old0"]
    assert m.at_top is True
    assert m.top_key == (1759740802, "old2")

    # freshness page arrives via prepend_new (the only Phase B entry door);
    # the buffer is sorted (created_at, id) DESC regardless of arrival order
    assert m.prepend_new(fresh) == ["new0", "new1"]
    assert m.new_ids == ["new0", "new1"]

    # pill tap flushes: buffer prepends ahead of the stale cache
    assert m.flush_new_at_top() == ["new0", "new1"]
    assert m.event_ids == ["new0", "new1", "old2", "old1", "old0"]
    assert m.at_top is True
    assert m.top_key == (1759999970, "new0")

    # relay echo-back dedups against BOTH window and buffer
    assert m.prepend_new([_row("old1", 1759740801)]) == []
    assert m.prepend_new([_row("new0", 1759999970)]) == []
    assert m.event_ids == ["new0", "new1", "old2", "old1", "old0"]


def test_freshness_gap_event_buffers_and_flushes():
    """The Phase B gap-fill case: a post newer than the stale cache top
    buffers during the hold and lands above the cache after the pill tap."""
    cached = [_row("c_hi", 2000), _row("c_lo", 1000)]
    db = FakeFeedDB(cached)
    m = FeedModel("owner", db, page_size=50, max_window=100)
    m.load_first()
    assert m.prepend_new([_row("gap", 2500)]) == ["gap"]
    m.flush_new_at_top()
    assert m.event_ids == ["gap", "c_hi", "c_lo"]
    assert m.top_key == (2500, "gap")
