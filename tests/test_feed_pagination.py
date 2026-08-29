"""Tests for keyset (cursor) pagination on database.get_feed_following.

Workstream #0 (feed pruning) contract 2: pagination is keyset on
(created_at, id), never OFFSET — OFFSET degrades with depth and
double-serves rows when new events arrive mid-scroll.

The real `Database` is used against a temp SQLite file (GLib dirs mocked),
so the assertions exercise the actual SQL. The "no OFFSET" guard reads the
source to catch a regression to an OFFSET-based query.
"""
import re

from gi.repository import GLib  # noqa: E402 - resolves to the mock in conftest


def _patch_glib_dirs(monkeypatch, tmp_path):
    # Database resolves GLib.get_user_data_dir at construction.
    monkeypatch.setattr(GLib, "get_user_data_dir", lambda: str(tmp_path / "data"))


def _seed(db, owner, events):
    """Insert kind-1 events and mark their authors as followed by `owner`."""
    followed = sorted({ev["pubkey"] for ev in events})
    db.save_contacts(owner, followed)
    for ev in events:
        db.save_event(ev)


def _mk(ev_id, pubkey, created_at):
    return {
        "id": ev_id,
        "pubkey": pubkey,
        "created_at": created_at,
        "kind": 1,
        "content": f"content {ev_id}",
        "tags": [],
        "sig": "s",
    }


def test_get_feed_following_first_page_newest_first(tmp_path, monkeypatch):
    _patch_glib_dirs(monkeypatch, tmp_path)
    from gnostr.database import Database

    db = Database()
    events = [_mk(f"e{i:02d}", "pk_a" if i % 2 else "pk_b", 100 + i) for i in range(7)]
    _seed(db, "me", events)

    page = db.get_feed_following("me", limit=3)
    assert [e["id"] for e in page] == ["e06", "e05", "e04"]
    assert page[0]["pubkey"] == "pk_b"


def test_get_feed_following_pages_cover_all_exactly_once(tmp_path, monkeypatch):
    _patch_glib_dirs(monkeypatch, tmp_path)
    from gnostr.database import Database

    db = Database()
    events = [_mk(f"e{i:02d}", f"pk_{i % 3}", 1000 + i * 10) for i in range(10)]
    _seed(db, "me", events)

    seen = []
    before = None
    pages = 0
    while True:
        page = db.get_feed_following("me", limit=4, before=before)
        pages += 1
        if not page:
            break
        seen.extend(e["id"] for e in page)
        # page strictly older than the previous page's tail
        if before is not None:
            assert all((e["created_at"], e["id"]) < before for e in page)
        before = (page[-1]["created_at"], page[-1]["id"])
        if len(page) < 4:  # short page = end of DB
            break
        assert pages < 20, "pagination not converging"

    # exactly once, newest-first, no gaps or dupes
    assert seen == [f"e{i:02d}" for i in range(9, -1, -1)]


def test_get_feed_following_keyset_stable_under_new_events_mid_scroll(
    tmp_path, monkeypatch
):
    """The OFFSET hazard: events arriving between pages must not cause the
    next page to re-serve or skip rows. Keyset pagination is stable."""
    _patch_glib_dirs(monkeypatch, tmp_path)
    from gnostr.database import Database

    db = Database()
    events = [_mk(f"e{i:02d}", "pk_a", 100 + i) for i in range(10)]
    _seed(db, "me", events)

    page1 = db.get_feed_following("me", limit=3)
    assert len(page1) == 3
    before = (page1[-1]["created_at"], page1[-1]["id"])

    # a brand-new event lands while the user is mid-scroll
    db.save_event(_mk("eNEW", "pk_a", 99999))

    page2 = db.get_feed_following("me", limit=3, before=before)
    # no overlap with page 1 (e09,e08,e07), no skip — e06 follows e07,
    # and the new event (newer than the cursor) is NOT served here; refresh's job
    assert [e["id"] for e in page2] == ["e06", "e05", "e04"]
    assert "eNEW" not in [e["id"] for e in page2]


def test_get_feed_following_tiebreak_on_equal_created_at(tmp_path, monkeypatch):
    """Two events sharing created_at must paginate deterministically via the
    id tiebreaker — no dupe, no drop across the page boundary."""
    _patch_glib_dirs(monkeypatch, tmp_path)
    from gnostr.database import Database

    db = Database()
    same_ts = [1000] * 5
    events = [_mk(f"e{i:02d}", "pk_a", ts) for i, ts in enumerate(same_ts)]
    _seed(db, "me", events)

    page1 = db.get_feed_following("me", limit=3)
    before = (page1[-1]["created_at"], page1[-1]["id"])
    page2 = db.get_feed_following("me", limit=10, before=before)

    all_ids = [e["id"] for e in page1] + [e["id"] for e in page2]
    assert len(all_ids) == len(set(all_ids)) == 5  # exactly once, no dupe/drop
    # each page internally sorted by (created_at DESC, id DESC)
    for page in (page1, page2):
        keys = [(e["created_at"], e["id"]) for e in page]
        assert keys == sorted(keys, reverse=True)


def test_get_feed_following_short_page_signals_end_of_db(tmp_path, monkeypatch):
    _patch_glib_dirs(monkeypatch, tmp_path)
    from gnostr.database import Database

    db = Database()
    _seed(db, "me", [_mk(f"e{i:02d}", "pk_a", 100 + i) for i in range(5)])

    page1 = db.get_feed_following("me", limit=4)
    assert len(page1) == 4
    page2 = db.get_feed_following(
        "me", limit=4, before=(page1[-1]["created_at"], page1[-1]["id"])
    )
    assert len(page2) < 4  # short page: exhausted
    assert len(page2) == 1
    page3 = db.get_feed_following(
        "me", limit=4, before=(page2[-1]["created_at"], page2[-1]["id"])
    )
    assert page3 == []


def test_get_feed_following_filters_non_posts_and_unfollowed(tmp_path, monkeypatch):
    _patch_glib_dirs(monkeypatch, tmp_path)
    from gnostr.database import Database

    db = Database()
    _seed(db, "me", [_mk("e01", "pk_a", 100), _mk("e02", "pk_a", 101)])
    # non-kind-1 event from a followed author
    db.save_event({"id": "k0", "pubkey": "pk_a", "created_at": 200, "kind": 0,
                   "content": "profile", "tags": [], "sig": "s"})
    db.save_event({"id": "k7", "pubkey": "pk_a", "created_at": 201, "kind": 7,
                   "content": "+", "tags": [], "sig": "s"})
    # kind-1 event from an unfollowed author
    db.save_event(_mk("stranger", "pk_z", 300))

    ids = [e["id"] for e in db.get_feed_following("me", limit=50)]
    assert ids == ["e02", "e01"]


def test_get_feed_following_sql_never_uses_offset():
    """Guard: the feed query must stay keyset — a regression to OFFSET
    re-introduces depth degradation + mid-scroll row dupe/skip.

    Reads the source file directly (not via `import gnostr`), so the guard
    works even in environments where the full package's optional deps
    (e.g. `ecdsa` for nostr_utils) are absent.
    """
    import os
    import re as _re

    here = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.join(here, "..", "src", "gnostr", "database.py")
    with open(db_path) as f:
        src = f.read()
    fn_match = _re.search(
        r"def get_feed_following\(.*?\n(?=\n    def )", src, _re.DOTALL
    )
    assert fn_match, "get_feed_following not found"
    # strip the leading docstring so the guard inspects SQL, not prose
    fn_body = _re.sub(r'"""[\s\S]*?"""', "", fn_match.group(), count=1)
    assert not _re.search(r"\bOFFSET\b", fn_body, _re.IGNORECASE)
    assert "(e.created_at, e.id) < (?, ?)" in fn_body


def test_feed_query_uses_feed_index(tmp_path, monkeypatch):
    """EXPLAIN QUERY PLAN: both pages SEARCH events via idx_events_feed
    (pubkey + kind + created_at) instead of a full scan."""
    _patch_glib_dirs(monkeypatch, tmp_path)
    from gnostr.database import Database

    db = Database()
    _seed(db, "me", [_mk(f"e{i:02d}", f"pk_{i % 2}", 100 + i) for i in range(8)])

    def plan_for(before):
        where = "WHERE f.owner_pubkey = ? AND e.kind = 1"
        params = ("me",)
        if before:
            where += " AND (e.created_at, e.id) < (?, ?)"
            params += (150, "e05")
        sql = (
            "EXPLAIN QUERY PLAN "
            "SELECT e.* FROM events e INNER JOIN following f "
            "ON e.pubkey = f.followed_pubkey "
            + where
            + " ORDER BY e.created_at DESC, e.id DESC LIMIT ?"
        )
        rows = db.conn.execute(sql, params + (4,)).fetchall()
        return " | ".join(str(r) for r in rows)

    first_plan = plan_for(before=None)
    cursor_plan = plan_for(before=True)
    for plan in (first_plan, cursor_plan):
        assert "idx_events_feed" in plan, plan
        assert "SCAN e" not in plan, plan
