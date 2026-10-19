"""Infinite-scroll phase 2: two-tier relay backfill.

When the DB tier is exhausted, the feed view fires ONE bounded history
REQ (`until` = window-tail created_at) per relay; events land in the DB
via the normal event-received path and 'backfill-done' (all relays EOSE'd)
re-triggers the page load. Courtesy rules: one in flight, min 2 s between
REQs (issue #26 ban-spiral guard). Backfill history must NOT ride the
live pill path (is_live gate) — it would bump the viewport mid-scroll.

Client/FeedView methods are ast-extracted from source and bound to fake
selves (house pattern: test_feed_freshness.py) — both classes are
GObject-bound and inert under the conftest gi mocks. FeedModel is
GTK-free and imported for real.
"""
import ast
import time as _time
import types
from unittest.mock import MagicMock, Mock

from gi.repository import GLib  # noqa: E402 - resolves to the mock in conftest

from gnostr.service.feed_model import FeedModel
from gnostr.service.feed_pagination import backfill_filter

_HERE = __import__("os").path.dirname(__import__("os").path.abspath(__file__))
_CLIENT_PATH = _HERE + "/../src/gnostr/client.py"
_VIEW_PATH = _HERE + "/../src/gnostr/ui/feed_view.py"


def _extract(path, method_name, extra_ns=None):
    """Module-level or NostrClient/FeedView method, compiled from source."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    fn = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            fn = next(
                (n for n in node.body if isinstance(n, ast.FunctionDef)
                 and n.name == method_name), None)
            if fn:
                break
        elif isinstance(node, ast.FunctionDef) and node.name == method_name:
            fn = node
            break
    assert fn is not None, f"{method_name} not found in {path}"
    ns = {
        "time": _time,
        "GLib": types.SimpleNamespace(idle_add=lambda fn, *a: fn(*a)),
    }
    if extra_ns:
        ns.update(extra_ns)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), path, "exec"), ns)
    return ns[method_name]


def _mk(ev_id, created_at, pubkey="pk"):
    return {"id": ev_id, "pubkey": pubkey, "created_at": created_at,
            "kind": 1, "content": "c", "tags": [], "sig": "s"}


# ------------------------------------------------------- backfill_filter


def test_backfill_filter_global_shape():
    f = backfill_filter("global", "me", (1000, "abc"), 50, None)
    assert f == {"kinds": [1], "until": 1000, "limit": 50}


def test_backfill_filter_following_authors_capped():
    db = Mock()
    db.get_following_list.return_value = [f"{i:064x}" for i in range(350)]
    f = backfill_filter("following", "me", (2000, "x"), 50, db)
    assert f["until"] == 2000 and f["limit"] == 50
    assert len(f["authors"]) == 300


def test_backfill_filter_following_without_contacts_is_none():
    db = Mock()
    db.get_following_list.return_value = []
    assert backfill_filter("following", "me", (1, "x"), 50, db) is None


def test_backfill_filter_me_and_no_cursor():
    assert backfill_filter("me", "pk_self", (5, "x"), 50, None) == {
        "kinds": [1], "until": 5, "limit": 50, "authors": ["pk_self"]}
    assert backfill_filter("global", "me", None, 50, None) is None


# ------------------------------------------------- client.request_backfill


class FakeClient:
    BACKFILL_MIN_INTERVAL = 2.0
    BACKFILL_TIMEOUT = 10.0

    def __init__(self, relays=("wss://a", "wss://b")):
        self.active_relays = {u: Mock() for u in relays}
        self._backfill_pending = {}
        self.emitted = []

    def request_once(self, sub_id, filters):
        for r in self.active_relays.values():
            r.request_once(sub_id, filters)

    def emit(self, sig, *args):
        self.emitted.append((sig, args))


def _mk_client(methods):
    c = FakeClient()
    for m in methods:
        setattr(c, m, _extract(_CLIENT_PATH, m).__get__(c))
    return c


def test_request_backfill_sends_and_registers():
    c = _mk_client(["request_backfill", "_settle_backfill"])
    assert c.request_backfill("bf_1", {"kinds": [1]}) is True
    assert "bf_1" in c._backfill_pending
    assert all(r.request_once.called for r in c.active_relays.values())


def test_request_backfill_throttles_within_min_interval():
    c = _mk_client(["request_backfill", "_settle_backfill"])
    c.request_backfill("bf_1", {})
    assert c.request_backfill("bf_2", {}) is False  # < 2 s later
    # one in flight per sub_id too
    assert c.request_backfill("bf_1", {}) is False


def test_request_backfill_eose_fanin_settles_once():
    c = _mk_client(["request_backfill", "_settle_backfill", "_handle_eose"])
    c._dm_status_by_sub = {}
    c.request_backfill("bf_1", {})
    c._handle_eose("bf_1", "wss://a")
    assert c.emitted == []  # one relay left
    c._handle_eose("bf_1", "wss://b")
    assert c.emitted == [("backfill-done", ("bf_1",))]
    c._handle_eose("bf_1", "wss://b")  # late duplicate EOSE: no re-fire
    assert len(c.emitted) == 1


def test_request_backfill_stale_batch_settles_on_next_call():
    c = _mk_client(["request_backfill", "_settle_backfill"])
    c.request_backfill("bf_old", {})
    c._backfill_pending["bf_old"]["sent"] -= 60  # relays died, no EOSE
    assert c.request_backfill("bf_new", {}) is True
    assert ("backfill-done", ("bf_old",)) in c.emitted
    assert "bf_old" not in c._backfill_pending


def test_request_backfill_no_relays_returns_false():
    c = _mk_client(["request_backfill", "_settle_backfill"])
    c.active_relays = {}
    assert c.request_backfill("bf_x", {}) is False


# ------------------------------------------------------- FeedModel.is_live


def test_is_live_splits_history_from_new_posts():
    rows = [_mk("b", 200), _mk("a", 100)]
    db = Mock()
    db.get_feed_following = Mock(side_effect=lambda *a, **k: rows)
    m = FeedModel("me", db, page_size=2, max_window=10)
    m.load_first()  # window: b(200), a(100); cursor=(100,'a')
    assert m.is_live(_mk("new", 300)) is True   # newer than tail -> pill
    assert m.is_live(_mk("old", 50)) is False   # backfill history -> DB
    assert m.is_live(_mk("a", 100)) is False    # equal -> not newer


# ------------------------------------------- FeedView backfill plumbing


class FakeSentinel:
    def __init__(self):
        self.label = None
        self.visible = False

    def set_label(self, s):
        self.label = s

    def set_visible(self, v):
        self.visible = v


class FakeVadj:
    def __init__(self, upper=1000, page=400, value=600):  # at bottom
        self._u, self._p, self._v = upper, page, value

    def get_upper(self):
        return self._u

    def get_page_size(self):
        return self._p

    def get_value(self):
        return self._v


def _mk_view(client_ok=True):
    rows = [_mk(f"e{i}", 1000 - i) for i in range(4)]
    db = Mock()
    db.get_feed_following = Mock(side_effect=lambda *a, **k: rows[:2])
    db.get_following_list.return_value = ["pk_a"]
    model = FeedModel("me", db, page_size=2, max_window=10)
    model.load_first()
    model.exhausted = True
    client = Mock()
    client.request_backfill.return_value = client_ok
    view = types.SimpleNamespace(
        feed_model=model,
        _backfill_inflight=None,
        main_window=types.SimpleNamespace(pub_key="me", client=client, db=db),
        _sentinel=FakeSentinel(),
        _vadj=FakeVadj(),
        synced=[],
    )
    view._sync_window = lambda **k: view.synced.append(1)
    view._on_near_bottom = lambda: view.synced.append("bottom")
    for m in ("_begin_backfill", "on_backfill_done"):
        setattr(view, m, _extract(_VIEW_PATH, m,
                                  {"NEAR_BOTTOM_PX": 300,
                                   "__package__": "gnostr.ui"}).__get__(view))
    return view, client


def test_view_begin_backfill_sends_req_and_shows_loading():
    view, client = _mk_view()
    view._begin_backfill()
    sub_id, filt = client.request_backfill.call_args[0]
    assert sub_id.startswith("backfill_following_")
    assert filt["until"] == view.feed_model.cursor[0]
    assert filt["authors"] == ["pk_a"]
    assert view._backfill_inflight == sub_id
    assert view._sentinel.label == "Loading more…"
    assert view._sentinel.visible is True


def test_view_begin_backfill_single_flight_and_throttle():
    view, client = _mk_view()
    view._begin_backfill()
    view._begin_backfill()  # already in flight
    assert client.request_backfill.call_count == 1
    view2, client2 = _mk_view(client_ok=False)
    view2._begin_backfill()
    assert view2._backfill_inflight is None  # throttled: nothing armed


def test_view_backfill_done_retries_page_at_bottom():
    view, _ = _mk_view()
    view._begin_backfill()
    sub_id = view._backfill_inflight
    view.feed_model.exhausted = True
    view.on_backfill_done(None, sub_id)
    assert view._backfill_inflight is None
    assert view.feed_model.exhausted is False
    assert "bottom" in view.synced  # page load retried
    assert view._sentinel.label == "You're all caught up"


def test_view_backfill_done_ignores_stale_sub_id():
    view, _ = _mk_view()
    view._begin_backfill()
    view.on_backfill_done(None, "backfill_following_999999")  # stale
    assert view._backfill_inflight is not None  # untouched
