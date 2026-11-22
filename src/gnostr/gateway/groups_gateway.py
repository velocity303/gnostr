"""Groups data gateway — SQLite persistence for NIP-29 communities.

Spec nipw-communities-v1 §4 (schema fixed by the ABI). Extends gnostr.db
idempotently, same pattern as SQLiteDMGateway: threaded lock, parameterized
queries, per-op error logging. No relay I/O, no crypto, no UI.

Tables:

    groups (relay TEXT, group_id TEXT, name TEXT, picture TEXT, about TEXT,
        banner TEXT, flags TEXT, supported_kinds TEXT, admins TEXT,
        member_count INTEGER, fetched_at INTEGER,
        PRIMARY KEY (relay, group_id))
    group_events (id TEXT PRIMARY KEY, relay TEXT, group_id TEXT, kind INT,
        pubkey TEXT, created_at INT, content TEXT, tags TEXT)
    group_membership (relay TEXT, group_id TEXT, state TEXT, updated_at INT,
        PRIMARY KEY (relay, group_id))

`group_membership.state` is 'member' | 'pending' — the persisted half of the
join state machine (spec §4: survives restart).
"""

import json
import sqlite3
import threading
import time
import traceback

from gi.repository import GLib


class SQLiteGroupsGateway:
    """Persistence for the group registry, cached group events, and
    membership state. Safe for concurrent use (client threads + main)."""

    def __init__(self, db_path=None):
        self.db_path = db_path or GLib.get_user_data_dir() + "/gnostr/gnostr.db"
        self.lock = threading.Lock()
        self.conn = None

    # -- lifecycle ---------------------------------------------------------

    def init_db(self):
        try:
            import os

            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS groups (
                    relay TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    name TEXT,
                    picture TEXT,
                    about TEXT,
                    banner TEXT,
                    flags TEXT,
                    supported_kinds TEXT,
                    admins TEXT,
                    member_count INTEGER,
                    fetched_at INTEGER,
                    PRIMARY KEY (relay, group_id)
                );
                CREATE TABLE IF NOT EXISTS group_events (
                    id TEXT PRIMARY KEY,
                    relay TEXT,
                    group_id TEXT,
                    kind INTEGER,
                    pubkey TEXT,
                    created_at INTEGER,
                    content TEXT,
                    tags TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_group_events_group
                    ON group_events (relay, group_id, created_at);
                CREATE TABLE IF NOT EXISTS group_membership (
                    relay TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    state TEXT,
                    updated_at INTEGER,
                    PRIMARY KEY (relay, group_id)
                );
                """
            )
            self.conn.commit()
        except Exception:
            traceback.print_exc()

    def close(self):
        with self.lock:
            if self.conn:
                self.conn.close()
                self.conn = None

    # -- registry (groups table) --------------------------------------------

    def upsert_group(self, relay, group_id, **fields):
        """Insert or update metadata for one group. `fields` is any subset of
        name/picture/about/banner/flags/supported_kinds/admins/member_count/
        fetched_at (lists/dicts stored as JSON)."""
        try:
            with self.lock:
                row = self.conn.execute(
                    "SELECT 1 FROM groups WHERE relay=? AND group_id=?",
                    (relay, group_id),
                ).fetchone()
                payload = {
                    k: (
                        json.dumps(v)
                        if k in ("flags", "supported_kinds", "admins") and v is not None
                        else v
                    )
                    for k, v in fields.items()
                    if k
                    in (
                        "name", "picture", "about", "banner", "flags",
                        "supported_kinds", "admins", "member_count", "fetched_at",
                    )
                }
                if row:
                    if payload:
                        sets = ", ".join(f"{k}=?" for k in payload)
                        self.conn.execute(
                            f"UPDATE groups SET {sets} WHERE relay=? AND group_id=?",
                            (*payload.values(), relay, group_id),
                        )
                else:
                    cols = ["relay", "group_id", *payload.keys()]
                    marks = ", ".join("?" for _ in cols)
                    self.conn.execute(
                        f"INSERT INTO groups ({', '.join(cols)}) VALUES ({marks})",
                        (relay, group_id, *payload.values()),
                    )
                self.conn.commit()
        except Exception:
            traceback.print_exc()

    def list_groups(self):
        """All registry rows (metadata may be NULL until preview fetch)."""
        try:
            with self.lock:
                cur = self.conn.execute(
                    "SELECT relay, group_id, name, picture, about, banner, flags,"
                    " supported_kinds, admins, member_count, fetched_at"
                    " FROM groups ORDER BY name COLLATE NOCASE"
                )
                out = []
                for row in cur.fetchall():
                    out.append(self._row_to_group(row))
                return out
        except Exception:
            traceback.print_exc()
            return []

    @staticmethod
    def _row_to_group(row):
        return {
            "relay": row[0],
            "group_id": row[1],
            "name": row[2],
            "picture": row[3],
            "about": row[4],
            "banner": row[5],
            "flags": json.loads(row[6]) if row[6] else [],
            "supported_kinds": json.loads(row[7]) if row[7] else None,
            "admins": json.loads(row[8]) if row[8] else [],
            "member_count": row[9],
            "fetched_at": row[10],
        }

    def get_group(self, relay, group_id):
        """Registry row for ONE group (same shape as list_groups rows), or
        None. NIPW-P3-FIX-3 (Bug A): GroupDetailPage reads this on entry so
        a revisit renders verified metadata immediately instead of waiting
        on a fresh 39000 snapshot that khatru sometimes never replays."""
        try:
            with self.lock:
                row = self.conn.execute(
                    "SELECT relay, group_id, name, picture, about, banner, flags,"
                    " supported_kinds, admins, member_count, fetched_at"
                    " FROM groups WHERE relay=? AND group_id=?",
                    (relay, group_id),
                ).fetchone()
                return self._row_to_group(row) if row else None
        except Exception:
            traceback.print_exc()
            return None

    def remove_group(self, relay, group_id):
        """Leave: drop registry + membership rows. Cached group_events are
        purged separately (purge_left_events) so re-joining can backfill."""
        try:
            with self.lock:
                self.conn.execute(
                    "DELETE FROM groups WHERE relay=? AND group_id=?",
                    (relay, group_id),
                )
                self.conn.execute(
                    "DELETE FROM group_membership WHERE relay=? AND group_id=?",
                    (relay, group_id),
                )
                self.conn.commit()
        except Exception:
            traceback.print_exc()

    # -- cached events -------------------------------------------------------

    def save_event(self, relay, group_id, event):
        """Idempotent upsert of one group-scoped event (timeline or probe)."""
        try:
            with self.lock:
                self.conn.execute(
                    "INSERT OR REPLACE INTO group_events"
                    " (id, relay, group_id, kind, pubkey, created_at, content, tags)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.get("id"),
                        relay,
                        group_id,
                        event.get("kind"),
                        event.get("pubkey"),
                        event.get("created_at"),
                        event.get("content"),
                        json.dumps(event.get("tags") or []),
                    ),
                )
                self.conn.commit()
        except Exception:
            traceback.print_exc()

    def get_events(self, relay, group_id, limit=100, since=None):
        """Newest-first cached events for one group (optionally since ts)."""
        try:
            with self.lock:
                q = (
                    "SELECT id, kind, pubkey, created_at, content, tags"
                    " FROM group_events WHERE relay=? AND group_id=?"
                )
                args = [relay, group_id]
                if since:
                    q += " AND created_at >= ?"
                    args.append(int(since))
                q += " ORDER BY created_at DESC LIMIT ?"
                args.append(int(limit))
                out = []
                for row in self.conn.execute(q, args).fetchall():
                    out.append(
                        {
                            "id": row[0],
                            "kind": row[1],
                            "pubkey": row[2],
                            "created_at": row[3],
                            "content": row[4],
                            "tags": json.loads(row[5]) if row[5] else [],
                        }
                    )
                return out
        except Exception:
            traceback.print_exc()
            return []

    def purge_left_events(self, older_than_days=30):
        """Delete cached events for groups no longer in the registry and older
        than the retention window (spec §4: keep 30 days after leave)."""
        try:
            cutoff = int(time.time()) - older_than_days * 86400
            with self.lock:
                cur = self.conn.execute(
                    "DELETE FROM group_events WHERE created_at < ? AND"
                    " NOT EXISTS (SELECT 1 FROM groups g WHERE g.relay ="
                    " group_events.relay AND g.group_id = group_events.group_id)"
                )
                self.conn.commit()
                return cur.rowcount
        except Exception:
            traceback.print_exc()
            return 0

    # -- membership -----------------------------------------------------------

    def set_membership(self, relay, group_id, state):
        try:
            with self.lock:
                self.conn.execute(
                    "INSERT OR REPLACE INTO group_membership"
                    " (relay, group_id, state, updated_at) VALUES (?, ?, ?, ?)",
                    (relay, group_id, state, int(time.time())),
                )
                self.conn.commit()
        except Exception:
            traceback.print_exc()

    def get_membership(self, relay, group_id):
        """'member' | 'pending' | None."""
        try:
            with self.lock:
                row = self.conn.execute(
                    "SELECT state FROM group_membership WHERE relay=? AND group_id=?",
                    (relay, group_id),
                ).fetchone()
                return row[0] if row else None
        except Exception:
            traceback.print_exc()
            return None
