"""Infinite-scroll phase 1: get_feed_global + FeedSource plumbing.

get_feed_global must honor the same keyset contract as
get_feed_following (pinned in test_feed_pagination.py): (created_at, id)
tuple cursor, newest-first, short page = end-of-DB. FeedSource lets one
FeedModel paginate following/global/me through the same state machine.
"""
from unittest.mock import Mock

from gi.repository import GLib  # noqa: E402 - resolves to the mock in conftest

from gnostr.service.feed_model import FeedModel
from gnostr.service.feed_pagination import make_source


def _patch_glib_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(GLib, "get_user_data_dir", lambda: str(tmp_path / "data"))


def _mk(ev_id, pubkey, created_at, kind=1):
    return {
        "id": ev_id,
        "pubkey": pubkey,
        "created_at": created_at,
        "kind": kind,
        "content": f"c {ev_id}",
        "tags": [],
        "sig": "s",
    }


# ---------------------------------------------------------------- global


def test_get_feed_global_newest_first_no_follow_required(tmp_path, monkeypatch):
    _patch_glib_dirs(monkeypatch, tmp_path)
    from gnostr.database import Database

    db = Database()
    # No following rows at all — global must still see every author.
    for i in range(7):
        db.save_event(_mk(f"e{i:02d}", f"pk_{i % 3}", 100 + i))

    page = db.get_feed_global(limit=3)
    assert [e["id"] for e in page] == ["e06", "e05", "e04"]


def test_get_feed_global_pages_cover_all_exactly_once(tmp_path, monkeypatch):
    _patch_glib_dirs(monkeypatch, tmp_path)
    from gnostr.database import Database

    db = Database()
    events = [_mk(f"e{i:02d}", f"pk_{i % 4}", 1000 + i * 10) for i in range(10)]
    for ev in events:
        db.save_event(ev)

    seen, cursor, guard = [], None, 0
    while guard < 10:
        guard += 1
        page = db.get_feed_global(limit=4, before=cursor)
        if not page:
            break
        seen.extend(e["id"] for e in page)
        cursor = (page[-1]["created_at"], page[-1]["id"])
        if len(page) < 4:
            break
    assert sorted(seen) == sorted(e["id"] for e in events)
    assert len(seen) == len(set(seen))


def test_get_feed_global_tiebreak_on_equal_created_at(tmp_path, monkeypatch):
    _patch_glib_dirs(monkeypatch, tmp_path)
    from gnostr.database import Database

    db = Database()
    for eid in ("a", "b", "c"):
        db.save_event(_mk(eid, "pk_x", 500))

    first = db.get_feed_global(limit=2)
    # id DESC tiebreak: 'c', 'b' newest-first
    assert [e["id"] for e in first] == ["c", "b"]
    second = db.get_feed_global(
        limit=2, before=(first[-1]["created_at"], first[-1]["id"])
    )
    assert [e["id"] for e in second] == ["a"]


def test_get_feed_global_pubkey_narrows_to_author(tmp_path, monkeypatch):
    _patch_glib_dirs(monkeypatch, tmp_path)
    from gnostr.database import Database

    db = Database()
    db.save_event(_mk("mine1", "me", 100))
    db.save_event(_mk("theirs", "other", 200))
    db.save_event(_mk("mine2", "me", 300))

    page = db.get_feed_global(pubkey="me")
    assert [e["id"] for e in page] == ["mine2", "mine1"]


def test_get_feed_global_excludes_non_kind1(tmp_path, monkeypatch):
    _patch_glib_dirs(monkeypatch, tmp_path)
    from gnostr.database import Database

    db = Database()
    db.save_event(_mk("post", "pk_a", 100))
    db.save_event(_mk("repost", "pk_a", 200, kind=6))

    assert [e["id"] for e in db.get_feed_global()] == ["post"]


# ---------------------------------------------------------------- source


def _bidir_db(rows):
    """Mock Database honoring keyset before/after on both queries."""
    db = Mock()

    def _filter(limit, before=None, after=None, pubkey=None):
        out = list(rows)
        if pubkey is not None:
            out = [r for r in out if r["pubkey"] == pubkey]
        if after is not None:
            out = [r for r in out if (r["created_at"], r["id"]) > after]
        elif before is not None:
            out = [r for r in out if (r["created_at"], r["id"]) < before]
        return out[:limit]

    db.get_feed_global = Mock(side_effect=lambda **kw: _filter(**kw))
    db.get_feed_following = Mock(
        side_effect=lambda owner, limit, before=None, after=None: _filter(
            limit, before=before, after=after
        )
    )
    return db


def test_model_pages_global_through_source():
    rows = [_mk(f"g{i}", f"pk{i % 3}", 1000 - i) for i in range(12)]
    db = _bidir_db(rows)
    m = FeedModel("me", db, page_size=5, max_window=100,
                  source=make_source("global", db, "me"))
    first = m.load_first()
    assert len(first) == 5
    assert not m.exhausted
    older = m.load_older()
    assert len(older) == 5
    older2 = m.load_older()
    assert len(older2) == 2
    assert m.exhausted  # short page = end of DB
    assert m.load_older() is None


def test_model_me_source_filters_live_events():
    db = _bidir_db([])
    src = make_source("me", db, "me")
    assert src.accepts(_mk("x", "me", 1))
    assert not src.accepts(_mk("y", "other", 2))
    assert not src.accepts(_mk("z", "me", 3, kind=6))


def test_model_me_source_pages_own_posts_only():
    rows = [
        _mk("m1", "me", 300),
        _mk("t1", "them", 250),
        _mk("m2", "me", 200),
    ]
    db = _bidir_db(rows)
    m = FeedModel("me", db, page_size=5, max_window=100,
                  source=make_source("me", db, "me"))
    assert m.load_first() == ["m1", "m2"]


def test_default_source_is_following():
    db = _bidir_db([_mk("f1", "pk_a", 100)])
    m = FeedModel("me", db)
    assert m.source.kind == "following"
    m.load_first()
    db.get_feed_following.assert_called()
