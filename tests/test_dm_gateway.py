"""Tests for the DM gateway data layer (slice 1 of 5).

Exercises the real SQLiteDMGateway against an isolated temp gnostr.db — no
relay I/O, no crypto, no GTK. Follows the test_profile_db.py pattern: patch
GLib.get_user_data_dir to a temp dir so the real Database/Gateway class runs
against an isolated SQLite file.

Covers:
- inbound vs. outbound rumor roll-up (unread_count)
- conversation upsert + activity ordering
- thread paging with keyset before_ts (newest page + older page)
- mark_read zeroes unread
- idempotent schema init (coexists with events/profiles tables)
- the abstract contract (AbstractDMGateway) is enforced
"""

from gi.repository import GLib  # noqa: E402 - resolves to the conftest mock


def _make_gateway(tmp_path, monkeypatch, our_pubkey: str = "owner"):
    monkeypatch.setattr(GLib, "get_user_data_dir", lambda: str(tmp_path / "data"))
    from gnostr.gateway.dm_gateway import SQLiteDMGateway

    gw = SQLiteDMGateway()
    gw.set_our_pubkey(our_pubkey)
    return gw


def _event(event_id, sender, ts, content, tags=None):
    return {
        "id": event_id,
        "pubkey": sender,
        "created_at": ts,
        "content": content,
        "tags": tags or [],
    }


def test_inbound_rumor_creates_conversation_and_bumps_unread(tmp_path, monkeypatch):
    gw = _make_gateway(tmp_path, monkeypatch, our_pubkey="owner")
    gw.save_event(_event("e1", "peerA", 100, "hello"))

    convs = gw.get_conversations(10)
    assert len(convs) == 1
    assert convs[0]["peer_pk"] == "peerA"
    assert convs[0]["unread_count"] == 1
    assert convs[0]["last_event_id"] == "e1"
    assert convs[0]["last_ts"] == 100


def test_outbound_rumor_does_not_bump_unread(tmp_path, monkeypatch):
    gw = _make_gateway(tmp_path, monkeypatch, our_pubkey="owner")
    gw.save_event(_event("e1", "owner", 100, "sent"))

    convs = gw.get_conversations(10)
    assert len(convs) == 1
    assert convs[0]["peer_pk"] == "owner"
    assert convs[0]["unread_count"] == 0


def test_second_inbound_from_same_peer_does_not_double_unread(tmp_path, monkeypatch):
    gw = _make_gateway(tmp_path, monkeypatch, our_pubkey="owner")
    gw.save_event(_event("e1", "peerA", 100, "first"))
    gw.save_event(_event("e2", "peerA", 110, "second"))

    convs = gw.get_conversations(10)
    assert len(convs) == 1
    assert convs[0]["unread_count"] == 2
    assert convs[0]["last_event_id"] == "e2"
    assert convs[0]["last_ts"] == 110


def test_unread_zeroed_after_mark_read(tmp_path, monkeypatch):
    gw = _make_gateway(tmp_path, monkeypatch, our_pubkey="owner")
    gw.save_event(_event("e1", "peerA", 100, "hi"))
    assert gw.get_conversations(10)[0]["unread_count"] == 1

    gw.mark_read("peerA")
    assert gw.get_conversations(10)[0]["unread_count"] == 0

    # A new inbound after mark_read bumps unread back to 1.
    gw.save_event(_event("e2", "peerA", 120, "later"))
    assert gw.get_conversations(10)[0]["unread_count"] == 1


def test_get_thread_newest_first(tmp_path, monkeypatch):
    gw = _make_gateway(tmp_path, monkeypatch, our_pubkey="owner")
    # Both messages belong to peerA's conversation (keyed by sender).
    gw.save_event(_event("e1", "peerA", 100, "old"))
    gw.save_event(_event("e2", "peerA", 110, "new"))

    thread = gw.get_thread("peerA", 10, None)
    assert [m["id"] for m in thread] == ["e2", "e1"]
    assert thread[0]["sender_pk"] == "peerA"
    assert thread[0]["content"] == "new"


def test_get_thread_before_ts_pages_older(tmp_path, monkeypatch):
    gw = _make_gateway(tmp_path, monkeypatch, our_pubkey="owner")
    for ts in (100, 110, 120, 130):
        gw.save_event(_event(f"e{ts}", "peerA", ts, f"m{ts}"))

    # Newest page: e130, e120
    page1 = gw.get_thread("peerA", 2, None)
    assert [m["id"] for m in page1] == ["e130", "e120"]

    # Next OLDER page using the oldest ts of page1 as the keyset cursor.
    page2 = gw.get_thread("peerA", 2, before_ts=page1[-1]["rumor_ts"])
    assert [m["id"] for m in page2] == ["e110", "e100"]

    # No further older pages.
    page3 = gw.get_thread("peerA", 2, before_ts=page2[-1]["rumor_ts"])
    assert page3 == []


def test_get_thread_only_returns_messages_for_peer(tmp_path, monkeypatch):
    gw = _make_gateway(tmp_path, monkeypatch, our_pubkey="owner")
    gw.save_event(_event("e1", "peerA", 100, "mine"))
    gw.save_event(_event("e2", "peerB", 200, "theirs"))

    assert [m["id"] for m in gw.get_thread("peerA", 10, None)] == ["e1"]
    assert [m["id"] for m in gw.get_thread("peerB", 10, None)] == ["e2"]


def test_schema_coexists_with_existing_tables(tmp_path, monkeypatch):
    """DM tables are created idempotently alongside events/profiles/following."""
    import sqlite3

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = sqlite3.connect(data_dir / "gnostr.db")
    conn.execute(
        "CREATE TABLE events (id TEXT PRIMARY KEY, pubkey TEXT, created_at INTEGER, "
        "kind INTEGER, content TEXT, tags TEXT, sig TEXT)"
    )
    conn.execute(
        "CREATE TABLE profiles (pubkey TEXT PRIMARY KEY, name TEXT, updated_at INTEGER)"
    )
    conn.commit()
    conn.close()

    gw = _make_gateway(tmp_path, monkeypatch)
    gw.save_event(_event("dm1", "peerA", 1, "hi"))
    assert len(gw.get_conversations(10)) == 1
    gw.close()


def test_get_conversations_limit(tmp_path, monkeypatch):
    gw = _make_gateway(tmp_path, monkeypatch, our_pubkey="owner")
    for i in range(5):
        gw.save_event(_event(f"e{i}", f"peer{i}", 100 + i, f"m{i}"))

    convs = gw.get_conversations(3)
    assert len(convs) == 3
    # Newest activity first.
    assert convs[0]["peer_pk"] == "peer4"


def test_activity_ordering_across_peers(tmp_path, monkeypatch):
    gw = _make_gateway(tmp_path, monkeypatch, our_pubkey="owner")
    gw.save_event(_event("a", "peerA", 100, "x"))
    gw.save_event(_event("b", "peerB", 300, "y"))
    gw.save_event(_event("c", "peerC", 200, "z"))

    peers = [c["peer_pk"] for c in gw.get_conversations(10)]
    assert peers == ["peerB", "peerC", "peerA"]


def test_missing_pubkey_event_still_persisted(tmp_path, monkeypatch):
    gw = _make_gateway(tmp_path, monkeypatch, our_pubkey="owner")
    gw.save_event({"id": "e1", "created_at": 100, "content": "no sender", "tags": []})

    convs = gw.get_conversations(10)
    assert len(convs) == 1
    assert convs[0]["peer_pk"] is None


def test_close_is_idempotent(tmp_path, monkeypatch):
    gw = _make_gateway(tmp_path, monkeypatch)
    gw.close()
    gw.close()  # must not raise on a second close


def test_abstract_contract_enforced():
    """A class missing any abstract method cannot be instantiated."""
    from gnostr.gateway.dm_gateway import AbstractDMGateway

    class Broken(AbstractDMGateway):
        pass

    try:
        Broken()
    except TypeError:
        # Expected: abstract methods not implemented.
        return
    raise AssertionError("AbstractDMGateway did not enforce abstract methods")


def test_save_event_no_connection_is_safe(tmp_path, monkeypatch):
    from gnostr.gateway.dm_gateway import SQLiteDMGateway

    gw = SQLiteDMGateway.__new__(SQLiteDMGateway)  # bypass __init__ (no conn)
    gw.conn = None
    gw.lock = __import__("threading").Lock()
    gw._our_pubkey_override = ""
    # Should not raise.
    gw.save_event(_event("e1", "peerA", 100, "hi"))
    gw.get_conversations(10)
