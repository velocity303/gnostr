import json
import threading
import time
import os
import collections
from gi.repository import GObject, GLib
import traceback
import gnostr
from .util.connection_state import ConnectionState

# Connection status definitions (Name, ColorCode)
STATUS = {
    "CONNECTED": ("🟢 Connected", "#28a745"),  # Green
    "WARNING": ("🟡 Warning/Error", "#ffc107"),  # Yellow
    "DISCONNECTED": ("🔴 Disconnected", "#dc3545"),
}

try:
    import websocket
except ImportError:
    websocket = None


# --- Connection Status Definitions (Color-coded) ---
class ConnectionStatus:
    """Defines standard connection status codes and associated display colors."""

    GREEN = ("Connected", "Green")  # Stable/Operational
    YELLOW = ("Warning", "Yellow")  # Transient issue, e.g., Rate Limiting
    RED = ("Disconnected", "Red")  # Critical failure or no relay connectivity


DEFAULT_RELAYS = ["wss://relay.nostr.band", "wss://nos.lol", "wss://relay.primal.net"]


class NostrRelay(GObject.Object):
    def __init__(self, url, on_event, on_status, on_ok=None):
        super().__init__()
        self.url = url
        self.on_event = on_event
        self.on_status = on_status
        self.on_ok = on_ok or (lambda *a: None)
        self.ws = None
        self.is_connected = False
        self.sub_id = None
        self.active_sub_ids = set()  # sub_ids actually opened on THIS relay
        self.request_queue = []
        self.is_processing_queue = False
        self.snapshot_ids = set()  # Track subscriptions that should close on EOSE

    def start(self):
        def on_msg(ws, m):
            try:
                d = json.loads(m)
                if d[0] == "EVENT":
                    self.on_event(d[2])
                elif d[0] == "OK":
                    # NIP-01: ["OK", <event_id>, <true|false>, <message>]
                    ok = gnostr.nostr_utils.parse_ok_message(d)
                    if ok:
                        self.on_ok(ok[0], ok[1], ok[2], self.url)
                elif d[0] == "EOSE":
                    sub_id = d[1]
                    if sub_id in self.snapshot_ids:
                        # Auto-close snapshot subscription
                        # print(f"DEBUG [{self.url}] Closing Snapshot {sub_id}")
                        self.ws.send(json.dumps(["CLOSE", sub_id]))
                        self.snapshot_ids.remove(sub_id)
                elif d[0] == "NOTICE":
                    print(f"NOTICE [{self.url}]: {d[1]}")
            except Exception:
                print(f"❌ ERROR [{self.url}] Message Handler Failed:")
                print(f"   Msg: {m[:100]}...")
                traceback.print_exc()

        def on_open(ws):
            self.is_connected = True
            GLib.idle_add(self.on_status, self.url, ConnectionState.CONNECTED)
            self.process_queue()

        def on_err(ws, e):
            self.is_connected = False
            GLib.idle_add(self.on_status, self.url, ConnectionState.WARNING)

        def on_close(ws, c, m):
            self.is_connected = False
            GLib.idle_add(self.on_status, self.url, ConnectionState.DISCONNECTED)

        self.ws = websocket.WebSocketApp(
            self.url,
            on_open=on_open,
            on_message=on_msg,
            on_error=on_err,
            on_close=on_close,
        )
        threading.Thread(target=self.ws.run_forever, daemon=True).start()

    def restart(self):
        if not self.is_connected:
            # Simple restart check: if thread is dead, start new one?
            # WebSocketApp run_forever blocks, so if it exited, the thread died.
            # We can just call start() again.
            self.start()

    def subscribe(self, sub_id, filters, snapshot=False):
        if not self.is_connected:
            return

        # Close the previous subscription before opening a new one, but only if
        # it's actually open on THIS relay. The old code used a single shared
        # self.sub_id and sent CLOSE for IDs the relay didn't recognize, which
        # produced "bad close: invalid subscription id length" NOTICEs and
        # "too many concurrent REQs" — degrading relay communication.
        if self.sub_id and self.sub_id != sub_id:
            try:
                self.ws.send(json.dumps(["CLOSE", self.sub_id]))
            except Exception:
                pass
            self.active_sub_ids.discard(self.sub_id)

        if snapshot:
            self.snapshot_ids.add(sub_id)
        elif sub_id in self.snapshot_ids:
            self.snapshot_ids.remove(sub_id)

        self.sub_id = sub_id
        self.active_sub_ids.add(sub_id)
        try:
            self.ws.send(
                json.dumps(
                    ["REQ", sub_id]
                    + (filters if isinstance(filters, list) else [filters])
                )
            )
        except Exception:
            pass

    def request_once(self, sub_id, filters):
        self.request_queue.append((sub_id, filters))
        if self.is_connected:
            self.process_queue()

    def process_queue(self):
        if self.is_processing_queue or not self.request_queue:
            return
        self.is_processing_queue = True

        def _worker():
            while self.request_queue and self.is_connected:
                sub_id, filters = self.request_queue.pop(0)
                try:
                    self.ws.send(
                        json.dumps(
                            ["REQ", sub_id]
                            + (filters if isinstance(filters, list) else [filters])
                        )
                    )
                    time.sleep(0.1)
                except Exception:
                    break
            self.is_processing_queue = False

        threading.Thread(target=_worker, daemon=True).start()

    def publish(self, event_json):
        if not self.is_connected:
            # Relay not connected — surface it so the like flow is observable.
            print(f"❌ [{self.url}] publish skipped: relay not connected")
            return False
        try:
            self.ws.send(json.dumps(["EVENT", event_json]))
            return True
        except Exception as e:
            print(f"❌ [{self.url}] publish send failed: {e}")
            return False

    def close(self):
        if self.ws:
            self.ws.close()


class NostrClient(GObject.Object):
    __gsignals__ = {
        "event-received": (GObject.SignalFlags.RUN_FIRST, None, (str, str, str, str)),
        # quote-event-received: full event JSON for pending quote/naddr cards
        # (fires for ALL kinds, not just kind-1 — addressable events are kind
        # 30000-39999 and never hit event-received).
        "quote-event-received": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "profile-updated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "contacts-updated": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "status-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "relay-list-updated": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "metrics-updated": (GObject.SignalFlags.RUN_FIRST, None, (str, int, int, int)),
        # publish-result: (event_id, accepted:bool, message:str, relay_url:str, label:str)
        "publish-result": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str, bool, str, str, str),
        ),
        # relay-log-updated: (line:str) — a new relay-activity log line
        "relay-log-updated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # external-identities-updated: (pubkey:str) — NIP-39 kind-10011 arrived
        "external-identities-updated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # badges-updated: (pubkey:str) — NIP-58 kind-10008 profile badges arrived
        "badges-updated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, db):
        super().__init__()
        self.active_relays = {}
        self.relay_urls = set(DEFAULT_RELAYS)
        self.seen_events = set()
        self.db = db
        # event_id -> {"label": str, "expires": float} for OK-ack resolution
        self.pending_publishes = {}
        # Bounded relay-activity log (newest last); surfaced via relay-log-updated
        self.relay_log = collections.deque(maxlen=200)
        self.my_pubkey = None
        self.my_privkey = None
        self.requested_profiles = {}  # pubkey -> last request timestamp (TTL cache)
        self.requested_external_ids = {}  # NIP-39 kind-10011 TTL cache
        self.requested_badges = {}  # NIP-58 kind-10008 TTL cache
        self.metrics = {}
        self.config_file = os.path.join(
            GLib.get_user_config_dir(), "gnostr", "config.json"
        )
        self.load_config()

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r") as f:
                    data = json.load(f)
                    if data.get("relays"):
                        self.relay_urls = set(data["relays"])
            except Exception:
                pass

    def save_config(self):
        d = os.path.dirname(self.config_file)
        if not os.path.exists(d):
            os.makedirs(d, exist_ok=True)
        try:
            with open(self.config_file, "w") as f:
                json.dump({"relays": list(self.relay_urls)}, f)
        except Exception:
            pass

    def set_keys(self, pub, priv):
        self.my_pubkey = pub
        self.my_privkey = priv

    def connect_all(self):
        def _connect_loop():
            for url in list(self.relay_urls):
                GLib.idle_add(self.add_relay_connection, url)
                time.sleep(0.2)

        threading.Thread(target=_connect_loop, daemon=True).start()

    def add_relay_connection(self, url):
        if url in self.active_relays:
            # If exists but disconnected, consider restarting?
            # Handled by check_connections
            return
        r = NostrRelay(
            url, self._handle_event, self._handle_status, on_ok=self._handle_ok
        )
        r.start()
        self.active_relays[url] = r

    def check_connections(self):
        # Scan active relays, if disconnected, restart them
        for url, relay in self.active_relays.items():
            if not relay.is_connected:
                print(f"🔄 Reconnecting to {url}...")
                relay.restart()

    def add_relay(self, url):
        if url not in self.relay_urls:
            self.relay_urls.add(url)
            self.save_config()
            self.add_relay_connection(url)
            self.emit("relay-list-updated")
            self.publish_relay_list()

    def remove_relay(self, url):
        if url in self.relay_urls:
            self.relay_urls.remove(url)
            self.save_config()
            if url in self.active_relays:
                self.active_relays[url].close()
                del self.active_relays[url]
            self.emit("relay-list-updated")
            self.publish_relay_list()

    def fetch_user_relays(self):
        if not self.my_pubkey:
            return
        filter = {"kinds": [10002], "authors": [self.my_pubkey], "limit": 1}
        sub_id = f"relays_{self.my_pubkey[:8]}_{int(time.time())}"
        for r in self.active_relays.values():
            r.request_once(sub_id, filter)

    def request_once(self, sub_id, filters):
        for r in self.active_relays.values():
            r.request_once(sub_id, filters)

    def subscribe(self, sub_id, filters, snapshot=False):
        for r in self.active_relays.values():
            r.subscribe(sub_id, filters, snapshot=snapshot)

    def publish(self, event):
        sent = 0
        for r in self.active_relays.values():
            if r.publish(event):
                sent += 1
        # Surface how many relays actually received the EVENT — the like flow
        # must be observable, not silently swallowed.
        self._log_relay(
            f"EVENT {event['id'][:8]} sent to {sent}/{len(self.active_relays)} relay(s)"
        )

    def _build_and_publish(self, kind, content, tags, label="Event"):
        """Shared build → sign → publish for any event kind. Returns True on
        success. All social-action publishes route through here (DRY)."""
        if not self.my_privkey or not self.my_pubkey:
            print("❌ No private key loaded")
            return False
        event = gnostr.nostr_utils.build_event(self.my_pubkey, kind, content, tags)
        signed = gnostr.nostr_utils.sign_event(event, self.my_privkey)
        if signed:
            self.publish(signed)
            self._track_publish(signed, label)
            return True
        return False

    def _track_publish(self, event, label):
        """Register a just-published event for OK-ack resolution. The entry
        carries a 30s expiry; handle_ok resolves it, the timeout sweep emits a
        'not confirmed' result for dead relays."""
        self.pending_publishes[event["id"]] = {
            "label": label,
            "expires": time.time() + 30,
        }

    def _log_relay(self, line):
        """Append a relay-activity line to the bounded log and emit the
        relay-log-updated signal so the UI can show it live."""
        ts = time.strftime("%H:%M:%S")
        full = f"[{ts}] {line}"
        self.relay_log.append(full)
        GLib.idle_add(self.emit, "relay-log-updated", full)

    def _handle_ok(self, event_id, accepted, message, relay_url):
        """NIP-01 OK ack: [\"OK\", event_id, accepted, message]. Resolves the
        pending publish for this event_id (if tracked) and emits publish-result."""
        pending = self.pending_publishes.pop(event_id, None)
        if not pending:
            return
        self._log_relay(
            f"{pending['label']} {'OK' if accepted else 'REJECTED'} {relay_url}"
            + (f" ({message})" if message else "")
        )
        GLib.idle_add(
            self.emit,
            "publish-result",
            event_id,
            accepted,
            message,
            relay_url,
            pending["label"],
        )

    def sweep_pending_publishes(self):
        """Emit a not-confirmed result for publishes that never got an OK.
        Call periodically (e.g. on a timer); idempotent."""
        now = time.time()
        for eid, p in list(self.pending_publishes.items()):
            if now > p["expires"]:
                self.pending_publishes.pop(eid, None)
                self._log_relay(f"{p['label']} NOT CONFIRMED (no relay ack in 30s)")
                GLib.idle_add(
                    self.emit,
                    "publish-result",
                    eid,
                    False,
                    "no relay acknowledged within 30s",
                    "",
                    p["label"],
                )

    def publish_post(self, content, reply_to=None):
        """kind-1 text post. `reply_to` = dict(root, parent, root_pk, parent_pk)
        to thread a reply per NIP-01 (root e-tag first, direct parent last)."""
        if reply_to:
            tags = [
                ["e", reply_to["root"]],
                ["e", reply_to["parent"]],
                ["p", reply_to["root_pk"]],
                ["p", reply_to["parent_pk"]],
            ]
        else:
            tags = []
        return self._build_and_publish(1, content, tags, label="Post")

    def publish_reaction(self, target_event_id, target_pubkey, content="+"):
        """NIP-25: kind-7 reaction. content '+' adds, '-' removes/undo."""
        if not self.my_privkey or not self.my_pubkey:
            print("❌ No private key loaded")
            return False
        event = gnostr.nostr_utils.build_reaction_event(
            self.my_pubkey, target_event_id, target_pubkey, content
        )
        signed = gnostr.nostr_utils.sign_event(event, self.my_privkey)
        if signed:
            self.publish(signed)
            self._track_publish(signed, "Like" if content == "+" else "Unlike")
            return True
        return False

    def publish_repost(
        self, target_event_id, target_pubkey, target_kind=1, original_event=None
    ):
        """NIP-18: kind-6 generic repost. content = original event JSON.
        `original_event` is the DB row; falls back to fetching it."""
        if not self.my_privkey or not self.my_pubkey:
            print("❌ No private key loaded")
            return False
        ev = original_event or self.db.get_event_by_id(target_event_id)
        if not ev:
            print("❌ Could not load original event for repost")
            return False
        event = gnostr.nostr_utils.build_repost_event(
            self.my_pubkey,
            target_event_id,
            target_pubkey,
            ev.get("kind", target_kind),
            ev,
        )
        signed = gnostr.nostr_utils.sign_event(event, self.my_privkey)
        if signed:
            self.publish(signed)
            return True
        return False

    def publish_contacts(self):
        """Publish the current follow list as a kind-3 contact-list event."""
        if not self.my_privkey or not self.my_pubkey:
            return False
        followed = self.db.get_following_list(self.my_pubkey)
        event = {
            "pubkey": self.my_pubkey,
            "created_at": int(time.time()),
            "kind": 3,
            "tags": [["p", pk] for pk in followed],
            "content": "",
        }
        signed = gnostr.nostr_utils.sign_event(event, self.my_privkey)
        if signed:
            self.publish(signed)
            return True
        return False

    def publish_profile(self, metadata):
        """Publish a kind-0 user-metadata event (NIP-01/NIP-24).

        `metadata` is a dict of profile fields (name, display_name, about,
        picture, banner, website, nip05, lud16, bot, birthday). Builds the
        stringified-JSON content, signs, publishes, tracks the OK ack, and
        persists locally. Returns True on success."""
        if not self.my_privkey or not self.my_pubkey:
            print("❌ No private key loaded")
            return False
        content = json.dumps(metadata, separators=(",", ":"))
        event = gnostr.nostr_utils.build_event(self.my_pubkey, 0, content, [])
        signed = gnostr.nostr_utils.sign_event(event, self.my_privkey)
        if signed:
            self.publish(signed)
            self._track_publish(signed, "Profile")
            self.db.save_profile(self.my_pubkey, content, event["created_at"])
            GLib.idle_add(self.emit, "profile-updated", self.my_pubkey)
            return True
        return False

    def publish_relay_list(self):

        event = {
            "pubkey": self.my_pubkey,
            "created_at": int(time.time()),
            "kind": 10002,
            "tags": [["r", u] for u in self.relay_urls],
            "content": "",
        }
        signed = gnostr.nostr_utils.sign_event(event, self.my_privkey)
        if signed:
            self.publish(signed)

    def follow_user(self, target_pubkey):
        if not self.my_pubkey or not self.my_privkey:
            return
        following = self.db.get_following_list(self.my_pubkey)
        if target_pubkey in following:
            return
        following.append(target_pubkey)
        self._publish_contact_list(following, label="Follow")
        print(f"✅ Followed {target_pubkey[:8]}...")

    def unfollow_user(self, target_pubkey):
        if not self.my_pubkey or not self.my_privkey:
            return
        following = self.db.get_following_list(self.my_pubkey)
        if target_pubkey not in following:
            return
        following.remove(target_pubkey)
        self._publish_contact_list(following, label="Unfollow")
        print(f"✅ Unfollowed {target_pubkey[:8]}...")

    def _publish_contact_list(self, following, label="Follow"):
        tags = [["p", pk] for pk in following]
        event = {
            "pubkey": self.my_pubkey,
            "created_at": int(time.time()),
            "kind": 3,
            "tags": tags,
            "content": "",
        }
        signed = gnostr.nostr_utils.sign_event(event, self.my_privkey)
        if signed:
            self.publish(signed)
            self._track_publish(signed, label)
            self.db.save_contacts(self.my_pubkey, following)
            GLib.idle_add(self.emit, "contacts-updated")

    def get_ref_id(self, tags):
        for t in tags:
            if t[0] == "e":
                return t[1]
        return None

    def _handle_event(self, ev):
        try:
            eid = ev.get("id")
            kind = ev["kind"]
            pubkey = ev["pubkey"]
            tags = ev.get("tags", [])
        except KeyError as e:
            print(f"❌ ERROR: Malformed Event: {e}")
            return

        # Dedup FIRST so re-subscribed events (e.g. thread refresh) are never
        # re-counted into metrics or re-saved/re-emitted.
        if eid in self.seen_events:
            return
        self.seen_events.add(eid)

        target = self.get_ref_id(tags)
        if target:
            if target not in self.metrics:
                self.metrics[target] = {"likes": 0, "reposts": 0, "replies": 0}
            updated = False
            if kind == 7:
                self.metrics[target]["likes"] += 1
                updated = True
            elif kind == 6:
                self.metrics[target]["reposts"] += 1
                updated = True
            elif kind == 1:
                self.metrics[target]["replies"] += 1
                updated = True
            if updated:
                m = self.metrics[target]
                GLib.idle_add(
                    self.emit,
                    "metrics-updated",
                    target,
                    m["likes"],
                    m["reposts"],
                    m["replies"],
                )

        # Update metrics for this event's own ID for likes/reposts
        if kind == 1:
            if eid not in self.metrics:
                self.metrics[eid] = {"likes": 0, "reposts": 0, "replies": 0}

        self.db.save_event(ev)

        # Fire the quote-event signal for EVERY kind so pending quote/naddr
        # cards can populate on arrival. Addressable events (kind 30000-39999)
        # never hit the kind-1 event-received path, so this is their only route.
        GLib.idle_add(self.emit, "quote-event-received", json.dumps(ev))

        if kind == 0:
            self.db.save_profile(pubkey, ev["content"], ev["created_at"])
            GLib.idle_add(self.emit, "profile-updated", pubkey)

        elif kind == 10011:
            # NIP-39 external identities
            ids = gnostr.profile_nips.parse_external_identities(ev)
            if ids:
                self.db.save_external_identities(pubkey, ids)
                GLib.idle_add(self.emit, "external-identities-updated", pubkey)

        elif kind == 10008:
            # NIP-58 profile badges (ordered a/e pairs)
            badges = gnostr.profile_nips.parse_profile_badges(ev)
            if badges:
                self.db.save_profile_badges(pubkey, badges)
                GLib.idle_add(self.emit, "badges-updated", pubkey)

        elif kind == 30009:
            # NIP-58 badge definition — store for later image resolution
            self.db.save_badge_definition(ev)

        elif kind == 3:
            if pubkey == self.my_pubkey:
                c = gnostr.nostr_utils.extract_followed_pubkeys(ev)
                self.db.save_contacts(self.my_pubkey, c)
                GLib.idle_add(self.emit, "contacts-updated")
                try:
                    if ev["content"]:
                        rj = json.loads(ev["content"])
                        if isinstance(rj, dict):
                            self._merge_relays(rj.keys())
                except Exception:
                    pass

        elif kind == 10002:
            if pubkey == self.my_pubkey:
                nr = [t[1] for t in tags if t[0] == "r" and len(t) > 1]
                if nr:
                    self._merge_relays(nr)

        elif kind == 1:
            GLib.idle_add(
                self.emit,
                "event-received",
                eid,
                pubkey,
                ev["content"],
                json.dumps(tags),
            )

    def _merge_relays(self, new_list):
        changed = False
        for r in new_list:
            r = r.rstrip("/")
            if r.startswith("ws") and r not in self.relay_urls:
                self.relay_urls.add(r)
                self.add_relay_connection(r)
                changed = True
        if changed:
            self.save_config()
            GLib.idle_add(self.emit, "relay-list-updated")

    def _handle_status(self, url, status):
        # Log relay connect/disconnect so the activity pane shows connectivity.
        self._log_relay(f"{url} {status.status}")
        self.emit("status-changed", status.status)

    def fetch_contacts(self):
        if self.my_pubkey:
            self.subscribe(
                "sub_contacts", {"kinds": [3], "authors": [self.my_pubkey], "limit": 1}
            )

    def fetch_profile(self, pubkey, ttl=600):
        # TTL cache: re-request a profile only if not requested recently, so
        # failed/incomplete lookups get retried (the old one-shot set never retried).
        now = time.time()
        if (
            pubkey in self.requested_profiles
            and (now - self.requested_profiles[pubkey]) < ttl
        ):
            return
        self.requested_profiles[pubkey] = now
        for r in self.active_relays.values():
            r.request_once(
                f"meta_{pubkey[:8]}", {"kinds": [0], "authors": [pubkey], "limit": 1}
            )

    def fetch_external_identities(self, pubkey, ttl=600):
        """Request a user's NIP-39 external identities (kind 10011)."""
        now = time.time()
        if (
            pubkey in self.requested_external_ids
            and (now - self.requested_external_ids[pubkey]) < ttl
        ):
            return
        self.requested_external_ids[pubkey] = now
        for r in self.active_relays.values():
            r.request_once(
                f"extid_{pubkey[:8]}",
                {"kinds": [10011], "authors": [pubkey], "limit": 1},
            )

    def fetch_badges(self, pubkey, ttl=600):
        """Request a user's NIP-58 profile badges (kind 10008)."""
        now = time.time()
        if (
            pubkey in self.requested_badges
            and (now - self.requested_badges[pubkey]) < ttl
        ):
            return
        self.requested_badges[pubkey] = now
        for r in self.active_relays.values():
            r.request_once(
                f"badges_{pubkey[:8]}",
                {"kinds": [10008], "authors": [pubkey], "limit": 1},
            )

    def fetch_thread(self, root_id):
        f1 = {"ids": [root_id]}
        f2 = {"kinds": [1], "#e": [root_id], "limit": 50}
        f3 = {"kinds": [6, 7], "#e": [root_id], "limit": 100}
        self.subscribe(f"thread_{root_id}", [f1, f2, f3])

    def close(self):
        for r in self.active_relays.values():
            r.close()
