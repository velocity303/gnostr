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

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    pubkey TEXT,
                    created_at INTEGER,
                    kind INTEGER,
                    content TEXT,
                    tags TEXT,
                    sig TEXT
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS profiles (
                    pubkey TEXT PRIMARY KEY,
                    name TEXT,
                    display_name TEXT,
                    about TEXT,
                    picture TEXT,
                    updated_at INTEGER
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS following (
                    owner_pubkey TEXT,
                    followed_pubkey TEXT,
                    UNIQUE(owner_pubkey, followed_pubkey)
                )
            ''')

            self.conn.commit()
            print(f"✅ Database initialized at: {self.db_path}")
        except Exception as e:
            print(f"❌ Database Init Error: {e}")
            traceback.print_exc()

    def save_event(self, event):
        if not self.conn: return
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute('''
                    INSERT OR IGNORE INTO events (id, pubkey, created_at, kind, content, tags, sig)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    event['id'],
                    event['pubkey'],
                    event['created_at'],
                    event['kind'],
                    event['content'],
                    json.dumps(event['tags']),
                    event['sig']
                ))
                self.conn.commit()
            except Exception as e:
                print(f"⚠️ DB Save Event Error: {e}")

    def save_profile(self, pubkey, content_json, created_at):
        if not self.conn: return
        try:
            data = json.loads(content_json)
            name = data.get('name', '')
            display_name = data.get('display_name', '')
            about = data.get('about', '')
            picture = data.get('picture', '')

            with self.lock:
                cursor = self.conn.cursor()
                cursor.execute('''
                    INSERT INTO profiles (pubkey, name, display_name, about, picture, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(pubkey) DO UPDATE SET
                        name=excluded.name,
                        display_name=excluded.display_name,
                        about=excluded.about,
                        picture=excluded.picture,
                        updated_at=excluded.updated_at
                    WHERE excluded.updated_at > profiles.updated_at
                ''', (pubkey, name, display_name, about, picture, created_at))
                self.conn.commit()
        except Exception as e:
            print(f"⚠️ DB Save Profile Error: {e}")

    def get_profile(self, pubkey):
        if not self.conn: return None
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT name, display_name, about, picture FROM profiles WHERE pubkey = ?", (pubkey,))
            row = cursor.fetchone()
            if row:
                return {
                    "name": row[0],
                    "display_name": row[1],
                    "about": row[2],
                    "picture": row[3]
                }
            return None

    def get_event_by_id(self, event_id):
        """Fetch a single event by ID."""
        if not self.conn: return None
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM events WHERE id = ?", (event_id,))
            rows = cursor.fetchall()
            events = self._rows_to_events(rows)
            return events[0] if events else None

    def get_feed_for_user(self, pubkey, limit=50):
        if not self.conn: return []
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT * FROM events
                WHERE pubkey = ? AND kind = 1
                ORDER BY created_at DESC LIMIT ?
            ''', (pubkey, limit))
            return self._rows_to_events(cursor.fetchall())

    def get_feed_following(self, owner_pubkey, limit=50):
        if not self.conn: return []
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT e.* FROM events e
                INNER JOIN following f ON e.pubkey = f.followed_pubkey
                WHERE f.owner_pubkey = ? AND e.kind = 1
                ORDER BY e.created_at DESC LIMIT ?
            ''', (owner_pubkey, limit))
            return self._rows_to_events(cursor.fetchall())

    def save_contacts(self, owner_pubkey, followed_pubkeys):
        if not self.conn: return
        with self.lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute("DELETE FROM following WHERE owner_pubkey = ?", (owner_pubkey,))
                data = [(owner_pubkey, pk) for pk in followed_pubkeys]
                cursor.executemany("INSERT OR IGNORE INTO following (owner_pubkey, followed_pubkey) VALUES (?, ?)", data)
                self.conn.commit()
            except Exception as e:
                print(f"⚠️ DB Contact Save Error: {e}")

    def get_following_list(self, owner_pubkey):
        if not self.conn: return []
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT followed_pubkey FROM following WHERE owner_pubkey = ?", (owner_pubkey,))
            return [row[0] for row in cursor.fetchall()]

    def _rows_to_events(self, rows):
        events = []
        for row in rows:
            try:
                events.append({
                    'id': row[0],
                    'pubkey': row[1],
                    'created_at': row[2],
                    'kind': row[3],
                    'content': row[4],
                    'tags': json.loads(row[5]),
                    'sig': row[6]
                })
            except:
                pass
        return events
