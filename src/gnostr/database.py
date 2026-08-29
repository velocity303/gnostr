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
        if not self.conn:
            return
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO events (id, pubkey, created_at, kind, content, tags, sig)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        event["id"],
                        event["pubkey"],
                        event["created_at"],
                        event["kind"],
                        event["content"],
                        json.dumps(event["tags"]),
                        event["sig"],
                    ),
                )
                self.conn.commit()
            except Exception as e:
                print(f"⚠️ DB Save Event Error: {e}")

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

    def get_feed_following(self, owner_pubkey, limit=50, before=None):
        """Feed events from authors the owner follows, newest first.

        `before=None` fetches the first page. Pass `before=(created_at, id)`
        of the last event from the previous page to fetch the next page:
        keyset pagination on `(created_at, id)` — never OFFSET, which both
        degrades with depth and double-serves rows when new events arrive
        mid-scroll. The `id` tiebreaker keeps pages stable when two events
        share a created_at. A page shorter than `limit` signals end-of-DB.
        """
        if not self.conn:
            return []
        with self.lock:
            cursor = self.conn.cursor()
            if before is None:
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
