"""Tests for FeedModel — the widget-agnostic feed data window (workstream
#0, task 2).

FeedModel owns the ordered event-id window, the keyset cursor state, the
bounded-window eviction policy, and the live-prepend buffer. It must
stay GTK-free so it is unit-testable headless: the only dependency is
a Database-like object, which we mock here (no real SQLite).

Ordering contract (mirrors get_feed_following): newest first, ordered by
(created_at DESC, id DESC). The model stores only event ids; the view
hydrates rows via database.get_event(id).

Cursor contract: ``cursor`` is always (created_at, id) of the OLDEST
event in the window. ``load_older()`` passes it as ``before=`` to fetch
the next page strictly older; the model appends it, then advances the
cursor to the new page's tail.
"""
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gnostr.service.feed_model import FeedModel  # noqa: E402


def _mk(eid, created, **kw):
    d = {"id": eid, "created_at": created}
    d.update(kw)
    return d


def _db(pages, limit=50):
    """Mock Database whose get_feed_following pops pages in order."""
    db = Mock()
    calls = []

    def _fetch(owner, limit, before=None):
        calls.append({"owner": owner, "limit": limit, "before": before})
        if not pages:
            return []
        return list(pages.pop(0))

    db.get_feed_following = _fetch
    return db, calls


def _db_bidir(rows, limit=50):
    """Mock Database over a single newest-first row list, honouring the
    keyset (created_at, id) before=/after= semantics like the real query.

    ``rows`` must already be sorted newest-first. Returns (db, calls) where
    each call records {'owner', 'limit', 'before', 'after'}.
    """

    db = Mock()
    calls = []

    def _fetch(owner, limit, before=None, after=None):
        calls.append({"owner": owner, "limit": limit, "before": before, "after": after})
        out = list(rows)
        if after is not None:
            a_ca, a_id = after
            out = [r for r in out if (r["created_at"], r["id"]) > (a_ca, a_id)]
        elif before is not None:
            b_ca, b_id = before
            out = [r for r in out if (r["created_at"], r["id"]) < (b_ca, b_id)]
        return out[:limit]

    db.get_feed_following = _fetch
    return db, calls


class TestLoadFirst:
    def test_loads_first_page_and_sets_cursor(self):
        page = [_mk("e3", 300), _mk("e2", 200), _mk("e1", 100)]
        db, calls = _db([page])
        m = FeedModel(owner_pubkey="me", database=db)
        m.load_first()
        assert m.event_ids == ["e3", "e2", "e1"]
        assert m.cursor == (100, "e1")  # oldest in window
        assert calls[0] == {"owner": "me", "limit": 50, "before": None}
        # 3 < 50 -> short page -> end-of-DB sentinel set
        assert m.exhausted

    def test_full_page_keeps_cursor_open(self):
        full = [_mk(f"e{i}", i) for i in range(50, 0, -1)]
        db, _ = _db([full])
        m = FeedModel(owner_pubkey="me", database=db)
        m.load_first()
        assert len(m.event_ids) == 50
        assert m.cursor == (1, "e1")
        assert not m.exhausted

    def test_empty_feed_marks_exhausted(self):
        db, _ = _db([[]])
        m = FeedModel(owner_pubkey="me", database=db)
        m.load_first()
        assert m.event_ids == []
        assert m.exhausted
        assert m.cursor is None


class TestLoadOlder:
    def test_fetches_strictly_older_and_appends(self):
        page1 = [_mk("e3", 300), _mk("e2", 200), _mk("e1", 100)]
        page2 = [_mk("e0", 50)]
        db, calls = _db([page1, page2])
        m = FeedModel(owner_pubkey="me", database=db, page_size=3)
        m.load_first()  # 3 == page_size -> full page -> not exhausted
        assert not m.exhausted
        added = m.load_older()
        assert added == ["e0"]
        assert m.event_ids == ["e3", "e2", "e1", "e0"]
        # cursor passed to the DB was page-1's tail:
        assert calls[1]["before"] == (100, "e1")
        # page2 (1 row) < page_size 3 -> end-of-DB:
        assert m.exhausted
        assert m.cursor == (50, "e0")

    def test_full_second_page_keeps_cursor_open(self):
        page1 = [_mk("e2", 200), _mk("e1", 100)]
        # o50(50)..o1(1): newest-first, strictly older than page1
        page2 = [_mk(f"o{i}", i) for i in range(50, 0, -1)]
        db, calls = _db([page1, page2])
        m = FeedModel(owner_pubkey="me", database=db, page_size=2,
                      max_window=100)
        m.load_first()  # 2 == page_size -> not exhausted
        assert not m.exhausted
        m.load_older()  # full page again (50 == page_size? no: 50 > 2)
        # 50 rows returned >= page_size 2 -> NOT exhausted
        assert not m.exhausted
        assert len(m.event_ids) == 52
        # cursor = window tail (oldest) = o1
        assert m.cursor == (1, "o1")
        # second fetch used page-1's tail as the cursor
        assert calls[1]["before"] == (100, "e1")

    def test_exhausted_returns_none_and_skips_db(self):
        db, calls = _db([[_mk("e1", 100)]])
        m = FeedModel(owner_pubkey="me", database=db)
        m.load_first()
        assert m.exhausted
        assert m.load_older() is None
        # no second fetch
        assert len(calls) == 1

    def test_empty_page_marks_exhausted(self):
        db, _ = _db([[_mk("e1", 100)], []], limit=2)
        m = FeedModel(owner_pubkey="me", database=db, page_size=2)
        m.load_first()  # 1 < 2 -> exhausted already
        assert m.exhausted
        assert m.load_older() is None


class TestEviction:
    def test_evict_newest_bounds_window_without_touching_cursor(self):
        page = [_mk(f"e{i}", i) for i in range(10, 0, -1)]  # e10..e1, newest first
        db, _ = _db([page], limit=10)
        m = FeedModel(owner_pubkey="me", database=db, max_window=8)
        m.load_first()
        # first page: viewport at top -> bound by evicting the 2 OLDEST
        assert len(m.event_ids) == 8
        assert m.event_ids == ["e10", "e9", "e8", "e7", "e6", "e5", "e4", "e3"]
        assert "e1" not in m.event_ids
        assert "e2" not in m.event_ids
        assert m.cursor == (3, "e3")
        # the 2 newest were evicted by explicit evict_newest
        evicted = m.evict_newest(2)
        assert evicted == ["e10", "e9"]
        # oldest (cursor anchor) untouched
        assert m.cursor == (3, "e3")

    def test_eviction_on_load_older_keeps_oldest_anchor(self):
        # page1: e1..e10, created 99..90, newest-first
        page1 = [_mk(f"e{i}", 100 - i) for i in range(1, 11)]
        # page2: o1..o5, created 88..84, all strictly older
        page2 = [_mk(f"o{i}", 89 - i) for i in range(1, 6)]
        db, _ = _db([page1, page2], limit=10)
        # page_size=10 so page1 (10 rows) is a FULL page -> load_older runs
        m = FeedModel(owner_pubkey="me", database=db, page_size=10,
                      max_window=15)
        m.load_first()
        m.load_older()
        assert len(m.event_ids) == 15  # 10 + 5, unbounded
        # scroll down: drop the 7 newest off the top -> window of 8
        evicted = m.evict_newest(7)
        assert evicted == ["e1", "e2", "e3", "e4", "e5", "e6", "e7"]
        # oldest (cursor) end intact; cursor untouched by top eviction.
        # window tail is o5 (created 84), the oldest row.
        assert m.event_ids == ["e8", "e9", "e10", "o1", "o2", "o3", "o4", "o5"]
        assert m.event_ids[-1] == "o5"
        assert m.cursor == (84, "o5")

    def test_evict_newest_manual(self):
        page = [_mk("e2", 200), _mk("e1", 100)]
        db, _ = _db([page], limit=50)
        m = FeedModel(owner_pubkey="me", database=db)
        m.load_first()
        evicted = m.evict_newest(1)
        assert evicted == ["e2"]
        assert m.event_ids == ["e1"]
        # cursor (oldest) untouched by top eviction
        assert m.cursor == (100, "e1")

    def test_evict_all_sets_cursor_none_and_exhausted(self):
        page = [_mk("e1", 100)]
        db, _ = _db([page], limit=50)
        m = FeedModel(owner_pubkey="me", database=db)
        m.load_first()
        evicted = m.evict_newest(1)
        assert evicted == ["e1"]
        assert m.event_ids == []
        assert m.cursor is None
        assert m.exhausted


class TestLiveBuffer:
    def test_live_events_buffered_not_appended_while_scrolled(self):
        db, _ = _db([[_mk("e3", 300), _mk("e2", 200), _mk("e1", 100)]], limit=50)
        m = FeedModel(owner_pubkey="me", database=db)
        m.load_first()
        m.prepend_new([_mk("n1", 400)])
        m.prepend_new([_mk("n2", 350)])
        # new events are NOT in the visible window yet
        assert "n1" not in m.event_ids and "n2" not in m.event_ids
        # newest first in the buffer (the pill count source)
        assert m.new_ids == ["n1", "n2"]

    def test_live_insert_preserves_order(self):
        db, _ = _db([[_mk("e3", 300), _mk("e2", 200), _mk("e1", 100)]], limit=50)
        m = FeedModel(owner_pubkey="me", database=db)
        m.load_first()
        # arrive out of order: older first, newer second
        m.prepend_new([_mk("n2", 350)])
        m.prepend_new([_mk("n1", 400)])
        assert m.new_ids == ["n1", "n2"]
        # mid-stream event slots between buffered ones
        m.prepend_new([_mk("nm", 325)])
        assert m.new_ids == ["n1", "n2", "nm"]

    def test_live_dedupes_against_window(self):
        db, _ = _db([[_mk("e3", 300), _mk("e2", 200), _mk("e1", 100)]], limit=50)
        m = FeedModel(owner_pubkey="me", database=db)
        m.load_first()
        # e2 already in the window -> not buffered
        added = m.prepend_new([_mk("e2", 200)])
        assert added == []
        assert m.new_ids == []

    def test_flush_new_at_top_prepends_buffer(self):
        db, _ = _db([[_mk("e3", 300), _mk("e2", 200), _mk("e1", 100)]], limit=50)
        m = FeedModel(owner_pubkey="me", database=db)
        m.load_first()
        m.prepend_new([_mk("n1", 400)])
        m.prepend_new([_mk("n2", 350)])
        added = m.flush_new_at_top()
        assert added == ["n1", "n2"]
        assert m.event_ids == ["n1", "n2", "e3", "e2", "e1"]
        assert m.new_ids == []
        # cursor (oldest) unchanged by a top prepend
        assert m.cursor == (100, "e1")

    def test_flush_empty_is_noop(self):
        db, _ = _db([[_mk("e1", 100)]], limit=50)
        m = FeedModel(owner_pubkey="me", database=db)
        m.load_first()
        assert m.flush_new_at_top() == []
        assert m.event_ids == ["e1"]


class TestReset:
    def test_reset_clears_window_cursor_and_buffer(self):
        page = [_mk("e3", 300), _mk("e2", 200), _mk("e1", 100)]
        # two copies: the mock pops a page per load_first
        db, _ = _db([page, list(page)])
        m = FeedModel(owner_pubkey="me", database=db)
        m.load_first()
        m.prepend_new([_mk("n1", 400)])
        m.reset()
        assert m.event_ids == []
        assert m.new_ids == []
        assert m.cursor is None
        assert m.exhausted
        # a fresh first load works after reset
        again = m.load_first()
        assert again == ["e3", "e2", "e1"]


class TestBoundaries:
    def test_max_window_one(self):
        page = [_mk("e2", 200), _mk("e1", 100)]
        db, _ = _db([page], limit=2)
        m = FeedModel(owner_pubkey="me", database=db, max_window=1)
        m.load_first()
        # first page keeps the NEWEST: only e2 survives the bound of 1
        assert m.event_ids == ["e2"]
        assert m.cursor == (200, "e2")

    def test_page_size_one(self):
        db, calls = _db([[_mk("e1", 100)], [_mk("e0", 50)]], limit=1)
        m = FeedModel(owner_pubkey="me", database=db, page_size=1)
        m.load_first()
        # 1 == page_size(1) -> full page -> NOT exhausted
        assert not m.exhausted
        m.load_older()
        assert calls[1]["before"] == (100, "e1")
        # page2 (1) == page_size(1) -> full -> still not exhausted
        assert not m.exhausted
        assert m.event_ids == ["e1", "e0"]


class TestBidirectionalRepull:
    """Contract 6: viewport-relative eviction + re-pull on scroll-up.

    The window slides down (load_older evicts the newest end) then back
    up (load_newer re-pulls the evicted top, evicting the oldest end).
    """

    def _full_feed(self, n=10):
        # newest-first: p1(100) .. p10(91)
        return [_mk(f"p{i}", 100 - (i - 1)) for i in range(1, n + 1)]

    def test_load_first_is_at_top(self):
        db, _ = _db_bidir(self._full_feed(), limit=10)
        m = FeedModel(owner_pubkey="me", database=db, page_size=5, max_window=10)
        m.load_first()
        assert m.at_top
        assert m.can_load_newer() is False
        assert m.top_key == (100, "p1")
        assert m.cursor == (96, "p5")

    def test_load_newer_is_noop_at_top(self):
        db, calls = _db_bidir(self._full_feed(), limit=10)
        m = FeedModel(owner_pubkey="me", database=db, page_size=5, max_window=10)
        m.load_first()
        assert m.load_newer() is None
        # no extra fetch
        assert len(calls) == 1

    def test_repull_after_top_eviction(self):
        rows = self._full_feed()
        db, calls = _db_bidir(rows, limit=10)
        m = FeedModel(owner_pubkey="me", database=db, page_size=5, max_window=4)
        m.load_first()
        # window p1..p4; p5 (oldest) evicted off the bottom; at the top
        assert m.event_ids == ["p1", "p2", "p3", "p4"]
        assert m.at_top is True
        assert m.cursor == (97, "p4")

        # scroll down: the view evicts the 2 newest off the top
        evicted = m.evict_newest(2)
        assert evicted == ["p1", "p2"]
        assert m.event_ids == ["p3", "p4"]
        assert m.at_top is False
        assert m.can_load_newer() is True

        # scroll up: re-pull newer than top_key (98,p3) -> p2(99), p1(100)
        added = m.load_newer()
        assert added == ["p1", "p2"]
        # window p1..p4 again, bounded to 4; back at the absolute top
        assert m.event_ids == ["p1", "p2", "p3", "p4"]
        assert m.at_top is True
        assert m.cursor == (97, "p4")
        # re-fetch used top_key as `after`
        assert calls[1] == {
            "owner": "me", "limit": 5, "before": None, "after": (98, "p3")
        }

    def test_repull_reaches_absolute_top(self):
        rows = self._full_feed(5)  # p1..p5
        db, _ = _db_bidir(rows, limit=5)
        # page_size 2, max_window 2: load_first -> p1,p2 ; at_top True (full page)
        m = FeedModel(owner_pubkey="me", database=db, page_size=2, max_window=2)
        m.load_first()
        assert m.at_top  # p1,p2 is the newest page
        # evict the newest to simulate scrolling down
        m.evict_newest(2)
        assert m.event_ids == []
        # re-pull: after=top_key(None) -> nothing to re-pull (window empty)
        assert m.load_newer() is None

    def test_evict_oldest_reopens_exhausted(self):
        # build an exhausted feed, then evict the oldest end to reopen it
        rows = [_mk("a", 50), _mk("b", 40), _mk("c", 30)]
        db, _ = _db_bidir(rows, limit=5)
        m = FeedModel(owner_pubkey="me", database=db, page_size=5, max_window=5)
        m.load_first()
        assert m.exhausted  # 3 < 5 -> end of DB
        m.evict_oldest(1)
        # dropping the oldest means older posts may exist -> not exhausted
        assert m.exhausted is False
        assert m.event_ids == ["a", "b"]
        assert m.cursor == (40, "b")

    def test_evict_newest_flags_not_at_top(self):
        rows = [_mk("a", 50), _mk("b", 40), _mk("c", 30)]
        db, _ = _db_bidir(rows, limit=5)
        m = FeedModel(owner_pubkey="me", database=db, page_size=5, max_window=5)
        m.load_first()
        assert m.at_top is True
        m.evict_newest(1)
        assert m.at_top is False  # 'a' evicted -> newer posts to re-pull
        assert m.can_load_newer() is True
