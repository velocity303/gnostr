import sqlite3
import os
import json
import time
import threading
import traceback
from gi.repository import GLib


class Database:
    def __init__(self):
        self.data_dir = GLib.get_user_data_dir()
        self.db_path = os.path.join(self.data_dir, "gnostr.db")
        self.conn = None
        self.lock = threading.Lock()
        self.init_db()

    def init_db(self):
        try:
            if not os.path.exists(self.data_dir):
                os.makedirs(self.data_dir)

            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)

            # gnostr-startup-2 (startup hang fix #2): journal_mode=WAL lets
            # readers — the main thread's switch_feed feed queries — run
            # CONCURRENTLY with the relay ingestion writes; the legacy
            # rollback journal serialized every read behind every INSERT.
            # synchronous=NORMAL is safe under WAL (only checkpoints fsync;
            # a crash loses at most the un-checkpointed WAL tail, and any
            # lost event is simply re-fetched from the relays). busy_timeout
            # rides out write contention with the DM gateway's second
            # connection to this file. journal_mode is PERSISTENT (stored in
            # the DB header), so every later connection — gateway included —
            # gets WAL without re-setting the pragma.
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA busy_timeout=5000")

            cursor = self.conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    pubkey TEXT,
                    created_at INTEGER,
                    kind INTEGER,
                    content TEXT,
                    tags TEXT,
                    sig TEXT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS profiles (
                    pubkey TEXT PRIMARY KEY,
                    name TEXT,
                    display_name TEXT,
                    about TEXT,
                    picture TEXT,
                    website TEXT,
                    banner TEXT,
                    nip05 TEXT,
                    lud16 TEXT,
                    bot INTEGER,
                    birthday TEXT,
                    raw_json TEXT,
                    updated_at INTEGER
                )
            """)

            # Migrate pre-existing profiles tables (CREATE IF NOT EXISTS won't
            # add columns to an existing table). NIP-01/NIP-24 metadata fields.
            cols = {r[1] for r in cursor.execute("PRAGMA table_info(profiles)")}
            for col, decl in (
                ("website", "TEXT"),
                ("banner", "TEXT"),
                ("nip05", "TEXT"),
                ("lud16", "TEXT"),
                ("bot", "INTEGER"),
                ("birthday", "TEXT"),
                ("raw_json", "TEXT"),
                # NIP-39 external identities + NIP-58 profile badges (JSON lists)
                ("external_identities", "TEXT"),
                ("profile_badges", "TEXT"),
            ):
                if col not in cols:
                    cursor.execute(f"ALTER TABLE profiles ADD COLUMN {col} {decl}")

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS following (
                    owner_pubkey TEXT,
                    followed_pubkey TEXT,
                    UNIQUE(owner_pubkey, followed_pubkey)
                )
            """)

            # NIP-58 badge definitions (kind 30009), keyed by addressable coordinate
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS badge_definitions (
                    coordinate TEXT PRIMARY KEY,
                    pubkey TEXT,
                    d_tag TEXT,
                    name TEXT,
                    image TEXT,
                    description TEXT,
                    updated_at INTEGER
                )
            """)

            # Zaps (spec zaps-payments-v1 §3.3/§2.5): paid-invoice set for the
            # double-pay guard, and the append-only monthly budget ledger.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS paid_invoices (
                    bolt11 TEXT PRIMARY KEY,
                    paid_at INTEGER,
                    amount_msat INTEGER
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS zap_ledger (
                    month TEXT,
                    bolt11 TEXT,
                    sats INTEGER
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_zap_ledger_month "
                "ON zap_ledger(month)"
            )

            # Zaps-B (spec zaps-payments-v1 §1.4): verified 9735 receipts.
            # event_id PK dedupes relay echoes (same event id); split
            # payments are distinct rows summed by the aggregate queries.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS zaps (
                    event_id TEXT PRIMARY KEY,
                    bolt11 TEXT,
                    request_id TEXT,
                    sender TEXT,
                    recipient TEXT,
                    target_event TEXT,
                    msat INTEGER,
                    comment TEXT,
                    ts INTEGER
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_zaps_target "
                "ON zaps(target_event)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_zaps_recipient "
                "ON zaps(recipient, ts)"
            )
            # Rule 6 dedupe key: one receipt per bolt11 even if a relay
            # re-echoed it as a differently-signed event.
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_zaps_bolt11 "
                "ON zaps(bolt11)"
            )

            # trust-shield-v1 §5.2: NIP-85 kind-30382 provider ranks.
            # rank_updated = the rank event's created_at (provider's clock,
            # R6 selection key); fetched_at = our wall clock (staleness).
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS trust_ranks (
                    provider TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    rank INTEGER NOT NULL CHECK (rank BETWEEN 0 AND 100),
                    rank_updated INTEGER NOT NULL,
                    fetched_at INTEGER NOT NULL,
                    PRIMARY KEY (provider, subject)
                )
                """
            )

            # Indexes for feed queries (JOIN on pubkey + ORDER BY created_at).
            # Without these, feed loads full-table-scan on a growing DB.
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_feed "
                "ON events(pubkey, kind, created_at)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_created " "ON events(created_at)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_following_owner "
                "ON following(owner_pubkey)"
            )

            self.conn.commit()
            print(f"✅ Database initialized at: {self.db_path}")
        except Exception as e:
            print(f"❌ Database Init Error: {e}")
            traceback.print_exc()

    def save_event(self, event):
        """Persist a single event. Thin wrapper over save_events so every
        existing caller (publish echo, DM rumor row, backfill helpers) keeps
        its exact behavior; the batching lives in client._buffer_event."""
        self.save_events([event])

    def save_events(self, events):
        """Persist a batch of events with ONE executemany + ONE commit
        (gnostr-startup-2: the relay ingestion burst used to mean one
        fsync per event while the main thread's feed read waited on the
        same lock — the startup hang). INSERT OR IGNORE dedupes both relay
        echoes inside a batch and rows already in the table. Atomic: a
        mid-batch failure rolls the whole batch back rather than leaving a
        partial transaction for the next commit to publish. Malformed rows
        (missing keys) are skipped with a log line, same as save_event."""
        if not self.conn or not events:
            return
        rows = []
        for event in events:
            try:
                rows.append(
                    (
                        event["id"],
                        event["pubkey"],
                        event["created_at"],
                        event["kind"],
                        event["content"],
                        json.dumps(event["tags"]),
                        event["sig"],
                    )
                )
            except Exception as e:
                print(f"⚠️ DB Save Events skipped malformed event: {e}")
        if not rows:
            return
        with self.lock:
            try:
                self.conn.executemany(
                    """
                    INSERT OR IGNORE INTO events (id, pubkey, created_at, kind, content, tags, sig)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                    rows,
                )
                self.conn.commit()
            except Exception as e:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                print(f"⚠️ DB Save Events Error: {e}")

    def is_invoice_paid(self, bolt11):
        """Double-pay guard (spec zaps-payments-v1 §3.3): True when this
        bolt11 was already paid through this app."""
        if not self.conn:
            return False
        with self.lock:
            try:
                cur = self.conn.cursor()
                cur.execute("SELECT 1 FROM paid_invoices WHERE bolt11 = ?", (bolt11,))
                return cur.fetchone() is not None
            except Exception as e:
                print(f"⚠️ DB is_invoice_paid Error: {e}")
                return False

    def record_zap_payment(self, bolt11, month, sats, amount_msat):
        """Persist a settled pay: paid_invoices set + append-only zap_ledger
        row for the monthly budget (spec §2.5/§3.3). Ledger is append-only:
        a re-record of the same bolt11 updates paid_invoices but never
        double-counts the ledger (guarded by is_invoice_paid upstream; a
        duplicate here is a no-op)."""
        if not self.conn:
            return
        with self.lock:
            try:
                cur = self.conn.cursor()
                cur.execute("SELECT 1 FROM paid_invoices WHERE bolt11 = ?", (bolt11,))
                if cur.fetchone() is not None:
                    return
                now = int(time.time())
                cur.execute(
                    "INSERT INTO paid_invoices (bolt11, paid_at, amount_msat) "
                    "VALUES (?, ?, ?)",
                    (bolt11, now, amount_msat),
                )
                cur.execute(
                    "INSERT INTO zap_ledger (month, bolt11, sats) VALUES (?, ?, ?)",
                    (month, bolt11, sats),
                )
                self.conn.commit()
            except Exception as e:
                print(f"⚠️ DB record_zap_payment Error: {e}")

    def zap_month_spent(self, month):
        """Σ sats recorded for a calendar month ('YYYY-MM')."""
        if not self.conn:
            return 0
        with self.lock:
            try:
                cur = self.conn.cursor()
                cur.execute(
                    "SELECT COALESCE(SUM(sats), 0) FROM zap_ledger WHERE month = ?",
                    (month,),
                )
                return int(cur.fetchone()[0])
            except Exception as e:
                print(f"⚠️ DB zap_month_spent Error: {e}")
                return 0

    # --- received zaps (Zaps-B, spec zaps-payments-v1 §1.4) --------------

    def save_zap(self, info):
        """Persist a *verified* 9735 receipt (verify_9735 output dict).
        INSERT OR IGNORE on both dedupe keys: event_id (relay echo of the
        same event) and bolt11 (rule 6: one receipt per zap request)."""
        if not self.conn:
            return
        with self.lock:
            try:
                cur = self.conn.cursor()
                cur.execute(
                    """
                    INSERT OR IGNORE INTO zaps
                        (event_id, bolt11, request_id, sender, recipient,
                         target_event, msat, comment, ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        info["event_id"],
                        info.get("bolt11"),
                        info["request_id"],
                        info["sender"],
                        info["recipient"],
                        info.get("target_e"),
                        info["amount_msat"],
                        info.get("comment", ""),
                        int(info.get("ts") or 0) or int(time.time()),
                    ),
                )
                self.conn.commit()
            except Exception as e:
                print(f"⚠️ DB save_zap Error: {e}")

    def zap_totals_for_events(self, event_ids):
        """{event_id: msat} over verified receipts (split receipts summed).
        Events with no zaps are absent from the map."""
        if not self.conn or not event_ids:
            return {}
        with self.lock:
            try:
                cur = self.conn.cursor()
                marks = ",".join("?" * len(event_ids))
                cur.execute(
                    f"SELECT target_event, SUM(msat) FROM zaps "
                    f"WHERE target_event IN ({marks}) GROUP BY target_event",
                    list(event_ids),
                )
                return {row[0]: int(row[1]) for row in cur.fetchall()}
            except Exception as e:
                print(f"⚠️ DB zap_totals_for_events Error: {e}")
                return {}

    def zap_totals_for_profiles(self, pubkeys):
        """{recipient_pubkey: msat} over verified receipts."""
        if not self.conn or not pubkeys:
            return {}
        with self.lock:
            try:
                cur = self.conn.cursor()
                marks = ",".join("?" * len(pubkeys))
                cur.execute(
                    f"SELECT recipient, SUM(msat) FROM zaps "
                    f"WHERE recipient IN ({marks}) GROUP BY recipient",
                    list(pubkeys),
                )
                return {row[0]: int(row[1]) for row in cur.fetchall()}
            except Exception as e:
                print(f"⚠️ DB zap_totals_for_profiles Error: {e}")
                return {}

    def zaps_for_event(self, event_id):
        """Receipt rows for one target event, newest first:
        [{sender, msat, comment, ts}, ...] — the tap-through list."""
        if not self.conn:
            return []
        with self.lock:
            try:
                cur = self.conn.cursor()
                cur.execute(
                    "SELECT sender, msat, comment, ts FROM zaps "
                    "WHERE target_event = ? ORDER BY ts DESC",
                    (event_id,),
                )
                return [
                    {
                        "sender": r[0],
                        "msat": int(r[1]),
                        "comment": r[2] or "",
                        "ts": int(r[3]),
                    }
                    for r in cur.fetchall()
                ]
            except Exception as e:
                print(f"⚠️ DB zaps_for_event Error: {e}")
                return []

    def zaps_received(self, pubkey, limit=50):
        """Recent incoming zaps to `pubkey` (own-received view, newest
        first): [{sender, msat, comment, ts, target_event}, ...]."""
        if not self.conn:
            return []
        with self.lock:
            try:
                cur = self.conn.cursor()
                cur.execute(
                    "SELECT sender, msat, comment, ts, target_event FROM zaps "
                    "WHERE recipient = ? ORDER BY ts DESC LIMIT ?",
                    (pubkey, limit),
                )
                return [
                    {
                        "sender": r[0],
                        "msat": int(r[1]),
                        "comment": r[2] or "",
                        "ts": int(r[3]),
                        "target_event": r[4],
                    }
                    for r in cur.fetchall()
                ]
            except Exception as e:
                print(f"⚠️ DB zaps_received Error: {e}")
                return []

    def save_profile(self, pubkey, content_json, created_at):
        if not self.conn:
            return
        try:
            data = json.loads(content_json)
            name = data.get("name", "")
            display_name = data.get("display_name", "")
            about = data.get("about", "")
            picture = data.get("picture", "")
            website = data.get("website", "")
            banner = data.get("banner", "")
            nip05 = data.get("nip05", "")
            lud16 = data.get("lud16", "")
            bot = 1 if data.get("bot") else 0
            birthday = (
                json.dumps(data["birthday"])
                if isinstance(data.get("birthday"), dict)
                else ""
            )

            with self.lock:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO profiles (pubkey, name, display_name, about, picture,
                        website, banner, nip05, lud16, bot, birthday, raw_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(pubkey) DO UPDATE SET
                        name=excluded.name,
                        display_name=excluded.display_name,
                        about=excluded.about,
                        picture=excluded.picture,
                        website=excluded.website,
                        banner=excluded.banner,
                        nip05=excluded.nip05,
                        lud16=excluded.lud16,
                        bot=excluded.bot,
                        birthday=excluded.birthday,
                        raw_json=excluded.raw_json,
                        updated_at=excluded.updated_at
                    WHERE excluded.updated_at > profiles.updated_at
                """,
                    (
                        pubkey,
                        name,
                        display_name,
                        about,
                        picture,
                        website,
                        banner,
                        nip05,
                        lud16,
                        bot,
                        birthday,
                        content_json,
                        created_at,
                    ),
                )
                self.conn.commit()
        except Exception as e:
            print(f"⚠️ DB Save Profile Error: {e}")

    def get_profile(self, pubkey):
        if not self.conn:
            return None
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT name, display_name, about, picture, website, banner, "
                "nip05, lud16, bot, birthday, raw_json FROM profiles WHERE pubkey = ?",
                (pubkey,),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "name": row[0],
                    "display_name": row[1],
                    "about": row[2],
                    "picture": row[3],
                    "website": row[4],
                    "banner": row[5],
                    "nip05": row[6],
                    "lud16": row[7],
                    "bot": row[8],
                    "birthday": row[9],
                    "raw_json": row[10],
                }
            return None

    def save_external_identities(self, pubkey, identities):
        """Store NIP-39 external identities (list of dicts) as JSON in the
        dedicated external_identities column. Creates the row if absent."""
        if not self.conn:
            return
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    "INSERT INTO profiles (pubkey, updated_at) VALUES (?, 0) "
                    "ON CONFLICT(pubkey) DO NOTHING",
                    (pubkey,),
                )
                cursor.execute(
                    "UPDATE profiles SET external_identities = ? WHERE pubkey = ?",
                    (json.dumps(identities), pubkey),
                )
                self.conn.commit()
            except Exception as e:
                print(f"⚠️ DB External Identities Error: {e}")

    def get_external_identities(self, pubkey):
        """Return the stored NIP-39 external identities for a pubkey."""
        if not self.conn:
            return []
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT external_identities FROM profiles WHERE pubkey = ?",
                (pubkey,),
            )
            row = cursor.fetchone()
            if not row or not row[0]:
                return []
            try:
                data = json.loads(row[0])
                return data if isinstance(data, list) else []
            except Exception:
                return []

    def save_profile_badges(self, pubkey, badges):
        """Store NIP-58 profile badges (list of (a,e) pairs) as JSON in the
        dedicated profile_badges column. Creates the row if absent."""
        if not self.conn:
            return
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    "INSERT INTO profiles (pubkey, updated_at) VALUES (?, 0) "
                    "ON CONFLICT(pubkey) DO NOTHING",
                    (pubkey,),
                )
                cursor.execute(
                    "UPDATE profiles SET profile_badges = ? WHERE pubkey = ?",
                    (json.dumps(badges), pubkey),
                )
                self.conn.commit()
            except Exception as e:
                print(f"⚠️ DB Profile Badges Error: {e}")

    def get_profile_badges(self, pubkey):
        """Return the stored NIP-58 profile badges for a pubkey."""
        if not self.conn:
            return []
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT profile_badges FROM profiles WHERE pubkey = ?", (pubkey,)
            )
            row = cursor.fetchone()
            if not row or not row[0]:
                return []
            try:
                data = json.loads(row[0])
                return data if isinstance(data, list) else []
            except Exception:
                return []

    def save_badge_definition(self, event):
        """Store a NIP-58 badge definition (kind 30009) so badge images can be
        resolved later. Keyed by its addressable coordinate (kind:pubkey:d-tag)."""
        if not self.conn:
            return
        d_tag = ""
        for t in event.get("tags", []):
            if len(t) >= 2 and t[0] == "d":
                d_tag = t[1]
                break
        if not d_tag:
            return
        coordinate = f"30009:{event['pubkey']}:{d_tag}"
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    "INSERT INTO badge_definitions (coordinate, pubkey, d_tag, "
                    "name, image, description, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(coordinate) DO UPDATE SET "
                    "name=excluded.name, image=excluded.image, "
                    "description=excluded.description, updated_at=excluded.updated_at",
                    (
                        coordinate,
                        event["pubkey"],
                        d_tag,
                        self._tag_value(event, "name"),
                        self._tag_value(event, "image"),
                        self._tag_value(event, "description"),
                        event["created_at"],
                    ),
                )
                self.conn.commit()
            except Exception as e:
                print(f"⚠️ DB Badge Definition Error: {e}")

    def get_badge_definition(self, coordinate):
        """Return a stored NIP-58 badge definition by coordinate, or None."""
        if not self.conn:
            return None
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT coordinate, pubkey, d_tag, name, image, description "
                "FROM badge_definitions WHERE coordinate = ?",
                (coordinate,),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "coordinate": row[0],
                    "pubkey": row[1],
                    "d_tag": row[2],
                    "name": row[3],
                    "image": row[4],
                    "description": row[5],
                }
            return None

    @staticmethod
    def _tag_value(event, key):
        for t in event.get("tags", []):
            if len(t) >= 2 and t[0] == key:
                return t[1]
        return ""

    def get_event_by_id(self, event_id):
        """Fetch a single event by ID."""
        if not self.conn:
            return None
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM events WHERE id = ?", (event_id,))
            rows = cursor.fetchall()
            events = self._rows_to_events(rows)
            return events[0] if events else None

    def get_event_by_a(self, kind, pubkey, d_tag):
        """Fetch the latest addressable event by its 'a'-tag coordinate
        (kind:pubkey:d-tag, NIP-33). Tags are stored as JSON text, so the
        kind+pubkey index narrows candidates, then the d-tag is matched in
        Python. Returns the newest match (addressable events are replaceable)."""
        if not self.conn:
            return None
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT * FROM events WHERE kind = ? AND pubkey = ? ORDER BY created_at DESC",
                (kind, pubkey),
            )
            events = self._rows_to_events(cursor.fetchall())
            for ev in events:
                for t in ev.get("tags", []):
                    if len(t) >= 2 and t[0] == "d" and t[1] == d_tag:
                        return ev
            return None

    def get_feed_for_user(self, pubkey, limit=50):
        if not self.conn:
            return []
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                SELECT * FROM events
                WHERE pubkey = ? AND kind = 1
                ORDER BY created_at DESC LIMIT ?
            """,
                (pubkey, limit),
            )
            return self._rows_to_events(cursor.fetchall())

    def get_feed_following(self, owner_pubkey, limit=50, before=None, after=None):
        """Feed events from authors the owner follows, newest first.

        `before=None, after=None` fetches the first (newest) page. Pass
        `before=(created_at, id)` of the last event from the previous page
        to fetch the next page OLDER: keyset pagination on `(created_at,
        id)` — never OFFSET, which both degrades with depth and double-
        serves rows when new events arrive mid-scroll. The `id` tiebreaker
        keeps pages stable when two events share a created_at. A page
        shorter than `limit` signals end-of-DB.

        Pass `after=(created_at, id)` to fetch the page NEWER than a
        given event (the reverse direction, used for the sliding window's
        re-pull when the user scrolls back up to the top). Mutually
        exclusive with `before`; if both are set, `after` wins.
        """
        if not self.conn:
            return []
        with self.lock:
            cursor = self.conn.cursor()
            if after is not None:
                after_created_at, after_id = after
                cursor.execute(
                    """
                    SELECT e.* FROM events e
                    INNER JOIN following f ON e.pubkey = f.followed_pubkey
                    WHERE f.owner_pubkey = ? AND e.kind = 1
                      AND (e.created_at, e.id) > (?, ?)
                    ORDER BY e.created_at DESC, e.id DESC LIMIT ?
                """,
                    (owner_pubkey, after_created_at, after_id, limit),
                )
            elif before is None:
                cursor.execute(
                    """
                    SELECT e.* FROM events e
                    INNER JOIN following f ON e.pubkey = f.followed_pubkey
                    WHERE f.owner_pubkey = ? AND e.kind = 1
                    ORDER BY e.created_at DESC, e.id DESC LIMIT ?
                """,
                    (owner_pubkey, limit),
                )
            else:
                before_created_at, before_id = before
                cursor.execute(
                    """
                    SELECT e.* FROM events e
                    INNER JOIN following f ON e.pubkey = f.followed_pubkey
                    WHERE f.owner_pubkey = ? AND e.kind = 1
                      AND (e.created_at, e.id) < (?, ?)
                    ORDER BY e.created_at DESC, e.id DESC LIMIT ?
                """,
                    (owner_pubkey, before_created_at, before_id, limit),
                )
            return self._rows_to_events(cursor.fetchall())

    def get_feed_global(self, limit=50, before=None, after=None, pubkey=None):
        """All kind-1 events newest first (optionally one author's only).

        Same keyset-pagination contract as get_feed_following — see its
        docstring; the (created_at, id) tuple comparison and short-page
        end-of-DB signal are identical. `pubkey` narrows to a single
        author (the "me" feed); None means every author (global feed).
        No JOIN, so idx_events_created carries the ORDER BY; the kind
        filter runs on the fetched rows' window and stays cheap at our
        row counts.
        """
        if not self.conn:
            return []
        with self.lock:
            cursor = self.conn.cursor()
            author_sql = " AND pubkey = ?" if pubkey is not None else ""
            author_args = (pubkey,) if pubkey is not None else ()
            if after is not None:
                after_created_at, after_id = after
                cursor.execute(
                    f"""
                    SELECT * FROM events
                    WHERE kind = 1{author_sql}
                      AND (created_at, id) > (?, ?)
                    ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                    author_args + (after_created_at, after_id, limit),
                )
            elif before is None:
                cursor.execute(
                    f"""
                    SELECT * FROM events
                    WHERE kind = 1{author_sql}
                    ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                    author_args + (limit,),
                )
            else:
                before_created_at, before_id = before
                cursor.execute(
                    f"""
                    SELECT * FROM events
                    WHERE kind = 1{author_sql}
                      AND (created_at, id) < (?, ?)
                    ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                    author_args + (before_created_at, before_id, limit),
                )
            return self._rows_to_events(cursor.fetchall())

    def save_contacts(self, owner_pubkey, followed_pubkeys):
        if not self.conn:
            return
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    "DELETE FROM following WHERE owner_pubkey = ?", (owner_pubkey,)
                )
                data = [(owner_pubkey, pk) for pk in followed_pubkeys]
                cursor.executemany(
                    "INSERT OR IGNORE INTO following (owner_pubkey, followed_pubkey) VALUES (?, ?)",
                    data,
                )
                self.conn.commit()
            except Exception as e:
                print(f"⚠️ DB Contact Save Error: {e}")

    def get_following_list(self, owner_pubkey):
        if not self.conn:
            return []
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT followed_pubkey FROM following WHERE owner_pubkey = ?",
                (owner_pubkey,),
            )
            return [row[0] for row in cursor.fetchall()]

    def set_following(self, owner_pubkey, pubkey, following):
        """Add or remove a single followed pubkey for an owner (targeted toggle)."""
        if not self.conn:
            return
        with self.lock:
            try:
                cursor = self.conn.cursor()
                if following:
                    cursor.execute(
                        "INSERT OR IGNORE INTO following (owner_pubkey, followed_pubkey) VALUES (?, ?)",
                        (owner_pubkey, pubkey),
                    )
                else:
                    cursor.execute(
                        "DELETE FROM following WHERE owner_pubkey = ? AND followed_pubkey = ?",
                        (owner_pubkey, pubkey),
                    )
                self.conn.commit()
            except Exception as e:
                print(f"⚠️ DB Following Error: {e}")

    def get_replies(self, parent_event_id, limit=50):
        """Fetch replies to a specific event (kind 1 events with an 'e' tag referencing parent)."""
        if not self.conn:
            return []
        with self.lock:
            cursor = self.conn.cursor()
            # Filter by the parent BEFORE applying LIMIT — the old code applied
            # LIMIT to the newest 50 events globally then filtered, so old
            # replies to a thread were silently dropped (thread looked incomplete).
            # tags is stored as JSON text; the parent event id appears verbatim
            # as a JSON string value, so a LIKE match narrows candidates first.
            cursor.execute(
                "SELECT * FROM events "
                "WHERE kind = 1 AND tags LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (f"%{parent_event_id}%", limit),
            )
            rows = cursor.fetchall()
            events = self._rows_to_events(rows)
            # Filter precisely for an 'e' tag referencing parent_event_id.
            replies = []
            for ev in events:
                tags = ev.get("tags", [])
                for t in tags:
                    if len(t) >= 2 and t[0] == "e" and t[1] == parent_event_id:
                        replies.append(ev)
                        break
            return replies

    def _rows_to_events(self, rows):
        events = []
        for row in rows:
            try:
                events.append(
                    {
                        "id": row[0],
                        "pubkey": row[1],
                        "created_at": row[2],
                        "kind": row[3],
                        "content": row[4],
                        "tags": json.loads(row[5]),
                        "sig": row[6],
                    }
                )
            except Exception:
                pass
        return events

    def user_reaction(self, target_event_id, pubkey):
        """Return the content ('+'/'-') of `pubkey`'s most recent kind-7
        reaction on `target_event_id`, or None if they haven't reacted. Powers
        the like-toggle: knowing the current user's reaction lets the button
        render filled vs. empty and publish '+' vs '-' to undo."""
        if not self.conn:
            return None
        with self.lock:
            cursor = self.conn.cursor()
            # Narrow with a LIKE on the JSON tag text, then confirm precisely.
            cursor.execute(
                "SELECT content FROM events "
                "WHERE kind = 7 AND pubkey = ? AND tags LIKE ? "
                "ORDER BY created_at DESC LIMIT 1",
                (pubkey, f"%{target_event_id}%"),
            )
            row = cursor.fetchone()
            return row[0] if row else None

    # -- trust-shield-v1 (NIP-85 ranks) ------------------------------------

    def upsert_trust_rank(self, provider, subject, rank, rank_updated):
        """Store/replace one provider rank for one subject (spec §5.2)."""
        if not self.conn:
            return False
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT INTO trust_ranks "
                "(provider, subject, rank, rank_updated, fetched_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(provider, subject) DO UPDATE SET "
                "rank = excluded.rank, rank_updated = excluded.rank_updated, "
                "fetched_at = excluded.fetched_at",
                (provider, subject, int(rank), int(rank_updated), int(time.time())),
            )
            self.conn.commit()
            return True

    def get_trust_rank(self, provider, subject):
        """The stored rank row for (provider, subject) as a dict, or None.
        Freshness/staleness is the caller's decision (trust_shield.is_stale)."""
        if not self.conn:
            return None
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT rank, rank_updated, fetched_at FROM trust_ranks "
                "WHERE provider = ? AND subject = ?",
                (provider, subject),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return {"rank": row[0], "rank_updated": row[1], "fetched_at": row[2]}

    def trust_rank_count(self, provider):
        """Number of cached ranks for a provider (settings status line)."""
        if not self.conn:
            return 0
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM trust_ranks WHERE provider = ?", (provider,)
            )
            return cursor.fetchone()[0]

    def trust_last_fetch(self, provider):
        """Latest fetched_at across a provider's rows (epoch seconds) or None."""
        if not self.conn or not provider:
            return None
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT MAX(fetched_at) FROM trust_ranks WHERE provider = ?",
                (provider,),
            )
            row = cursor.fetchone()
            return row[0] if row and row[0] is not None else None

    def purge_other_trust_ranks(self, provider):
        """Provider switch: drop every other provider's rows (spec §5.2 —
        the read set stays bounded to the active provider)."""
        if not self.conn or not provider:
            return
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM trust_ranks WHERE provider != ?", (provider,))
            self.conn.commit()
