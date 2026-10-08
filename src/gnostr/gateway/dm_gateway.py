"""DM data gateway — abstract interface + SQLite-backed implementation.

Slice 1 of 5 (DM spec: docs/specs/nip44-nip17-dms.md): the persistence layer
only. No relay I/O, no crypto, no UI. The crypto/service/network layers
(DM-2/DM-3) build on top of this contract.

Follows the existing Gateway/Repository pattern in gateway/gateway.py: an
abstract contract first, then a concrete SQLite implementation mirroring the
``Database`` class in src/gnostr/database.py (threaded lock, parameterized
queries, JSON tags, per-op error logging).

Tables (extend gnostr.db, never replace it):

    dm_conversations (peer_pk TEXT PRIMARY KEY, last_event_id TEXT,
        last_ts INTEGER, unread_count INTEGER DEFAULT 0)
    dm_messages (id TEXT PRIMARY KEY, peer_pk TEXT, sender_pk TEXT,
        content TEXT, rumor_ts INTEGER, inserted_at INTEGER, direction TEXT)

The schema is created idempotently on init (CREATE TABLE IF NOT EXISTS) so it
coexists with the events/profiles/following tables already in the DB.
"""

import abc
import json
import os
import sqlite3
import threading
import time
import traceback

from gi.repository import GLib


class AbstractDMGateway(abc.ABC):
    """Contract for DM persistence. Concrete impls (SQLiteDMGateway) must
    implement every method below. Slice 2/3 consumers depend only on this."""

    @abc.abstractmethod
    def save_event(self, event: dict) -> None:
        """Persist a DM event (rumor or wrap).

        `event` is the raw kind-14 rumor / kind-1059 wrap dict with keys:
        id, pubkey (sender), created_at, content, tags. The gateway decides
        which table(s) to touch; it never decrypts content.
        """

    @abc.abstractmethod
    def get_conversations(self, limit: int) -> list:
        """Return up to `limit` conversations, newest activity first.

        Each row: {peer_pk, last_event_id, last_ts, unread_count}.
        """

    @abc.abstractmethod
    def get_thread(self, peer_pubkey: str, limit: int, before_ts: int | None) -> list:
        """Return messages for a conversation, newest first.

        `before_ts=None` returns the newest page; pass the timestamp of the
        oldest message from the previous page to fetch the next OLDER page.
        """

    @abc.abstractmethod
    def mark_read(self, peer_pubkey: str) -> None:
        """Zero unread_count for a conversation (thread opened)."""

    def update_content(self, event_id: str, plaintext: str) -> None:
        """Replace a stored message's content with decrypted plaintext.

        Non-abstract: the UI writes through after DMService emits
        dm-decrypted so inbox previews and threads show plaintext instead of
        the raw NIP-44 payload. Gateways that don't support rewrite may leave
        this unimplemented (no-op default keeps older fakes working).
        """

    def total_unread(self) -> int:
        """SUM(unread_count) across conversations — the sidebar badge query."""
        return 0

    @abc.abstractmethod
    def close(self) -> None:
        """Close the underlying connection."""


class SQLiteDMGateway(AbstractDMGateway):
    """SQLite-backed DM gateway. Backs gnostr.db (same file as the rest of the
    app). Threaded lock guards every op; parameterized queries throughout."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or os.path.join(GLib.get_user_data_dir(), "gnostr.db")
        self.conn = None
        self.lock = threading.Lock()
        self.init_db()

    def init_db(self) -> None:
        try:
            if not os.path.exists(os.path.dirname(self.db_path)):
                os.makedirs(os.path.dirname(self.db_path))

            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = self.conn.cursor()

            # Conversations: one row per peer, keyed by their pubkey.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS dm_conversations (
                    peer_pk TEXT PRIMARY KEY,
                    last_event_id TEXT,
                    last_ts INTEGER,
                    unread_count INTEGER NOT NULL DEFAULT 0
                )
                """)

            # Messages: one row per stored (unwrapped) DM.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS dm_messages (
                    id TEXT PRIMARY KEY,
                    peer_pk TEXT NOT NULL,
                    sender_pk TEXT NOT NULL,
                    content TEXT,
                    rumor_ts INTEGER,
                    inserted_at INTEGER NOT NULL,
                    direction TEXT NOT NULL
                )
                """)

            # Indexes for the hot paths: conversation listing (activity order)
            # and thread paging (timestamp order).
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_dm_conversations_activity "
                "ON dm_conversations(last_ts DESC, peer_pk)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_dm_messages_peer_ts "
                "ON dm_messages(peer_pk, rumor_ts DESC)"
            )

            self.conn.commit()
            print(f"✅ DM Gateway initialized at: {self.db_path}")
        except Exception as e:
            print(f"❌ DM Gateway Init Error: {e}")
            traceback.print_exc()

    # ------------------------------------------------------------------
    # save_event
    # ------------------------------------------------------------------

    def save_event(self, event: dict) -> None:
        """Persist a DM rumor/wrap and roll up the conversation row.

        A rumor addressed to us (sender != us) is inbound: it creates the
        conversation row and bumps unread_count. A rumor we authored (sender ==
        us) is outbound: it creates the conversation row but leaves unread at 0.
        Self-authored rumors are still stored as messages so our own history is
        preserved.
        """
        if not self.conn:
            return
        with self.lock:
            try:
                cursor = self.conn.cursor()
                sender = event.get("pubkey")
                is_inbound = sender != self._our_pubkey()
                rumor_ts = int(event.get("created_at", 0))
                # Outbound peer: our own rumor must live in the RECIPIENT's
                # conversation (p-tag), not under our own pubkey — otherwise
                # the thread view can't show our sent messages next to the
                # peer's. Inbound keeps sender as peer. Falls back to sender
                # when no p-tag is present (legacy rows / headless tests).
                peer = sender
                if not is_inbound:
                    for tag in event.get("tags") or []:
                        if isinstance(tag, (list, tuple)) and len(tag) >= 2 and tag[0] == "p":
                            peer = tag[1]
                            break

                # Conversation row: upsert on peer_pk, roll up last activity.
                # `is_inbound` is computed in Python (SQLite's `excluded`
                # pseudo-table only carries the columns we name), so the CASE
                # reads the passed-in flag rather than a phantom column.
                cursor.execute(
                    """
                    INSERT INTO dm_conversations (peer_pk, last_event_id, last_ts, unread_count)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(peer_pk) DO UPDATE SET
                        last_event_id=excluded.last_event_id,
                        last_ts=excluded.last_ts,
                        unread_count = CASE WHEN ? THEN
                            dm_conversations.unread_count + 1
                            ELSE dm_conversations.unread_count END
                    """,
                    (
                        peer,
                        event.get("id"),
                        rumor_ts,
                        1 if is_inbound else 0,
                        1 if is_inbound else 0,
                    ),
                )

                # Message row: store the unwrapped plaintext locally only.
                cursor.execute(
                    """
                    INSERT INTO dm_messages
                        (id, peer_pk, sender_pk, content, rumor_ts, inserted_at, direction)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.get("id"),
                        peer,
                        sender,
                        event.get("content"),
                        rumor_ts,
                        int(time.time()),
                        "inbound" if is_inbound else "outbound",
                    ),
                )
                self.conn.commit()
            except Exception as e:
                print(f"⚠️ DM Gateway Save Event Error: {e}")
                traceback.print_exc()

    # ------------------------------------------------------------------
    # get_conversations
    # ------------------------------------------------------------------

    def get_conversations(self, limit: int) -> list:
        """Return up to `limit` conversations, newest activity first.

        Each row carries a `preview` — the last message's stored content
        (plaintext once DMService writes it back, raw payload until then).
        """
        if not self.conn:
            return []
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT c.peer_pk, c.last_event_id, c.last_ts, c.unread_count, "
                "m.content "
                "FROM dm_conversations c "
                "LEFT JOIN dm_messages m ON m.id = c.last_event_id "
                "ORDER BY c.last_ts DESC, c.peer_pk LIMIT ?",
                (limit,),
            )
            return [
                {
                    "peer_pk": row[0],
                    "last_event_id": row[1],
                    "last_ts": row[2],
                    "unread_count": row[3],
                    "preview": row[4] or "",
                }
                for row in cursor.fetchall()
            ]

    # ------------------------------------------------------------------
    # get_thread
    # ------------------------------------------------------------------

    def get_thread(self, peer_pubkey: str, limit: int, before_ts: int | None) -> list:
        """Return messages for a conversation, newest first.

        `before_ts=None` returns the newest page. Pass the timestamp of the
        oldest message from the previous page to fetch the next OLDER page.
        Uses the peer_ts index (never OFFSET).
        """
        if not self.conn:
            return []
        with self.lock:
            cursor = self.conn.cursor()
            if before_ts is None:
                cursor.execute(
                    "SELECT id, sender_pk, content, rumor_ts FROM dm_messages "
                    "WHERE peer_pk = ? ORDER BY rumor_ts DESC LIMIT ?",
                    (peer_pubkey, limit),
                )
            else:
                cursor.execute(
                    "SELECT id, sender_pk, content, rumor_ts FROM dm_messages "
                    "WHERE peer_pk = ? AND rumor_ts < ? "
                    "ORDER BY rumor_ts DESC LIMIT ?",
                    (peer_pubkey, before_ts, limit),
                )
            return [
                {
                    "id": row[0],
                    "sender_pk": row[1],
                    "content": row[2],
                    "rumor_ts": row[3],
                }
                for row in cursor.fetchall()
            ]

    # ------------------------------------------------------------------
    # mark_read
    # ------------------------------------------------------------------

    def mark_read(self, peer_pubkey: str) -> None:
        """Zero unread_count for a conversation (thread opened)."""
        if not self.conn:
            return
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    "UPDATE dm_conversations SET unread_count = 0 WHERE peer_pk = ?",
                    (peer_pubkey,),
                )
                self.conn.commit()
            except Exception as e:
                print(f"⚠️ DM Gateway Mark Read Error: {e}")

    # ------------------------------------------------------------------
    # update_content / total_unread
    # ------------------------------------------------------------------

    def update_content(self, event_id: str, plaintext: str) -> None:
        """Overwrite a stored message's content with decrypted plaintext.

        Called by the UI after dm-decrypted so previews/threads are readable.
        No-op when the row is gone (race with a DB reset).
        """
        if not self.conn:
            return
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    "UPDATE dm_messages SET content = ? WHERE id = ?",
                    (plaintext, event_id),
                )
                self.conn.commit()
            except Exception as e:
                print(f"⚠️ DM Gateway Update Content Error: {e}")

    def total_unread(self) -> int:
        """SUM(unread_count) across conversations — the sidebar badge query."""
        if not self.conn:
            return 0
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT COALESCE(SUM(unread_count), 0) FROM dm_conversations")
            return int(cursor.fetchone()[0])

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _our_pubkey(self) -> str:
        """Pubkey of the local user. Overridable in tests; defaults to empty
        so save_event treats every stored rumor as inbound by default."""
        return getattr(self, "_our_pubkey_override", "")

    def set_our_pubkey(self, pubkey: str) -> None:
        """Set the local user's pubkey (used by tests and by the caller to
        distinguish inbound vs. outbound rumors)."""
        self._our_pubkey_override = pubkey

    def close(self) -> None:
        """Close the underlying connection."""
        with self.lock:
            if self.conn is not None:
                try:
                    self.conn.close()
                except Exception as e:
                    print(f"⚠️ DM Gateway Close Error: {e}")
                finally:
                    self.conn = None


if __name__ == "__main__":
    gw = SQLiteDMGateway()
    gw.set_our_pubkey("owner")
    gw.save_event(
        {
            "id": "evt1",
            "pubkey": "peerA",
            "created_at": 100,
            "content": "hi",
            "tags": [],
        }
    )
    gw.save_event(
        {
            "id": "evt2",
            "pubkey": "owner",
            "created_at": 101,
            "content": "hello",
            "tags": [],
        }
    )
    print("conversations:", gw.get_conversations(10))
    print("thread peerA:", gw.get_thread("peerA", 10, None))
    gw.mark_read("peerA")
    print("after mark_read:", gw.get_conversations(10))
    gw.close()
