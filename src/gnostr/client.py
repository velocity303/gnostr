import collections
import json
import os
import threading
import time
import traceback

from gi.repository import GLib, GObject

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

# NIP-17 relay targeting (spec docs/specs/nip44-nip17-dms.md): TTL for cached
# kind-10050 lookups, and how long to wait for a 10050 response before we
# declare the peer unreachable for DMs (relays without the event answer only
# with EOSE, so a timeout is the "no 10050" signal).
DM_RELAY_TTL = 3600
DM_RELAY_TIMEOUT_MS = 5000

# P4 batch DM-capability probe (spec docs/specs/dm-initiation-and-feed-freshness.md):
# kind-3 authors per REQ, and how long to wait for the batch before settling
# (timeout-as-negative, same logic as the single-peer lookup above).
DM_STATUS_BATCH = 50
DM_STATUS_TIMEOUT_MS = 5000

# P5 client-side initiation throttle: gift wraps are signed by one-time keys,
# so relays cannot reputation-filter (NIP-17 Spam / NIP-59 Spam Protection) —
# the client owns abuse control. New-conversation initiations per rolling hour.
DM_INITIATION_WARN_PER_HOUR = 10
DM_INITIATION_HARD_STOP_PER_HOUR = 30


class NostrRelay(GObject.Object):
    def __init__(self, url, on_event, on_status, on_ok=None, on_eose=None):
        super().__init__()
        self.url = url
        self.on_event = on_event
        self.on_status = on_status
        self.on_ok = on_ok or (lambda *a: None)
        self.on_eose = on_eose
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
                    if self.on_eose:
                        self.on_eose(sub_id, self.url)
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
        # Register the sub_id for EOSE auto-close (spec Phase B2a): the on_msg
        # EOSE handler CLOSEs anything in snapshot_ids, so one-shot REQs
        # (freshness pull, fetch_user_relays, fetch_profile/… — all unique
        # sub_ids) no longer leak open subscriptions on every relay.
        self.snapshot_ids.add(sub_id)
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
        "contacts-updated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
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
        # dm-conversations-updated: (peer_pk:str) — the DM gateway persisted a
        # rumor (inbound or our outbound self-copy); inbox/thread views refresh.
        "dm-conversations-updated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # dm-unreachable: (peer_pk:str, event_id:str) — the recipient has no
        # kind-10050 DM-relay list, so the gift-wrap was NOT published (spec
        # docs/specs/nip44-nip17-dms.md 'Relay targeting' step 3). MainWindow
        # surfaces this as a toast; the thread view fails the optimistic bubble.
        "dm-unreachable": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        # dm-initiation-throttled: (peer_pk:str, level:str) — P5 client-side
        # abuse control (spec Phase P): gift wraps use one-time keys so relays
        # cannot reputation-filter; the client counts new-conversation
        # initiations per rolling hour. level is "warn" (threshold reached,
        # send still allowed) or "hard-stop" (send refused). UI wiring is a
        # separate card; the UI surfaces this as a toast.
        "dm-initiation-throttled": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
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
        # NIP-17 relay targeting: peer -> (timestamp, [relay urls]) from their
        # kind-10050. Same TTL-cache pattern as requested_external_ids (10011).
        self.dm_relay_cache = {}
        # peer -> [callbacks waiting on the open kind-10050 lookup]
        self._dm_relay_waiters = {}
        # P4 batch probe state: token -> batch dict, sub_id -> token
        self._dm_status_batches = {}
        self._dm_status_by_sub = {}
        self._dm_status_seq = 0
        # P5 throttle: timestamps (time.time()) of new-conversation
        # initiations in the rolling hour. In-memory only by design — a
        # persisted counter would punish the user after a restart for sends
        # the relays already accepted.
        self._dm_initiation_times = collections.deque()
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
            url,
            self._handle_event,
            self._handle_status,
            on_ok=self._handle_ok,
            on_eose=self._handle_eose,
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
        following = self.db.get_following_list(self.my_pubkey)
        self._publish_contact_list(following, label="Contacts")
        return True

    def sync_followers(self):
        """Manual 'Sync Followers' push: re-publish the DB following list to all
        relays (kind-3 is replaceable — relays swap the contact list wholesale),
        tracked for OK acks so the Relay Activity pane confirms the sync landed.
        Pairs with the startup pull (fetch_contacts → kind-3 → save_contacts)
        that reconciles the DB from the relays' newest view."""
        if not self.my_privkey or not self.my_pubkey:
            self._log_relay("follow-sync skipped: no keys")
            return False
        following = self.db.get_following_list(self.my_pubkey)
        self._log_relay(f"follow-sync: pushing {len(following)} follows")
        self._publish_contact_list(following, label="Follow Sync")
        return True

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
            GLib.idle_add(self.emit, "contacts-updated", self.my_pubkey)

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
            # PULL reconciliation for ANY author: the newest kind-3 is that
            # author's authoritative contact list — save_contacts replaces
            # that owner's following rows wholesale (the table is
            # owner-keyed, so foreign lists populate their own rows).
            c = gnostr.nostr_utils.extract_followed_pubkeys(ev)
            self.db.save_contacts(pubkey, c)
            if pubkey == self.my_pubkey:
                self._log_relay(f"kind-3 received: {len(c)} follows reconciled")
                # Relay-hint merging is own-pubkey-only: foreign kind-3
                # content is not ours to merge.
                try:
                    if ev["content"]:
                        rj = json.loads(ev["content"])
                        if isinstance(rj, dict):
                            self._merge_relays(rj.keys())
                except Exception:
                    pass
            GLib.idle_add(self.emit, "contacts-updated", pubkey)

        elif kind == 10002:
            if pubkey == self.my_pubkey:
                nr = [t[1] for t in tags if t[0] == "r" and len(t) > 1]
                if nr:
                    self._merge_relays(nr)

        elif kind == 10050:
            # NIP-17 DM relays: settle any open lookup for this author and
            # refresh the TTL cache so the next send skips the fetch.
            # P3 tag-name tolerance: NIP-17's example uses ["relay", ...]
            # while nostr-tools/NIP-65 convention publish ["r", ...] — parse
            # BOTH (publish stays "r"), or a spec-literal peer's list reads
            # as empty -> false "unreachable".
            nr = [t[1] for t in tags if t[0] in ("r", "relay") and len(t) > 1]
            self._dm_status_event(pubkey, nr)
            self._resolve_dm_relays(pubkey, nr or None)

        elif kind == 1:
            GLib.idle_add(
                self.emit,
                "event-received",
                eid,
                pubkey,
                ev["content"],
                json.dumps(tags),
            )

        elif kind == 4:
            # NIP-04 DM rumor addressed to us (since-tag). Persist the raw
            # rumor via the DM gateway — save_event rolls up dm_conversations
            # (unread_count, last_activity) using the gateway's bound pubkey —
            # then route the raw rumor through DMService for off-thread
            # decryption. Skeleton/placeholder: the row is visible immediately
            # with the raw payload and resolves to plaintext as the worker
            # finishes.
            gw = getattr(self, "dm_gateway", None)
            if gw is not None:
                try:
                    # Our own echo collides with the plaintext self-copy's id
                    # (PRIMARY KEY) and aborts harmlessly — same no-op path as
                    # relay dedup; the row keeps its plaintext.
                    gw.save_event(ev)
                    peer = ev.get("pubkey") or ""
                    GLib.idle_add(self.emit, "dm-conversations-updated", peer)
                except Exception as e:
                    print(f"❌ DM Gateway save failed: {e}")
            # Never decrypt our own echo: the self-copy already holds the
            # plaintext and update_content would clobber it with ciphertext.
            if (
                getattr(self, "dm_service", None) is not None
                and ev.get("pubkey") != self.my_pubkey
            ):
                self.dm_service.schedule_decrypt(ev)

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

    def set_dm_gateway(self, gateway):
        """Inject a pre-built DM gateway (tests wire their own on an isolated
        DB). The gateway must be bound to our pubkey so save_event can tell
        inbound from outbound rumors when rolling up the conversation row."""
        self.dm_gateway = gateway

    def _ensure_dm_service(self):
        """Lazily build the DM service + gateway on first kind-4 rumor.

        Production callers (main.py) can inject `self.dm_gateway` ahead of time
        to skip re-creation; headless/real-runtime paths create it here. The
        gateway is bound to our pubkey so inbound/outbound rumors are told
        apart when rolling up the conversation row.
        """
        if getattr(self, "dm_service", None) is not None:
            # Re-login with a different key: the cached service holds the OLD
            # privkey, which would encrypt/decrypt every DM wrong. Drop and
            # rebuild when the loaded key changed.
            svc = self.dm_service
            want = bytes.fromhex(self.my_privkey) if self.my_privkey else None
            if want is not None and getattr(svc, "_our_privkey", None) != want:
                self.dm_service = None
            else:
                return
        gw = getattr(self, "dm_gateway", None)
        if gw is None:
            from gnostr.gateway.dm_gateway import SQLiteDMGateway

            gw = SQLiteDMGateway()
            self.dm_gateway = gw
        # Bind the gateway to our pubkey so save_event can tell inbound from
        # outbound rumors when rolling up the conversation row. bind_account
        # also detects ACCOUNT SWITCH (persisted bound pubkey) and wipes the
        # shared DB first — user B must never see user A's decrypted DMs.
        if getattr(gw, "_our_pubkey_override", "") != self.my_pubkey:
            try:
                if hasattr(gw, "bind_account"):
                    gw.bind_account(self.my_pubkey or "")
                else:
                    gw.set_our_pubkey(self.my_pubkey or "")
            except Exception:
                pass
        from gnostr.service.dm_service import DMService

        self.dm_service = DMService(gateway=gw, our_privkey=self.my_privkey)

    def subscribe_dm(self):
        """Subscribe to kind-4 DM rumors addressed to us (since-tag)."""
        if not self.my_pubkey:
            print("⚠️ DM subscription skipped: no pubkey")
            return
        self.subscribe("dm_sub", {"kinds": [4], "#p": [self.my_pubkey], "limit": 50})

    def ensure_dm(self):
        """Public hook for the app: build DM gateway + service at login (not
        lazily on first rumor) so the UI can connect to dm-decrypted before
        any DM arrives, and open the DM subscription. Idempotent.

        Also maintains our OWN kind-10050 (spec step 4): default content is
        1-2 of the user's write relays; the settings UI is later scope."""
        self._ensure_dm_service()
        if self.my_pubkey:
            self.subscribe_dm()
            self.publish_dm_relay_list()

    def publish_dm(self, peer_pubkey, plaintext):
        """Encrypt + publish a DM to `peer_pubkey`. Returns the signed rumor
        dict (its id keys the UI's optimistic bubble) or None on failure.

        Wire: NIP-44 v2 ciphertext riding the kind-4 rumor skeleton the client
        already subscribes to (NIP-59 gift-wrap envelope is follow-up scope,
        per issue #19). The rumor is persisted through the DM gateway as our
        self-copy BEFORE publishing, so the sender's thread shows it even if
        no relay ever echoes it back. OK-ack tracking uses the rumor id — the
        publish-result signal resolves the optimistic bubble.

        Relay targeting (spec 'Relay targeting' step 3): the rumor is published
        ONLY to the recipient's kind-10050 DM relays, resolved asynchronously —
        Send never blocks the GTK main loop. If the peer has no 10050 the
        publish is aborted and `dm-unreachable` is emitted (MainWindow toasts).
        """
        if not self.my_privkey or not self.my_pubkey:
            print("❌ DM send failed: no keys loaded")
            return None
        if not self._dm_initiation_allowed(peer_pubkey):
            # P5 hard-stop: refuse before the self-copy is saved, so a
            # refused initiation never creates a phantom conversation row.
            return None
        self._ensure_dm_service()
        try:
            ciphertext = self.dm_service.encrypt(plaintext, peer_pubkey)
        except Exception as e:
            print(f"❌ DM encrypt failed: {e}")
            return None
        event = gnostr.nostr_utils.build_event(
            self.my_pubkey, 4, ciphertext, [["p", peer_pubkey]]
        )
        signed = gnostr.nostr_utils.sign_event(event, self.my_privkey)
        if not signed:
            return None
        gw = getattr(self, "dm_gateway", None)
        if gw is not None:
            try:
                # Self-copy stores PLAINTEXT, not the wire ciphertext: the
                # thread/inbox read straight from dm_messages and nothing
                # ever re-decrypts our own rows (no relay echo on targeted
                # publishes). The published event keeps the ciphertext.
                gw.save_event({**signed, "content": plaintext})
                # publish_dm runs on the UI thread (composer action), so the
                # inbox refresh can emit directly — no idle_add hop needed.
                self.emit("dm-conversations-updated", peer_pubkey)
            except Exception as e:
                print(f"❌ DM Gateway save (outbound) failed: {e}")
        self._publish_dm_targeted(peer_pubkey, signed)
        return signed

    def _publish_dm_targeted(self, peer_pubkey, event):
        """Publish a signed DM rumor to the peer's 10050 relays only, once the
        (async) lookup resolves. No 10050 -> abort + dm-unreachable, and the
        self-copy already in the gateway is the only trace (no publish)."""

        def _deliver(relays):
            if relays is None:
                self._log_relay(
                    f"DM {event['id'][:8]} aborted: {peer_pubkey[:8]} has no kind-10050"
                )
                GLib.idle_add(self.emit, "dm-unreachable", peer_pubkey, event["id"])
                return
            self.publish_to_relays(event, relays)
            self._track_publish(event, "DM")

        self.resolve_dm_relays(peer_pubkey, _deliver)

    def resolve_dm_relays(self, peer_pubkey, callback):
        """Resolve a peer's kind-10050 DM relays without blocking the caller.

        Fresh TTL cache -> callback on the next idle tick. Open lookup -> join
        its waiter list. Otherwise fire a one-shot REQ for the peer's kind-10050
        and arm a timeout: relays without the event answer only with EOSE, so
        the timeout IS the 'no 10050' signal (callback gets None). Same TTL
        pattern as fetch_external_identities (kind 10011).
        """
        cached = self.dm_relay_cache.get(peer_pubkey)
        if cached is not None and (time.time() - cached[0]) < DM_RELAY_TTL:
            GLib.idle_add(callback, cached[1])
            return
        waiters = self._dm_relay_waiters.get(peer_pubkey)
        if waiters is not None:
            waiters.append(callback)
            return
        self._dm_relay_waiters[peer_pubkey] = [callback]
        self.request_once(
            f"dmrelays_{peer_pubkey[:8]}",
            {"kinds": [10050], "authors": [peer_pubkey], "limit": 1},
        )
        try:
            GLib.timeout_add(DM_RELAY_TIMEOUT_MS, self._dm_relay_timeout, peer_pubkey)
        except Exception:
            # No main loop to arm the timeout on (headless): the lookup can
            # still resolve via _handle_event; tests drive the timeout directly.
            pass

    def _dm_relay_timeout(self, peer_pubkey):
        """Timeout tick: an unresolved lookup means the peer has no 10050."""
        self._resolve_dm_relays(peer_pubkey, None)
        return False  # one-shot

    def _resolve_dm_relays(self, peer_pubkey, relays):
        """Settle an open 10050 lookup (relays list, or None for unreachable).
        Positive results are cached with TTL; negative results are not, so a
        peer who later sets a 10050 becomes reachable without waiting."""
        if relays:
            self.dm_relay_cache[peer_pubkey] = (time.time(), relays)
        waiters = self._dm_relay_waiters.pop(peer_pubkey, [])
        for cb in waiters:
            GLib.idle_add(cb, relays if relays else None)

    def probe_dm_reachability(self, peer_pubkey, callback):
        """P1: public tri-state reachability probe (spec Phase P).

        Thin wrapper over resolve_dm_relays — SAME cache, waiter list and 5 s
        timeout; deliberately no second cache. callback(peer_pk, relays):
        relays non-empty -> reachable (has a 10050); relays None -> no 10050
        (timeout-as-negative). While the lookup is in flight the callback has
        not fired yet — that is the third state; callers render 'unknown'.

        Caveats for the UI (spec P1): 10050-present != deliverable (auth
        relays may drop gift wraps), and negatives are never cached, so a
        stale 'unreachable' badge must never block Send — `dm-unreachable`
        remains the authoritative mid-send failure signal.
        """
        self.resolve_dm_relays(
            peer_pubkey, lambda relays: callback(peer_pubkey, relays)
        )

    # --- P4: batch DM-capability probe (spec Phase P) ---

    def fetch_dm_relay_status(self, pubkeys, callback):
        """P4: batch kind-10050 presence probe (spec Phase P).

        Chunked <=50-author REQs ({"kinds":[10050],"authors":chunk,
        "limit":len}) via request_once on every connected relay; settle on
        EOSE-from-all-relays OR timeout (kind 10050 is replaceable — one
        event per author per relay, so absence across all relays after EOSE
        is the negative). callback(status_map) fires once with
        {pubkey: [relays]} for PRESENT peers only; positives are fed into the
        same dm_relay_cache (TTL 3600) so the first DM to a probed follower
        skips the lookup. Absent peers are unknown, never cached — no
        persisted capability cache (goes stale against peers editing 10050).
        """
        pubkeys = [p for p in dict.fromkeys(pubkeys or []) if p]
        if not pubkeys:
            GLib.idle_add(callback, {})
            return
        chunks = [
            pubkeys[i : i + DM_STATUS_BATCH]
            for i in range(0, len(pubkeys), DM_STATUS_BATCH)
        ]
        shared = {"results": {}, "pending": len(chunks)}
        relay_count = len(self.active_relays)
        for chunk in chunks:
            self._dm_status_seq += 1
            token = f"dmstatus_{self._dm_status_seq}_{int(time.time() * 1000)}"
            sub_id = f"dmstatus_{self._dm_status_seq}"
            self._dm_status_batches[token] = {
                "sub_id": sub_id,
                "authors": set(chunk),
                "shared": shared,
                "callback": callback,
                "seen": set(),
                "relay_count": relay_count,
                "settled": False,
            }
            self._dm_status_by_sub[sub_id] = token
            self.request_once(
                sub_id,
                {"kinds": [10050], "authors": chunk, "limit": len(chunk)},
            )
            try:
                GLib.timeout_add(DM_STATUS_TIMEOUT_MS, self._dm_status_timeout, token)
            except Exception:
                # No main loop to arm the timeout on (headless): tests drive
                # the tick / EOSE fan-in directly, same as the 10050 lookup.
                pass
        if relay_count == 0:
            # Nothing connected: settle everything now as unknown rather than
            # waiting out a timeout for relays that will never answer.
            for token in list(self._dm_status_batches):
                self._settle_dm_status_batch(token)

    def _dm_status_timeout(self, token):
        batch = self._dm_status_batches.get(token)
        if batch is not None:
            self._settle_dm_status_batch(token)
        return False  # one-shot

    def _handle_eose(self, sub_id, relay_url):
        """EOSE fan-in for P4 batch probes: a chunk settles once every relay
        that was connected at REQ time has answered. Other sub_ids are not
        tracked here and are ignored."""
        token = self._dm_status_by_sub.get(sub_id)
        if token is None:
            return
        batch = self._dm_status_batches.get(token)
        if batch is None or batch["settled"]:
            return
        batch["seen"].add(relay_url)
        if len(batch["seen"]) >= batch["relay_count"]:
            self._settle_dm_status_batch(token)

    def _dm_status_event(self, pubkey, relays):
        """Record a kind-10050 arrival into any open batch probe containing
        this author (called from _handle_event)."""
        if not relays:
            return
        for batch in self._dm_status_batches.values():
            if pubkey in batch["authors"]:
                batch["shared"]["results"][pubkey] = relays

    def _settle_dm_status_batch(self, token):
        batch = self._dm_status_batches.pop(token, None)
        if batch is None or batch["settled"]:
            return
        batch["settled"] = True
        self._dm_status_by_sub.pop(batch["sub_id"], None)
        shared = batch["shared"]
        now = time.time()
        # Positives into the shared TTL cache (TTL 3600 via DM_RELAY_TTL
        # expiry check); negatives (absent authors) are simply not present.
        for pk, relays in shared["results"].items():
            self.dm_relay_cache[pk] = (now, relays)
        shared["pending"] -= 1
        if shared["pending"] <= 0:
            GLib.idle_add(batch["callback"], dict(shared["results"]))

    # --- P5: client-side new-conversation initiation throttle ---

    def _dm_initiation_allowed(self, peer_pubkey):
        """P5 abuse control (spec Phase P): relays cannot reputation-filter
        gift wraps (one-time keys), so the client counts first-message-to-
        new-peer sends in a rolling in-memory hour. Replies in existing
        conversations are exempt (a dm_messages row for the peer exists).

        Returns True if the send may proceed. Warn at
        DM_INITIATION_WARN_PER_HOUR (send still allowed, signal emitted once
        per crossing), hard-stop at DM_INITIATION_HARD_STOP_PER_HOUR (send
        refused, signal emitted). The UI subscribes to
        `dm-initiation-throttled` (peer_pk, level) — wiring is UI scope.
        """
        gw = getattr(self, "dm_gateway", None)
        if gw is not None:
            try:
                if gw.get_thread(peer_pubkey, 1, None):
                    return True  # existing conversation: reply, exempt
            except Exception:
                pass
        now = time.time()
        times = self._dm_initiation_times
        while times and now - times[0] > 3600:
            times.popleft()
        if len(times) >= DM_INITIATION_HARD_STOP_PER_HOUR:
            self._log_relay(
                f"DM initiation hard stop: {peer_pubkey[:8]} "
                f"({len(times)} new conversations in the last hour)"
            )
            GLib.idle_add(
                self.emit, "dm-initiation-throttled", peer_pubkey, "hard-stop"
            )
            return False
        times.append(now)
        if len(times) == DM_INITIATION_WARN_PER_HOUR:
            self._log_relay(
                f"DM initiation warning: {len(times)} new conversations "
                "in the last hour"
            )
            GLib.idle_add(self.emit, "dm-initiation-throttled", peer_pubkey, "warn")
        return True

    def publish_to_relays(self, event, urls):
        """Publish an event ONLY to the relays in `urls` (NIP-17 targeting).
        Returns how many relays accepted the send."""
        sent = 0
        for url, r in self.active_relays.items():
            if url in urls and r.publish(event):
                sent += 1
        self._log_relay(
            f"EVENT {event['id'][:8]} sent to {sent}/{len(urls)} DM relay(s)"
        )
        return sent

    def dm_relay_defaults(self):
        """Own DM relays default: up to 2 of the user's configured relays
        (settings UI is explicitly later scope, spec step 4)."""
        return sorted(self.relay_urls)[:2]

    def publish_dm_relay_list(self):
        """Publish/maintain our own kind-10050 DM-relay list (spec step 4).
        Default content: 1-2 of the user's write relays. Called at login."""
        if not self.my_privkey or not self.my_pubkey:
            return False
        urls = self.dm_relay_defaults()
        if not urls:
            return False
        event = {
            "pubkey": self.my_pubkey,
            "created_at": int(time.time()),
            "kind": 10050,
            "tags": [["r", u] for u in urls],
            "content": "",
        }
        signed = gnostr.nostr_utils.sign_event(event, self.my_privkey)
        if not signed:
            return False
        self.publish(signed)
        self._track_publish(signed, "DM Relays")
        return True

    def _handle_status(self, url, status):
        # Log relay connect/disconnect so the activity pane shows connectivity.
        self._log_relay(f"{url} {status.status}")
        self.emit("status-changed", status.status)

    def fetch_contacts(self):
        if self.my_pubkey:
            self.fetch_contacts_for(self.my_pubkey)

    def fetch_contacts_for(self, pubkey):
        """Fetch any user's newest kind-3 contact list (kind-3 is
        replaceable — one event is the whole list)."""
        if pubkey:
            self.subscribe(
                f"sub_contacts_{pubkey[:8]}",
                {"kinds": [3], "authors": [pubkey], "limit": 1},
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
