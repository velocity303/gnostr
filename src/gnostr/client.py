import collections
import json
import os
import random
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

# --- Relay usage economy (issue #26 'Relay Usage Optimizations') ---
# Popular relays cap REQ messages per IP (nos.lol: 200 REQ / 10 s, then an
# automated ban). A client's relay economy is about how it batches, caches,
# re-issues subscriptions, and reacts to CLOSED — the failure mode shared by
# every heavy client in the issue's findings is 'CLOSED is unhandled'.
REQ_RATE_LIMIT = 18  # REQ messages per relay per second (18/s < 200/10s cap)
REQ_BURST = 5  # burst allowance for a legitimate simultaneous fan-out
REQ_DEDUP_WINDOW = 2.0  # s; same sub_id enqueued twice inside this window = 1 REQ
RECONNECT_BACKOFF_START = 2.0  # s; randomized exponential, capped below
RECONNECT_BACKOFF_MAX = 60.0
IDLE_RELAY_TIMEOUT = 300.0  # s of zero relay traffic before idle disconnect
# DM freshness poll (DM-UX): opening any other feed CLOSEs dm_sub —
# RelayConnection.subscribe closes the previous sub_id when a different one
# opens — so without this a new DM only surfaces on a manual refresh. One
# snapshot REQ per relay per interval is ~1 REQ/min, two orders of magnitude
# below relay rate limits (nos.lol bans at 200 REQ/10 s per IP).
DM_POLL_INTERVAL = 60  # s between kind-4 re-REQs while logged in
DM_POLL_SINCE_SLACK = 120  # s of overlap re-fetch; dedup absorbs the repeats


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
        self.queued_sub_ids = set()  # dedup: sub_ids already waiting in queue
        self._recent_reqs = {}  # sub_id -> monotonic ts (windowed REQ dedup)
        # DM-3 resilient publish: EVENTs aimed at this relay while it was down,
        # flushed on open (fallback relays get connected mid-retry and need
        # their send to survive until the socket is live).
        self.publish_queue = []
        self.is_processing_queue = False
        self.snapshot_ids = set()  # Track subscriptions that should close on EOSE
        # Live (non-snapshot) subscriptions, sub_id -> filters. A reconnect
        # used to silently drop every live sub (sub_following, dm_sub) until
        # the next manual feed switch — DMs stopped arriving after a flap.
        # Reconnect now replays these through the rate-limited queue.
        self.live_subs = {}
        # REQ rate limiting (issue #26): token bucket refilled at
        # REQ_RATE_LIMIT/s; the queue worker sleeps instead of bursting past
        # a relay's per-IP REQ cap (nos.lol bans at 200 REQ/10 s per IP).
        self._tokens = float(REQ_BURST)
        self._last_refill = time.monotonic()
        # Reconnect backoff (issue #26): randomized exponential so a fleet of
        # gnostr clients behind one CGNAT address does not re-thunder the
        # relay in lockstep after a flap. Reset on a successful CONNECTED.
        self._backoff = RECONNECT_BACKOFF_START
        self._last_attempt = 0.0
        self._last_traffic = time.monotonic()  # any inbound message
        self._closed_notified = False  # rate-limit NOTICE surfaced once
        self._idle_closed = False  # parked by the idle sweep (issue #26)

    def _refill_tokens(self):
        now = time.monotonic()
        self._tokens = min(
            float(REQ_BURST), self._tokens + (now - self._last_refill) * REQ_RATE_LIMIT
        )
        self._last_refill = now
        return now

    def _next_send_wait(self):
        """Seconds to sleep before the next REQ is legal (0 if a token is
        available; consumes the token)."""
        self._refill_tokens()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        return (1.0 - self._tokens) / REQ_RATE_LIMIT

    def start(self):
        def on_msg(ws, m):
            self._last_traffic = time.monotonic()
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
                elif d[0] == "CLOSED":
                    # NIP-01: ["CLOSED", <sub_id>, <message>]. The relay
                    # refused the subscription (rate limit, full). Heavy
                    # clients in issue #26 all ignore this and keep re-firing
                    # the same REQ — the ban spiral. Here: drop the sub so it
                    # is not replayed, and surface rate-limit closures once
                    # per connection so the user sees WHY a feed is stale.
                    sub_id = d[1] if len(d) > 1 else ""
                    reason = d[2] if len(d) > 2 else ""
                    self.active_sub_ids.discard(sub_id)
                    self.snapshot_ids.discard(sub_id)
                    self.live_subs.pop(sub_id, None)  # refused: never replayed
                    self.request_queue = [
                        (sid, f) for (sid, f) in self.request_queue if sid != sub_id
                    ]
                    self.queued_sub_ids.discard(sub_id)
                    if "rate-limited" in reason and not self._closed_notified:
                        self._closed_notified = True
                        print(f"⚠️ [{self.url}] rate-limited: {reason}")
                elif d[0] == "NOTICE":
                    print(f"NOTICE [{self.url}]: {d[1]}")
            except Exception:
                print(f"❌ ERROR [{self.url}] Message Handler Failed:")
                print(f"   Msg: {m[:100]}...")
                traceback.print_exc()

        def on_open(ws):
            self.is_connected = True
            self._backoff = RECONNECT_BACKOFF_START  # flap counter resets
            self._closed_notified = False
            self._last_traffic = time.monotonic()
            GLib.idle_add(self.on_status, self.url, ConnectionState.CONNECTED)
            # Replay live subscriptions that were open before the flap
            # (issue #26: reconnect used to drop sub_following/dm_sub until
            # the next manual feed switch — DMs silently stopped arriving).
            # Snapshots are excluded: they already closed at their EOSE.
            for sid, filters in self.live_subs.items():
                self._enqueue(sid, filters)
            self.process_queue()
            self._flush_publish_queue()

        def on_err(ws, e):
            self.is_connected = False
            GLib.idle_add(self.on_status, self.url, ConnectionState.WARNING)

        def on_close(ws, c, m):
            self.is_connected = False
            if self._idle_closed:
                return  # intentional park, not a failure — keep status quiet
            GLib.idle_add(self.on_status, self.url, ConnectionState.DISCONNECTED)

        self._last_attempt = time.monotonic()
        self.ws = websocket.WebSocketApp(
            self.url,
            on_open=on_open,
            on_message=on_msg,
            on_error=on_err,
            on_close=on_close,
        )
        threading.Thread(target=self.ws.run_forever, daemon=True).start()

    def restart(self):
        """Reconnect with randomized exponential backoff (issue #26). Called
        from the periodic check_connections sweep; returns without reconnecting
        while the backoff window is still open, or while the relay is parked
        idle (only explicit activity wakes a parked relay). Randomized jitter
        (0.5x-1.5x) keeps many clients behind one NAT from reconnecting in
        lockstep."""
        if self.is_connected or self._idle_closed:
            return
        wait = self._backoff * random.uniform(0.5, 1.5)
        if time.monotonic() - self._last_attempt < wait:
            return
        self._backoff = min(self._backoff * 2.0, RECONNECT_BACKOFF_MAX)
        self.start()

    def wake(self):
        """Explicit activity on a parked (idle-closed) relay: reconnect now,
        backoff reset — the user asked for something from this relay."""
        if self.is_connected:
            return
        self._idle_closed = False
        self._backoff = RECONNECT_BACKOFF_START
        self.start()

    def close_if_idle(self, now=None):
        """Park the connection if no inbound traffic for IDLE_RELAY_TIMEOUT
        (issue #26: idle relays hold sockets and count against per-IP
        connection caps for nothing — Gossip's idle-disconnect pattern).
        Relays with an open live sub are NOT parked: the user is streaming
        from them (following feed / DM inbox) and parking would delay new
        events. live_subs survive — the next subscribe or request wakes a
        parked relay and replays them."""
        if not self.is_connected or self._idle_closed or self.live_subs:
            return False
        now = now if now is not None else time.monotonic()
        if now - self._last_traffic < IDLE_RELAY_TIMEOUT:
            return False
        self._idle_closed = True
        if self.ws:
            self.ws.close()
        return True

    def subscribe(self, sub_id, filters, snapshot=False):
        # Close the previous subscription before opening a new one, but only if
        # it's actually open on THIS relay. The old code used a single shared
        # self.sub_id and sent CLOSE for IDs the relay didn't recognize, which
        # produced "bad close: invalid subscription id length" NOTICEs and
        # "too many concurrent REQs" — degrading relay communication.
        if self.is_connected and self.sub_id and self.sub_id != sub_id:
            try:
                self.ws.send(json.dumps(["CLOSE", self.sub_id]))
            except Exception:
                pass
            self.active_sub_ids.discard(self.sub_id)
            self.live_subs.pop(self.sub_id, None)  # closed: not replayed

        if snapshot:
            self.snapshot_ids.add(sub_id)
            self.live_subs.pop(sub_id, None)
        else:
            self.live_subs[sub_id] = filters
            self.snapshot_ids.discard(sub_id)

        self.sub_id = sub_id

        if not self.is_connected:
            # Record the live sub even while down (issue #26): on_open replays
            # live_subs after a flap, so a startup or post-flap subscribe is
            # no longer silently dropped until the next manual feed switch.
            if self._idle_closed:
                self.wake()  # explicit activity: park ends, on_open replays
            return

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

    def _enqueue(self, sub_id, filters):
        """Queue a REQ for the rate-limited worker. Dedup (issue #26): the
        same sub_id enqueued twice within REQ_DEDUP_WINDOW (e.g. a profile
        opened twice while scrolling, or a widget remount storm) is one REQ,
        not two. The window is short so a deliberate later re-fetch (TTL
        expiry) still goes out."""
        now = time.monotonic()
        if sub_id in self.queued_sub_ids:
            return
        if now - self._recent_reqs.get(sub_id, -1e9) < REQ_DEDUP_WINDOW:
            return
        if len(self._recent_reqs) > 512:  # bounded, never a slow leak
            self._recent_reqs = {
                k: v for k, v in self._recent_reqs.items() if now - v < REQ_DEDUP_WINDOW
            }
        self._recent_reqs[sub_id] = now
        self.queued_sub_ids.add(sub_id)
        self.request_queue.append((sub_id, filters))
        if self.is_connected:
            self.process_queue()
        elif self._idle_closed:
            self.wake()  # queued REQs drain via process_queue in on_open

    def request_once(self, sub_id, filters):
        # Register the sub_id for EOSE auto-close (spec Phase B2a): the on_msg
        # EOSE handler CLOSEs anything in snapshot_ids, so one-shot REQs
        # (freshness pull, fetch_user_relays, fetch_profile/… — all unique
        # sub_ids) no longer leak open subscriptions on every relay.
        self.snapshot_ids.add(sub_id)
        self._enqueue(sub_id, filters)

    def process_queue(self):
        if self.is_processing_queue or not self.request_queue:
            return
        self.is_processing_queue = True

        def _worker():
            while self.request_queue and self.is_connected:
                sub_id, filters = self.request_queue.pop(0)
                self.queued_sub_ids.discard(sub_id)
                # Token bucket (issue #26): pace REQs to REQ_RATE_LIMIT/s
                # instead of the old flat 0.1 s sleep — a burst of one-shot
                # fetches (scrolling a follows list) no longer spikes past
                # relay per-IP caps, and the sleep only happens when the
                # bucket is actually empty.
                wait = self._next_send_wait()
                if wait > 0:
                    time.sleep(wait)
                try:
                    self.ws.send(
                        json.dumps(
                            ["REQ", sub_id]
                            + (filters if isinstance(filters, list) else [filters])
                        )
                    )
                    self.active_sub_ids.add(sub_id)
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

    def _flush_publish_queue(self):
        """Send EVENTs queued while this relay was down (DM-3 fallback path:
        a fallback relay is connected mid-retry and its send is queued until
        the socket opens)."""
        while self.publish_queue and self.is_connected:
            event = self.publish_queue[0]
            try:
                self.ws.send(json.dumps(["EVENT", event]))
                self.publish_queue.pop(0)
            except Exception:
                break

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
        # backfill-done: (sub_id:str) — infinite-scroll phase 2: every relay
        # that was connected when request_backfill() fired has answered EOSE
        # (or the sweep timed the batch out). Events rode in via event-received
        # and are already in the DB; the feed view re-queries its page.
        "backfill-done": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
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
        self.signer = None  # SignerBackend (P2 #15); set_keys/set_signer install it
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
        # Infinite-scroll phase 2: one in-flight history backfill at a time
        # (sub_id -> {"seen": set, "relay_count": int, "sent": float}).
        self._backfill_pending = {}
        self._dm_poll_source = None  # GLib source id for the DM freshness poll
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
        if pub is None:
            # Logout: the DM freshness poll must die with the session.
            self.stop_dm_poll()
        # P2 (#15): signing goes through a SignerBackend. A local key wraps
        # into LocalKeyBackend here so every publish path is backend-routed.
        # Imported lazily: signer imports gnostr.nostr_utils, and client.py is
        # itself imported by gnostr/__init__ — a top-level import would race
        # the package's partial initialization.
        from . import signer as _signer

        self.signer = _signer.LocalKeyBackend(priv) if priv is not None else None

    def set_signer(self, backend):
        """Install a SignerBackend (e.g. BunkerBackend). The backend owns all
        signing; my_pubkey mirrors its user pubkey and my_privkey stays None
        for remote signers (nothing in the UI path may assume a local key)."""
        self.signer = backend
        self.my_pubkey = backend.get_public_key()
        self.my_privkey = getattr(backend, "local_privkey", None)

    def _can_sign(self):
        return getattr(self, "signer", None) is not None or bool(self.my_privkey)

    def _sign(self, event):
        """Single signing funnel: backend if installed, else legacy local key
        (kept for the mocked-GObject test fakes that set my_privkey directly).
        Fills a missing pubkey: compute_event_id requires it and not every
        builder sets it (blossom BUD-01 auth events arrive without one)."""
        if not event.get("pubkey"):
            pub = getattr(self, "my_pubkey", None)
            if pub:
                event = {**event, "pubkey": pub}
        backend = getattr(self, "signer", None)
        if backend is not None:
            return backend.sign_event(event)
        if not self.my_privkey:
            return None
        return gnostr.nostr_utils.sign_event(event, self.my_privkey)

    def _dispatch_signed(self, event, label, after=None):
        """Sign + publish an unsigned event through the active backend.

        Local backends sign inline (unchanged behaviour). A remote backend
        (BunkerBackend) needs a relay round-trip plus a user approval prompt,
        which can take seconds — so it runs on a worker thread and the UI
        path returns immediately; the real confirmation still arrives via the
        OK-ack publish-result signal. `after(signed)` runs post-publish side
        effects (DB writes + signals) on both paths."""
        backend = getattr(self, "signer", None)
        if backend is not None and not getattr(backend, "is_local", True):

            def _worker():
                signed = backend.sign_event(event)
                if signed:
                    sent = self.publish(signed)
                    self._track_publish(signed, label, sent_urls=sent)
                    if after:
                        GLib.idle_add(after, signed)

            threading.Thread(target=_worker, daemon=True).start()
            return True
        signed = self._sign(event)
        if signed:
            sent = self.publish(signed)
            self._track_publish(signed, label, sent_urls=sent)
            if after:
                after(signed)
            return True
        return False

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
        """Periodic sweep (main.py: every 300 s). Two jobs (issue #26):
        - reconnect disconnected relays through NostrRelay.restart(), which
          now applies randomized exponential backoff instead of hammering a
          relay that just dropped us (the ban spiral in the issue findings);
        - park relays with no traffic and no live subscription, so idle
          sockets do not count against per-IP connection caps."""
        for url, relay in self.active_relays.items():
            if relay.close_if_idle():
                self._log_relay(f"{url} parked: idle {int(IDLE_RELAY_TIMEOUT)}s")
            elif not relay.is_connected:
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

    # --- infinite-scroll phase 2: history backfill ---
    BACKFILL_MIN_INTERVAL = 2.0  # s between backfill REQs (relay courtesy)
    BACKFILL_TIMEOUT = 10.0  # s before a stalled batch settles anyway

    def request_backfill(self, sub_id, filters):
        """One-shot REQ for feed HISTORY older than the window tail.

        Courtesy rules (infinite-scroll phase 2): at most ONE backfill in
        flight per sub_id, and never two within BACKFILL_MIN_INTERVAL —
        a user flicking the scroll to the bottom must not turn into a REQ
        storm (issue #26 ban spiral). Returns False when throttled or
        nothing was sent; True when at least one relay got the REQ.
        'backfill-done' fires once every connected relay EOSEs (or the
        sweep times the batch out).
        """
        if sub_id in self._backfill_pending:
            return False
        now = time.time()
        for entry in self._backfill_pending.values():
            if now - entry["sent"] < self.BACKFILL_MIN_INTERVAL:
                return False
        # Drop stale batches (relays that died mid-flight never EOSE).
        for stale in [
            s
            for s, e in self._backfill_pending.items()
            if now - e["sent"] > self.BACKFILL_TIMEOUT
        ]:
            self._settle_backfill(stale)
        relay_count = len(self.active_relays)
        if relay_count == 0:
            return False
        self._backfill_pending[sub_id] = {
            "seen": set(),
            "relay_count": relay_count,
            "sent": now,
        }
        self.request_once(sub_id, filters)
        return True

    def _settle_backfill(self, sub_id):
        """Resolve one backfill batch exactly once (EOSE fan-in or timeout)."""
        if self._backfill_pending.pop(sub_id, None) is not None:
            GLib.idle_add(self.emit, "backfill-done", sub_id)

    def subscribe(self, sub_id, filters, snapshot=False):
        for r in self.active_relays.values():
            r.subscribe(sub_id, filters, snapshot=snapshot)

    def publish(self, event):
        sent_urls = set()
        for url, r in self.active_relays.items():
            if r.publish(event):
                sent_urls.add(url)
        # Surface how many relays actually received the EVENT — the like flow
        # must be observable, not silently swallowed.
        self._log_relay(
            f"EVENT {event['id'][:8]} sent to "
            f"{len(sent_urls)}/{len(self.active_relays)} relay(s)"
        )
        return sent_urls

    def _build_and_publish(self, kind, content, tags, label="Event"):
        """Shared build → sign → publish for any event kind. Returns True on
        success. All social-action publishes route through here (DRY)."""
        if not self.my_pubkey or not self._can_sign():
            print("❌ No signer backend loaded")
            return False
        event = gnostr.nostr_utils.build_event(self.my_pubkey, kind, content, tags)
        return self._dispatch_signed(event, label)

    def _track_publish(self, event, label, sent_urls=None, candidates=None):
        """Register a just-published event for OK-ack resolution and DM-3
        resilient retry. The entry carries a 30s expiry; handle_ok resolves
        it, the timeout sweep retries untried candidates then emits a
        'not confirmed' result for dead relays. `sent_urls` = relays the
        EVENT already went out on; `candidates` = relays eligible for retry
        (defaults to the configured set at retry time)."""
        tried = set()
        if sent_urls:
            try:
                tried = set(sent_urls)
            except TypeError:  # fake relays in tests may return True/None
                tried = set()
        self.pending_publishes[event["id"]] = {
            "label": label,
            "expires": time.time() + 30,
            "event": event,
            "tried": tried,
            "answered": set(),
            "rejections": [],
            "candidates": set(candidates) if candidates else None,
        }

    def _retry_publish(self, event_id, pending):
        """DM-3: send the tracked event on candidate relays not yet tried
        (connecting fallback relays on demand). True if a retry went out."""
        event = pending.get("event")
        if event is None:
            return False
        candidates = set(pending.get("candidates") or self.relay_urls)
        tried = pending.setdefault("tried", set())
        untried = [u for u in candidates if u not in tried]
        if not untried:
            return False
        sent = [u for u in untried if self._send_to_relay(event, u)]
        tried.update(sent)
        if sent:
            self._log_relay(
                f"{pending['label']} {event_id[:8]} retry -> " + ", ".join(sent)
            )
        return bool(sent)

    def _send_to_relay(self, event, url):
        """Send an EVENT to one relay; a relay that isn't up yet (fallback
        still connecting) gets the event queued for its socket-open flush."""
        relay = self.active_relays.get(url)
        if relay is None:
            try:
                self.add_relay_connection(url)
            except Exception:
                return False
            relay = self.active_relays.get(url)
            if relay is None:
                return False
        if relay.publish(event):
            return True
        try:
            # Bounded: a permanently-down fallback relay must not accumulate
            # retries forever (oldest queued event is dropped past the cap).
            if len(relay.publish_queue) >= 20:
                relay.publish_queue.pop(0)
            relay.publish_queue.append(event)
            return True
        except AttributeError:
            return False

    def _fail_publish(self, event_id, pending):
        """Settle a publish as failed: one aggregated publish-result so the
        UI shows a single clear toast naming the rejecting relay."""
        self.pending_publishes.pop(event_id, None)
        rejections = pending.get("rejections") or []
        if rejections:
            last_url, last_msg = rejections[-1]
            detail = last_msg or "rejected"
            message = (
                detail
                if len(rejections) == 1
                else f"{detail} (all {len(rejections)} relays rejected)"
            )
            relay_url = last_url
        else:
            message, relay_url = "no relay acknowledged", ""
        GLib.idle_add(
            self.emit,
            "publish-result",
            event_id,
            False,
            message,
            relay_url,
            pending["label"],
        )

    def _log_relay(self, line):
        """Append a relay-activity line to the bounded log and emit the
        relay-log-updated signal so the UI can show it live."""
        ts = time.strftime("%H:%M:%S")
        full = f"[{ts}] {line}"
        self.relay_log.append(full)
        GLib.idle_add(self.emit, "relay-log-updated", full)

    def _handle_ok(self, event_id, accepted, message, relay_url):
        """NIP-01 OK ack: [\"OK\", event_id, accepted, message]. Resolves the
        pending publish for this event_id (if tracked) and emits publish-result.

        DM-3 resilient publish: the first OK accepted=true settles the publish
        (dedupe — later acks from other relays are ignored). An OK accepted=false
        logs the rejecting relay and retries the event on any candidate relay not
        yet tried; publish-result fires only once every candidate has answered
        (or the 30s sweep expires the entry)."""
        pending = self.pending_publishes.get(event_id)
        if not pending:
            return
        self._log_relay(
            f"{pending['label']} {'OK' if accepted else 'REJECTED'} {relay_url}"
            + (f" ({message})" if message else "")
        )
        if accepted:
            # First acceptance wins: settle now, ignore later relay acks.
            self.pending_publishes.pop(event_id, None)
        else:
            pending.setdefault("answered", set()).add(relay_url)
            pending.setdefault("rejections", []).append((relay_url, message))
            if self._retry_publish(event_id, pending):
                return  # retry in flight — settle later
            answered = pending.get("answered", set())
            candidates = pending.get("candidates")
            # Effective candidate set: the DM-targeted list, else the relays
            # the EVENT actually went out on. All answered with rejection -> fail.
            effective = (
                set(candidates)
                if candidates is not None
                else pending.get("tried", set())
            )
            if effective and not (effective - answered):
                self._fail_publish(event_id, pending)
                return
            # Some candidates never answered: wait for the sweep to retry/fail.
            return
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
        """Retry then settle publishes that never got a full OK round.
        Call periodically (e.g. on a timer); idempotent."""
        now = time.time()
        for eid, p in list(self.pending_publishes.items()):
            if now <= p["expires"]:
                continue
            if self._retry_publish(eid, p):
                # DM-3: untried candidates existed — give them the full window.
                p["expires"] = now + 30
                continue
            self.pending_publishes.pop(eid, None)
            rejections = p.get("rejections") or []
            if rejections:
                self._fail_publish(eid, p)
                continue
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

    def publish_post(self, content, reply_to=None, extra_tags=None):
        """kind-1 text post. `reply_to` = dict(root, parent, root_pk, parent_pk)
        to thread a reply per NIP-01 (root e-tag first, direct parent last).
        `extra_tags` = additional tag lists (e.g. NIP-92 imeta for media)."""
        if reply_to:
            tags = [
                ["e", reply_to["root"]],
                ["e", reply_to["parent"]],
                ["p", reply_to["root_pk"]],
                ["p", reply_to["parent_pk"]],
            ]
        else:
            tags = []
        if extra_tags:
            tags.extend(extra_tags)
        return self._build_and_publish(1, content, tags, label="Post")

    def publish_reaction(self, target_event_id, target_pubkey, content="+"):
        """NIP-25: kind-7 reaction. content '+' adds, '-' removes/undo."""
        if not self.my_pubkey or not self._can_sign():
            print("❌ No signer backend loaded")
            return False
        event = gnostr.nostr_utils.build_reaction_event(
            self.my_pubkey, target_event_id, target_pubkey, content
        )
        return self._dispatch_signed(event, "Like" if content == "+" else "Unlike")

    def publish_repost(
        self, target_event_id, target_pubkey, target_kind=1, original_event=None
    ):
        """NIP-18: kind-6 generic repost. content = original event JSON.
        `original_event` is the DB row; falls back to fetching it."""
        if not self.my_pubkey or not self._can_sign():
            print("❌ No signer backend loaded")
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
        return self._dispatch_signed(event, "Repost")

    def publish_contacts(self):
        """Publish the current follow list as a kind-3 contact-list event."""
        if not self._can_sign() or not self.my_pubkey:
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
        if not self._can_sign() or not self.my_pubkey:
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
        if not self.my_pubkey or not self._can_sign():
            print("❌ No signer backend loaded")
            return False
        content = json.dumps(metadata, separators=(",", ":"))
        event = gnostr.nostr_utils.build_event(self.my_pubkey, 0, content, [])

        def _after(signed):
            self.db.save_profile(self.my_pubkey, content, event["created_at"])
            GLib.idle_add(self.emit, "profile-updated", self.my_pubkey)

        return self._dispatch_signed(event, "Profile", after=_after)

    def publish_relay_list(self):
        if not self.my_pubkey or not self._can_sign():
            return
        event = {
            "pubkey": self.my_pubkey,
            "created_at": int(time.time()),
            "kind": 10002,
            "tags": [["r", u] for u in self.relay_urls],
            "content": "",
        }
        self._dispatch_signed(event, "Relay list")

    def follow_user(self, target_pubkey):
        if not self.my_pubkey or not self._can_sign():
            return
        following = self.db.get_following_list(self.my_pubkey)
        if target_pubkey in following:
            return
        following.append(target_pubkey)
        self._publish_contact_list(following, label="Follow")
        print(f"✅ Followed {target_pubkey[:8]}...")

    def unfollow_user(self, target_pubkey):
        if not self.my_pubkey or not self._can_sign():
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

        def _after(signed):
            self.db.save_contacts(self.my_pubkey, following)
            GLib.idle_add(self.emit, "contacts-updated", self.my_pubkey)

        self._dispatch_signed(event, label, after=_after)

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

        # NIP-46 transport events are signer plumbing, not feed content: route
        # them to the active BunkerBackend and keep them out of the DB, the
        # dedup set, and every feed signal.
        if ev.get("kind") == 24133:
            backend = getattr(self, "signer", None)
            if backend is not None and hasattr(backend, "handle_event"):
                try:
                    backend.handle_event(ev)
                except Exception as e:
                    print(f"❌ NIP-46 response handling failed: {e}")
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
            self.start_dm_poll()

    def start_dm_poll(self):
        """Periodic kind-4 freshness poll (DM-UX). Opening any other feed
        CLOSEs dm_sub — RelayConnection.subscribe closes the previous sub_id
        when a different one opens — so without this a new DM only surfaces
        on a manual refresh. One snapshot REQ per relay per DM_POLL_INTERVAL
        (~1/min) is two orders of magnitude under relay REQ rate limits.
        Idempotent: re-starting replaces the old timer."""
        self.stop_dm_poll()
        if not self.my_pubkey:
            return
        self._dm_poll_source = GLib.timeout_add(
            DM_POLL_INTERVAL * 1000, self._dm_poll_tick
        )

    def stop_dm_poll(self):
        if getattr(self, "_dm_poll_source", None) is not None:
            try:
                GLib.source_remove(self._dm_poll_source)
            except Exception:
                pass
            self._dm_poll_source = None

    def _dm_poll_tick(self):
        """One poll REQ: kind-4 addressed to us over a window of
        interval+slack (relays carry created_at clock skew; the overlap is
        absorbed by seen_events dedup in _handle_event). Snapshot REQ:
        auto-closes on EOSE, never replays on reconnect, never disturbs a
        live dm_sub. Returns True to keep the timer; False self-removes on
        logout (pubkey gone)."""
        if not self.my_pubkey:
            self._dm_poll_source = None
            return False
        try:
            since = int(time.time()) - DM_POLL_INTERVAL - DM_POLL_SINCE_SLACK
            self._dm_poll_seq = getattr(self, "_dm_poll_seq", 0) + 1
            # Unique sub_id per tick: REQ_DEDUP_WINDOW keys on sub_id, and a
            # repeated id would silently swallow the tick.
            self.request_once(
                f"dm_poll_{self._dm_poll_seq}",
                {"kinds": [4], "#p": [self.my_pubkey], "since": since, "limit": 50},
            )
        except Exception:
            pass  # a missed tick is fine; the next one is a minute away
        return True

    def publish_dm(self, peer_pubkey, plaintext, extra_tags=None):
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
            self.my_pubkey,
            4,
            ciphertext,
            [["p", peer_pubkey]] + (extra_tags or []),
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
            sent = self.publish_to_relays(event, relays)
            # DM-3: retry candidates are the peer's 10050 relays — a rejection
            # on one retries the untried ones (connecting them on demand), and
            # only a full round of rejections settles the publish as failed.
            self._track_publish(event, "DM", sent_urls=sent, candidates=relays)

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
        bf = getattr(self, "_backfill_pending", {}).get(sub_id)
        if bf is not None:
            # Infinite-scroll phase 2: same fan-in shape as the P4 probes —
            # settle once every relay that got the REQ has answered EOSE.
            bf["seen"].add(relay_url)
            if len(bf["seen"]) >= bf["relay_count"]:
                self._settle_backfill(sub_id)
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
        Returns the set of relay URLs the EVENT actually went out on."""
        sent = set()
        for url, r in self.active_relays.items():
            if url in urls and r.publish(event):
                sent.add(url)
        self._log_relay(
            f"EVENT {event['id'][:8]} sent to {len(sent)}/{len(urls)} DM relay(s)"
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
        sent = self.publish(signed)
        self._track_publish(signed, "DM Relays", sent_urls=sent)
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
