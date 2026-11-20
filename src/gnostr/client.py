import collections
import hashlib
import json
import os
import queue
import random
import threading
import time
import traceback

from gi.repository import GLib, GObject

import gnostr

from . import proxy_settings
from .proxy_settings import proxy_get, url_allowed
from . import groups
from . import trust_settings, trust_shield
from .util.connection_state import ConnectionState
from .util.tor_state import (TorState, host_is_onion, onion_inert,
                             probe_socks5_detail)

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
# NIP-01 hygiene: a subscription id MUST be less than 64 characters. Relays
# accept an over-long REQ id but reject the matching CLOSE ("bad close:
# invalid subscription id length" NOTICEs from relay.primal.net, nostr.mom,
# nostr.bitcoiner.social, nos.lol), leaking the subscription until the
# socket drops. Every sub_id passes through nip01_sub_id() at the client and
# relay entry points, so REQ and CLOSE always carry the same legal id.
SUB_ID_MAX_LEN = 63  # NIP-01 "<64" bound


def nip01_sub_id(sub_id):
    """Clamp a subscription id to the NIP-01 <64-char limit.

    Deterministic and idempotent: an id that already fits is returned
    unchanged (clamp(clamp(x)) == clamp(x), and client-level maps keyed by
    the id see the same string the relay echoes back in EOSE/CLOSED). An
    over-long id keeps its leading ``prefix_`` label — prefix matching such
    as startswith("thread_") keeps working — and replaces the body with a
    sha256 slice of the whole id (56 hex chars for a 7-char prefix).
    """
    if len(sub_id) < 64:
        return sub_id
    cut = sub_id.find("_")
    prefix = sub_id[: cut + 1] if cut >= 0 else ""
    budget = SUB_ID_MAX_LEN - len(prefix)
    return prefix + hashlib.sha256(sub_id.encode("utf-8")).hexdigest()[:budget]


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

# tor-v1-status (spec gnostr-tor-onion-support-v1 §A.2): fixed retry while
# DOWN is one 4-byte probe — negligible; and this many consecutive
# proxy-refused/unreachable connect failures on onion relays while REACHABLE
# is treated as proxy-died evidence and re-triggers the probe (A.2.1 item 5).
TOR_RETRY_INTERVAL = 30  # s
TOR_ONION_FAILURE_REPROBE = 3

# --- Startup ingestion batching (spec gnostr-startup-2) ---
# The startup relay burst (feed backfill + freshness REQ + DM poll +
# kind-9735 zap history) used to mean one INSERT+commit (fsync) per event
# under the single shared DB lock: hundreds of fsyncs serialized the main
# thread's switch_feed read behind the write storm — the reported startup
# hang. Plain ingestion events now buffer in a flush window and land with
# ONE executemany+commit (see _buffer_event); WAL (database.init_db) keeps
# readers unblocked between flushes.
EVENT_BATCH_SIZE = 50  # events buffered before a synchronous flush
EVENT_BATCH_FLUSH_MS = 200  # main-thread flush window for a lone straggler


class NostrRelay(GObject.Object):
    def __init__(
        self, url, on_event, on_status, on_ok=None, on_eose=None, on_connect_error=None
    ):
        super().__init__()
        self.url = url
        self.on_event = on_event
        self.on_status = on_status
        self.on_ok = on_ok or (lambda *a: None)
        self.on_eose = on_eose
        # tor-v1 (spec §A.2.1 item 5): onion relays report connect errors here
        # so the client can count proxy-died evidence and re-probe.
        self.on_connect_error = on_connect_error
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
            if self.on_connect_error:
                try:
                    self.on_connect_error(e)
                except Exception:
                    pass
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
        # SOCKS5 proxy kwargs are read at connect time (not import time) so a
        # Settings change takes effect on the next (re)connect. socks5h =
        # remote DNS: the .onion hostname is sent verbatim to the proxy and
        # Python's getaddrinfo never touches it (spec §3 DNS leak rule).
        proxy_kwargs = proxy_settings.socks_proxy_kwargs()
        threading.Thread(
            target=self.ws.run_forever, kwargs=proxy_kwargs, daemon=True
        ).start()

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
        # NIP-01: clamp before anything keys on the id (previous-CLOSE,
        # snapshot/live sets, REQ) so CLOSE always matches what was REQ'd.
        sub_id = nip01_sub_id(sub_id)
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
        sub_id = nip01_sub_id(sub_id)  # NIP-01: legal id on REQ and EOSE-close
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

    def close_sub(self, sub_id):
        """Explicitly CLOSE a subscription on this relay (group leave /
        page detach). Removes it from replay state so a flap won't reopen it."""
        sub_id = nip01_sub_id(sub_id)
        self.active_sub_ids.discard(sub_id)
        self.live_subs.pop(sub_id, None)
        self.snapshot_ids.discard(sub_id)
        if self.sub_id == sub_id:
            self.sub_id = None
        if self.is_connected and self.ws:
            try:
                self.ws.send(json.dumps(["CLOSE", sub_id]))
            except Exception:
                pass

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
        # group-event-received: (relay_url, event_json) — NIP-29 group traffic
        # routed by h-tag/39000-39005 kind BEFORE the general feed path
        # (spec nipw-communities-v1 §3).
        "group-event-received": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        "profile-updated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "contacts-updated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "status-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # tor-state-changed: (state:str) — TorState value ("disabled"|
        # "reachable"|"down"); dispatched on the main thread via GLib.idle_add
        # (spec gnostr-tor-onion-support-v1 §A.2.1).
        "tor-state-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
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
        # blossom-servers-updated: (event_json:str) — NIP-B7 kind-10063
        # (author's Blossom server list) arrived; media_resolver caches it
        # for read-side recovery of dead content-addressed media (NIPW-P1).
        "blossom-servers-updated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
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
        # trust-ranks-updated: (provider_pk:str) — a trust-shield-v1 rank
        # batch landed in trust_ranks; the Settings status line refreshes.
        "trust-ranks-updated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # zap-received: (info_json:str) — a kind-9735 receipt that passed ALL
        # spec zaps-payments-v1 §1.4 rules and was persisted to the zaps
        # table. info_json is the verify_9735 result dict. Unverified
        # receipts never fire this signal (indistinguishable from absent).
        "zap-received": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, db):
        super().__init__()
        self.active_relays = {}
        # relay_entries is the ordered source of truth (spec
        # relays-and-settings-v1 §A): [{"url", "roles", "enabled"}], list
        # order = user preference. relay_urls is a derived property below.
        self.relay_entries = None
        self.relay_urls = set(DEFAULT_RELAYS)
        self.seen_events = set()
        # Zap receipt pipeline (FuriPhone lockup 2026-11-08): the old path
        # spawned ONE THREAD PER 9735 with no dedupe — every reconnect
        # reprocessed the whole spam backlog, and each thread ran pure-
        # Python schnorr + bolt11 decode (GIL-bound) plus an uncached 10s
        # LNURL timeout. Dozens of such threads saturated the GIL and the
        # main loop froze until the flood drained. Now: one worker thread,
        # event-id dedupe, negative metadata cache, rate-limited logging.
        self._zap_queue = queue.Queue()
        self._zap_worker = None
        self._zap_worker_lock = threading.Lock()
        self._zap_seen = collections.deque(maxlen=4096)
        self._zap_seen_set = set()
        self._zap_meta_negative = {}  # recipient_pk -> monotonic expiry
        self._zap_reject_logged = {}  # reason prefix -> monotonic
        # gnostr-startup-2: batched ingestion buffer (see _buffer_event).
        # Reader threads append plain events here; they land in the DB in
        # ONE executemany+commit per flush window instead of one fsync each.
        self._event_buffer = []
        self._event_buffer_lock = threading.Lock()
        self._flush_scheduled = False  # idle_add coalescing flag
        self.db = db
        # event_id -> {"label": str, "expires": float} for OK-ack resolution
        self.pending_publishes = {}
        # Bounded relay-activity log (newest last); surfaced via relay-log-updated
        self.relay_log = collections.deque(maxlen=200)
        # RELSET-3 §B: per-relay facts for the Settings details expander
        # (connection state / last error / last publish). In-memory only —
        # they describe the live session, not user intent.
        self.relay_status = {}
        self.relay_last_error = {}
        self.relay_last_publish = {}
        # RELSET-3 §B: last-used Settings page id, persisted in config.json
        # (spec AC6; tux change 6 — AdwPreferencesWindow does not do this).
        self.settings_page = None
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
        # --- trust-shield-v1 (NIP-85 WoT spam collapse; default OFF) ---
        # All inert while `trust-shield` is false (flag-off = byte-for-byte
        # pre-feature behavior). In-memory by design: pending/revealed
        # describe this session; durable ranks live in the trust_ranks table.
        self._trust_pending = set()  # rendered authors awaiting a rank REQ
        self._trust_no_rank = set()  # answered 'no rank' this session: no refetch
        self._trust_revealed = set()  # 'Show post' session reveals
        self._trust_batch = None  # in-flight {subjects, fallback} (single flight)
        self._trust_flush_source = None  # 30 s coalescing timer
        self._trust_seq = 0
        self._trust_dead = False  # provider unreachable -> shield inert this session
        self._trust_zero_rounds = 0  # consecutive #d-empty EOSE answers (Q1)
        self._trust_fallback_used = False  # limit:500 fallback fires once
        self._trust_follows = None  # follows cache; contacts-updated invalidates
        self.connect("contacts-updated", self._on_contacts_for_trust)
        # --- tor-v1-status (spec gnostr-tor-onion-support-v1 §A.2) ---
        # TorState is derived, never stored twice: proxy-enabled decides
        # DISABLED, the probe decides REACHABLE/DOWN. _tor_probe_reason
        # distinguishes the auth-required tooltip; _tor_probe_inflight keeps
        # overlapping schedule triggers from stacking worker threads;
        # _tor_retry_source is the single 30 s DOWN timer; _tor_onion_failures
        # counts consecutive proxy-refused onion connect failures while
        # REACHABLE (A.2.1 item 5).
        self.tor_state = (
            TorState.DISABLED
            if not proxy_settings.proxy_get("proxy-enabled")
            else TorState.DOWN
        )
        self._tor_probe_reason = "ok"
        self._tor_probe_inflight = False
        self._tor_retry_source = None
        self._tor_onion_failures = 0
        self.config_file = os.path.join(
            GLib.get_user_config_dir(), "gnostr", "config.json"
        )
        self.load_config()
        # Fresh install (no config yet): the default relays become entries
        # too, so the entry API always has a list to work with. Not saved
        # until the first mutation — a read-only startup writes no file.
        if self.relay_entries is None:
            self.relay_entries = gnostr.relay_entries.entries_from_legacy(
                DEFAULT_RELAYS
            )
        # A.2.1 item 1: probe at startup iff proxy-enabled.
        if proxy_settings.proxy_get("proxy-enabled"):
            self.probe_tor()

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r") as f:
                    data = json.load(f)
            except Exception:
                return
            re_ = gnostr.relay_entries.normalize_entries(data.get("relay_entries"))
            if re_:
                self.relay_entries = re_
            elif data.get("relays"):
                # Legacy migration (spec §A): derive entries from the flat
                # list (sorted, default roles) and persist immediately.
                self.relay_entries = gnostr.relay_entries.entries_from_legacy(
                    data.get("relays")
                )
                self.save_config()
            page = data.get("settings_page")
            if isinstance(page, str) and page:
                self.settings_page = page

    def set_settings_page(self, page_id):
        """Persist the last-used Settings page (spec §B AC6). No write when
        unchanged — page switches must not spam the config file."""
        if not page_id or page_id == self.settings_page:
            return
        self.settings_page = page_id
        self.save_config()

    def save_config(self):
        d = os.path.dirname(self.config_file)
        if not os.path.exists(d):
            os.makedirs(d, exist_ok=True)
        try:
            payload = {}
            if getattr(self, "relay_entries", None):
                payload["relay_entries"] = self.relay_entries
                # Legacy mirror so older builds reading "relays" never lose
                # the set (derived from entries, enabled only).
                payload["relays"] = gnostr.relay_entries.enabled_urls(
                    self.relay_entries
                )
            else:
                payload["relays"] = list(self.relay_urls)
            if getattr(self, "settings_page", None):
                payload["settings_page"] = self.settings_page
            with open(self.config_file, "w") as f:
                json.dump(payload, f)
        except Exception:
            pass

    # --- relay entries: derived relay_urls + entry API (spec §A) -----------
    #
    # Public surface for the Settings UI card (RELSET-3):
    #   relay_entries            list of {"url", "roles", "enabled"} (order =
    #                            user preference; mutate ONLY via the API below)
    #   relay_urls               derived set: enabled entries' urls (legacy)
    #   add_relay_entry(url, roles=None)     append (default roles read+write)
    #   remove_relay_entry(url)              delete entry + close connection
    #   set_relay_roles(url, roles)          replace roles (canonical order)
    #   set_relay_enabled(url, enabled)      park / unpark (intent persists)
    #   move_relay_entry(url, delta)         reorder (-1 up, +1 down)
    #   import_relays_from_profile(urls)     merge, keep roles/order
    # Every mutation persists via save_config() and emits relay-list-updated;
    # membership changes re-publish kind-10002 (debounced).

    @property
    def relay_urls(self):
        """Derived: urls of ENABLED entries, in preference order. When no
        entry model is present (legacy fakes / pre-migration), returns the
        plain set assigned directly."""
        entries = self.__dict__.get("relay_entries")
        if entries is not None:
            return set(gnostr.relay_entries.enabled_urls(entries))
        return self.__dict__.get("_relay_urls_fallback", set())

    @relay_urls.setter
    def relay_urls(self, urls):
        urls = set(urls or [])
        if self.__dict__.get("relay_entries") is not None:
            # Keep the entry model in sync: add missing, drop absent. Parked
            # entries survive — relay_urls only ever lists enabled urls, so
            # a legacy set-assignment must not silently delete parking.
            entries = self.relay_entries or []
            keep = [
                e for e in entries if e["url"] in urls or not e.get("enabled", True)
            ]
            added = {e["url"] for e in keep}
            keep.extend(
                gnostr.relay_entries.make_entry(u) for u in sorted(urls - added)
            )
            self.relay_entries = keep
        else:
            self.__dict__["_relay_urls_fallback"] = urls

    def add_relay_entry(self, url, roles=None):
        """Append an entry (list order = preference; new relays go last).
        Default roles [read, write]. Connects + re-publishes 10002."""
        re_ = gnostr.relay_entries
        entry = re_.make_entry(url, roles)
        if not entry["url"] or re_.entry_for(self.relay_entries, entry["url"]):
            return False
        self.relay_entries.append(entry)
        self.save_config()
        if host_is_onion(entry["url"]) and self.tor_state != TorState.REACHABLE:
            self.probe_tor()
        self.add_relay_connection(entry["url"])
        self.emit("relay-list-updated")
        self._schedule_relay_list_publish()
        return True

    def remove_relay_entry(self, url):
        url = str(url).rstrip("/")
        entry = gnostr.relay_entries.entry_for(self.relay_entries, url)
        if entry is None:
            return False
        self.relay_entries.remove(entry)
        self.save_config()
        if url in self.active_relays:
            self.active_relays[url].close()
            del self.active_relays[url]
        self.emit("relay-list-updated")
        self._schedule_relay_list_publish()
        return True

    def set_relay_roles(self, url, roles):
        """Replace an entry's roles (canonical order applied)."""
        entry = gnostr.relay_entries.entry_for(self.relay_entries, url)
        if entry is None:
            return False
        entry["roles"] = gnostr.relay_entries.normalize_roles(roles)
        self.save_config()
        self.emit("relay-list-updated")
        self._schedule_relay_list_publish()
        return True

    def set_relay_enabled(self, url, enabled):
        """Park (False) or unpark (True) an entry. Parked entries persist but
        never connect — distinct from the idle-park mechanism (spec §A)."""
        entry = gnostr.relay_entries.entry_for(self.relay_entries, url)
        if entry is None:
            return False
        entry["enabled"] = bool(enabled)
        self.save_config()
        if not entry["enabled"]:
            if url in self.active_relays:
                self.active_relays[url].close()
                del self.active_relays[url]
        else:
            self.add_relay_connection(url)
        self.emit("relay-list-updated")
        self._schedule_relay_list_publish()
        return True

    def move_relay_entry(self, url, delta):
        """Reorder one entry (-1 up, +1 down); persists the new order."""
        entries = self.relay_entries or []
        entry = gnostr.relay_entries.entry_for(entries, url)
        if entry is None:
            return False
        i = entries.index(entry)
        j = max(0, min(len(entries) - 1, i + int(delta)))
        if i == j:
            return False
        entries.insert(j, entries.pop(i))
        self.save_config()
        self.emit("relay-list-updated")
        # Spec §B: reorder is a mutation too — 10002 tag order follows
        # preference order, so re-publish (debounced).
        self._schedule_relay_list_publish()
        return True

    def import_relays_from_profile(self, urls):
        """Import-from-profile merge: new relays append with default roles,
        existing entries keep their roles and order. Returns added urls."""
        added = gnostr.relay_entries.merge_imported(self.relay_entries, urls)
        if added:
            self.save_config()
            for u in added:
                self.add_relay_connection(u)
            self.emit("relay-list-updated")
            self._schedule_relay_list_publish()
        return added

    def _schedule_relay_list_publish(self):
        """Debounced kind-10002 re-publish (same pattern as today's relay
        add/remove): a burst of UI mutations lands one event. Falls back to
        an immediate publish when no main loop can arm the timer."""
        timer = getattr(self, "_relay_list_timer", None)
        if timer is not None:
            try:
                GLib.source_remove(timer)
                self._relay_list_timer = None
            except Exception:
                pass
        try:
            self._relay_list_timer = GLib.timeout_add_seconds(
                2, self._publish_relay_list_now
            )
        except Exception:
            self._publish_relay_list_now()
        return False

    def _publish_relay_list_now(self):
        self._relay_list_timer = None
        self.publish_relay_list()
        return False

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
        # Gate lives in add_relay_connection (A.2.2): inert onion relays are
        # never instantiated here either — no connect attempts, no retry
        # storms, and they never touch the aggregate status-changed.
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
        # tor-v1 skip gate (spec §A.2.2): an onion relay is inert unless the
        # proxy is enabled AND reachable. Inert relays stay in relay_urls
        # (persisted list untouched) but are never instantiated as NostrRelay.
        if onion_inert(url, proxy_get("proxy-enabled"), self.tor_state):
            return
        # Scheme policy (spec gnostr-tor-onion-support-v1 §3): ws:// (no TLS)
        # is only legal for .onion hosts reached through the SOCKS proxy.
        # A disallowed endpoint is a HARD FAIL with a visible error — the URL
        # is never rewritten and there is never a silent clearnet fallback.
        if not url_allowed(url, proxy_get("proxy-enabled")):
            self._log_relay(
                f"{url} refused: ws:// without TLS is only allowed for "
                ".onion relays with the SOCKS proxy enabled"
            )
            self._handle_status(url, ConnectionState.WARNING)
            return
        r = NostrRelay(
            url,
            lambda ev, u=url: self._handle_event(ev, relay_url=u),
            self._handle_status,
            on_ok=self._handle_ok,
            on_eose=self._handle_eose,
            on_connect_error=(
                (lambda err, u=url: self._on_relay_connect_error(u, err))
                if host_is_onion(url)
                else None
            ),
        )
        r.start()
        self.active_relays[url] = r

    # --- tor-v1-status: probe schedule + transitions (spec §A.2.1/A.2.2) ---

    def probe_tor(self):
        """Run the SOCKS5 greeting probe on a worker thread; dispatch the
        resulting state via GLib.idle_add (house pattern). Overlapping
        triggers collapse into the in-flight probe (no thread stacking)."""
        if not proxy_get("proxy-enabled"):
            self._set_tor_state(TorState.DISABLED)
            return
        if self._tor_probe_inflight:
            return
        self._tor_probe_inflight = True
        host = proxy_get("proxy-host")
        port = proxy_get("proxy-port")

        def _worker():
            try:
                ok, reason = probe_socks5_detail(host, port)
            finally:
                self._tor_probe_inflight = False
            GLib.idle_add(
                self._set_tor_state,
                TorState.REACHABLE if ok else TorState.DOWN,
                reason,
            )

        threading.Thread(target=_worker, daemon=True).start()

    def on_proxy_setting_changed(self, _key=None):
        """Any proxy-* GSettings change re-probes immediately (A.2.1 item 2).
        proxy-enabled false→true re-probes first and connects onion relays
        only on REACHABLE; true→false demotes to DISABLED (relays inert)."""
        self.probe_tor()

    def _set_tor_state(self, state, reason="ok"):
        """Single mutation point for tor_state; runs on the main thread
        (probe dispatches here via GLib.idle_add)."""
        old = self.tor_state
        changed = state != old or reason != self._tor_probe_reason
        self.tor_state = state
        self._tor_probe_reason = reason
        if state == TorState.REACHABLE:
            self._tor_onion_failures = 0
            self._cancel_tor_retry()
            if old != TorState.REACHABLE:
                # A.2.2: connect exactly the previously-inert onion relays,
                # never a full connect_all().
                self._connect_inert_onion_relays()
                self._log_relay("tor: proxy reachable — onion relays enabled")
        elif state == TorState.DOWN:
            n = self._close_onion_relays()
            if old != TorState.DOWN:
                self._log_relay(f"tor: proxy down — {n} onion relay(s) skipped")
            self._start_tor_retry()
        else:  # DISABLED
            self._close_onion_relays()
            self._cancel_tor_retry()
        if changed:
            GLib.idle_add(self.emit, "tor-state-changed", state.value)
        return False  # usable directly from GLib.idle_add

    def _connect_inert_onion_relays(self):
        for url in list(self.relay_urls):
            if host_is_onion(url) and url not in self.active_relays:
                self.add_relay_connection(url)

    def _close_onion_relays(self):
        """Close live onion relays on a DOWN/DISABLED transition. Marked
        _idle_closed first so the intentional close stays quiet in the
        aggregate status (the sidebar must not sit red while clearnet relays
        are healthy and tor is merely down)."""
        n = 0
        for url in list(self.active_relays):
            if host_is_onion(url):
                r = self.active_relays.pop(url)
                r._idle_closed = True
                r.close()
                n += 1
        return n

    def _start_tor_retry(self):
        """While DOWN: one fixed 30 s retry timer (A.2.1 item 4)."""
        if self._tor_retry_source is not None:
            return

        def _tick():
            self._tor_retry_source = None
            if self.tor_state == TorState.DOWN:
                self.probe_tor()
            return GLib.SOURCE_REMOVE

        self._tor_retry_source = GLib.timeout_add_seconds(TOR_RETRY_INTERVAL, _tick)

    def _cancel_tor_retry(self):
        if self._tor_retry_source is not None:
            GLib.source_remove(self._tor_retry_source)
            self._tor_retry_source = None

    def _on_relay_connect_error(self, url, err):
        """A.2.1 item 5: while REACHABLE, 3 consecutive proxy-refused/
        unreachable connect failures on onion relays are proxy-died evidence
        and re-trigger the probe. Anything else is ordinary relay noise."""
        if self.tor_state != TorState.REACHABLE:
            return
        low = str(err).lower()
        if any(t in low for t in ("refused", "unreachable", "socks", "proxy")):
            self._tor_onion_failures += 1
            if self._tor_onion_failures >= TOR_ONION_FAILURE_REPROBE:
                self._tor_onion_failures = 0
                self.probe_tor()

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
        # Legacy entry point (dialogs): now a thin alias for the entry API —
        # new relays append with default roles [read, write].
        self.add_relay_entry(url)

    def remove_relay(self, url):
        self.remove_relay_entry(url)

    def fetch_user_relays(self):
        if not self.my_pubkey:
            return
        filter = {"kinds": [10002], "authors": [self.my_pubkey], "limit": 1}
        sub_id = f"relays_{self.my_pubkey[:8]}_{int(time.time())}"
        for r in self.active_relays.values():
            r.request_once(sub_id, filter)

    def request_once(self, sub_id, filters):
        # NIP-01: clamp at the client level too, so client-side maps keyed
        # by sub_id (backfill fan-in, dm-status probes) see the same id the
        # relay echoes back in EOSE/CLOSED. Idempotent — relay-side clamp
        # is a no-op on the already-legal id.
        sub_id = nip01_sub_id(sub_id)
        # Role routing (spec §A): one-shot REQs are reads — read-role relays.
        for r in self._read_relays().values():
            r.request_once(sub_id, filters)

    # --- zap receipts (spec zaps-payments-v1 §1.4/§2.3) --------------------

    def subscribe_zaps(self, pubkeys):
        """Live REQ for kind-9735 receipts whose recipient p-tag is one of
        `pubkeys` (followed profiles + self). Role routing comes free via
        subscribe() → _read_relays() (RELSET-2)."""
        authors = [p for p in dict.fromkeys(pubkeys or []) if p]
        if not authors:
            return
        self.subscribe(
            "sub_zaps",
            {"kinds": [9735], "#p": authors[:300], "limit": 50},
        )

    def _enqueue_zap_receipt(self, ev):
        """Queue one 9735 for the single verification worker.

        Dedupe on the event id BEFORE any expensive work: relays re-send
        the whole receipt window on every (re)subscribe, and rejected
        receipts never enter seen_events, so without this the entire spam
        backlog is re-verified on every reconnect. Bounded FIFO so the
        dedupe set cannot grow without limit."""
        eid = ev.get("id") or ""
        if not eid or eid in self._zap_seen_set:
            return
        if len(self._zap_seen) == self._zap_seen.maxlen:
            self._zap_seen_set.discard(self._zap_seen[0])
        self._zap_seen.append(eid)
        self._zap_seen_set.add(eid)
        self._zap_queue.put(ev)
        if self._zap_worker is None:
            with self._zap_worker_lock:
                if self._zap_worker is None:
                    self._zap_worker = threading.Thread(
                        target=self._zap_worker_loop, daemon=True
                    )
                    self._zap_worker.start()

    def _zap_worker_loop(self):
        while True:
            ev = self._zap_queue.get()
            try:
                self._process_zap_receipt(ev)
            except Exception:
                pass  # one bad receipt must not kill the pipeline

    def _zap_metadata_lookup(self, recipient_pk):
        """Rule 1 helper: LNURL server nostrPubkey for `recipient_pk`.
        Cached kind-0 -> resolve_target -> lnurl.fetch_metadata (1h TTL
        cache inside ZAPS-A lnurl module). None when the recipient has no
        zapable profile — caller treats that as a hard reject."""
        try:
            from .zaps import lnurl as _lnurl

            now = time.monotonic()
            if self._zap_meta_negative.get(recipient_pk, 0) > now:
                return None
            prof = self.db.get_profile(recipient_pk)
            if not prof:
                return None
            target = _lnurl.resolve_target(prof)
            if not target:
                return None
            return _lnurl.fetch_metadata(target)["nostrPubkey"]
        except Exception:
            # Negative cache (15 min): an unreachable LNURL server costs
            # the full 10s HTTP timeout per receipt otherwise — a spam
            # flood then stalls the worker for minutes at a time.
            self._zap_meta_negative[recipient_pk] = time.monotonic() + 900
            return None

    def _process_zap_receipt(self, ev):
        """Off-main-thread §1.4 gate for one 9735 (see _handle_event)."""
        try:
            from .zaps.receipt import process_9735

            info, reason = process_9735(
                ev, self.db, metadata_lookup=self._zap_metadata_lookup
            )
            if info is not None:
                GLib.idle_add(self.emit, "zap-received", json.dumps(info))
            else:
                # Rate-limit: spam floods print the same reason hundreds
                # of times (one line per reason per minute).
                prefix = (reason or "?").split(":")[0]
                now = time.monotonic()
                if now - self._zap_reject_logged.get(prefix, -60) >= 60:
                    self._zap_reject_logged[prefix] = now
                    print(f"🚫 zap receipt rejected: {reason}")
        except Exception as e:
            print(f"❌ zap receipt handling failed: {e}")

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
        # NIP-01: clamp at the client level (see request_once) so every
        # relay receives — and later CLOSEs — the identical legal id.
        sub_id = nip01_sub_id(sub_id)
        # Role routing (spec §A): feed/REQ subscriptions go to read-role
        # relays only. Fallback to every connected relay when the entry
        # model is absent (legacy fakes) or no relay carries `read` — a
        # client that reads from nothing would show an empty feed.
        for url, r in self._read_relays().items():
            r.subscribe(sub_id, filters, snapshot=snapshot)

    def _routed_relays(self, role):
        """Connected relays carrying `role`. When the entry model is absent
        (legacy fakes / pre-migration state) every connected relay qualifies;
        with entries present the routing is strict (acceptance criterion 2:
        a write-only relay receives no REQs, a read-only relay no EVENTs)."""
        active = self.active_relays
        entries = getattr(self, "relay_entries", None)
        if not entries:
            return dict(active)
        urls = set(gnostr.relay_entries.urls_for_role(entries, role))
        return {u: r for u, r in active.items() if u in urls}

    def _read_relays(self):
        return self._routed_relays("read")

    def _write_relays(self):
        return self._routed_relays("write")

    def publish(self, event):
        # Role routing (spec §A): publishes fan out to write-role relays.
        targets = self._write_relays()
        sent_urls = set()
        for url, r in targets.items():
            if r.publish(event):
                sent_urls.add(url)
        # Surface how many relays actually received the EVENT — the like flow
        # must be observable, not silently swallowed.
        self._log_relay(
            f"[w] EVENT {event['id'][:8]} sent to "
            f"{len(sent_urls)}/{len(targets)} relay(s)"
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
        candidates = pending.get("candidates")
        if not candidates:
            # Role routing (spec §A): the default retry set is write-role
            # relays when the entry model exists — a retry is still a
            # publish, and a read-only relay must never receive an EVENT
            # (acceptance criterion 2). Legacy state retries every relay.
            entries = getattr(self, "relay_entries", None)
            candidates = (
                gnostr.relay_entries.urls_for_role(entries, "write")
                if entries
                else list(self.relay_urls)
            )
        candidates = set(candidates)
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
        tag = self._role_tag(relay_url)
        self._log_relay(
            f"{tag + ' ' if tag else ''}{pending['label']} "
            f"{'OK' if accepted else 'REJECTED'} {relay_url}"
            + (f" ({message})" if message else "")
        )
        # RELSET-3 §B: per-relay last publish result for the details expander.
        # Defensive getattr: ast-extracted fakes in the older suites predate
        # this dict and must keep working unchanged.
        store = getattr(self, "relay_last_publish", None)
        if store is not None:
            store[relay_url] = (
                f"{pending['label']} {'OK' if accepted else 'REJECTED'}"
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
        entries = getattr(self, "relay_entries", None)
        if entries:
            # NIP-65 read/write markers per entry roles (spec §A).
            tags = gnostr.relay_entries.relay10002_tags(entries)
        else:
            tags = [["r", u] for u in self.relay_urls]
        event = {
            "pubkey": self.my_pubkey,
            "created_at": int(time.time()),
            "kind": 10002,
            "tags": tags,
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

    def _buffer_event(self, ev):
        """Queue a plain ingestion event for batched persistence
        (gnostr-startup-2). Called from relay reader threads per event.

        The batch lands in ONE executemany+commit either when it reaches
        EVENT_BATCH_SIZE (flushed here, synchronously, on the calling
        thread) or on the next main-loop idle hop — a single idle is
        attached only when the buffer transitions empty -> non-empty, so a
        lone straggler still lands within the flush window without
        re-attaching a source per event. Dedup already happened via
        seen_events; INSERT OR IGNORE covers relay echoes inside a batch."""
        flush_now = False
        schedule_idle = False
        with self._event_buffer_lock:
            self._event_buffer.append(ev)
            if len(self._event_buffer) >= EVENT_BATCH_SIZE:
                flush_now = True
            elif not self._flush_scheduled:
                self._flush_scheduled = True
                schedule_idle = True
        # Never call out while holding the buffer lock: the flush re-takes it
        # (threading.Lock is not reentrant), and the idle_add/flush must stay
        # outside the critical section for any synchronous idle stub too.
        if flush_now:
            self._flush_event_buffer()
        elif schedule_idle:
            GLib.idle_add(self._flush_event_buffer)

    def _flush_event_buffer(self):
        """Drain the event buffer into the DB with ONE executemany+commit.
        Safe from any thread (Database.save_events takes the DB lock).
        Returns False so it doubles as a one-shot GLib idle callback."""
        with self._event_buffer_lock:
            batch = self._event_buffer
            self._event_buffer = []
            self._flush_scheduled = False
        if batch:
            self.db.save_events(batch)
        return False

    def _flush_and_emit(self, signal_name, *args):
        """Idle callback preserving the 'saved before emitted' contract:
        UI handlers read the just-arrived row out of the DB
        (main.on_event_received -> get_event_by_id, thread_view ->
        get_replies), so the buffer MUST be committed before the signal
        fires. Commit first, then emit. Returns False (one-shot idle)."""
        self._flush_event_buffer()
        self.emit(signal_name, *args)
        return False

    def _handle_event(self, ev, relay_url=None):
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

        # Zap receipts (spec zaps-payments-v1 §1.4): the §1.4 gate runs off
        # the main thread (rule 1 may fetch LNURL metadata); verified
        # receipts persist to the zaps table and fire zap-received. An
        # unverified receipt is dropped here and never reaches the DB or
        # any feed signal — indistinguishable from absent (card constraint).
        if kind == 9735:
            self._enqueue_zap_receipt(ev)
            return

        # Dedup FIRST so re-subscribed events (e.g. thread refresh) are never
        # re-counted into metrics or re-saved/re-emitted.
        if eid in self.seen_events:
            return
        self.seen_events.add(eid)

        # NIP-29 routing (spec nipw-communities-v1 §3): group traffic is
        # h-tagged or in the 39000-39005 range and belongs to the GROUP page
        # only — it must never leak into the general feed signals or the
        # general events table. Emitted on the main thread like every other
        # signal (RelayConnection callbacks arrive on the socket thread).
        if relay_url and groups.is_group_event(ev):
            _gid, _kind = groups.group_of(ev)
            GLib.idle_add(
                self.emit, "group-event-received", relay_url, json.dumps(ev)
            )
            # Cache group timeline traffic only. 39000-39005 metadata is NOT
            # persisted here — the page layer persists it only after the
            # NIP-11 self-key signature gate (parse_group_metadata) passes.
            gw = getattr(self, "_groups_gateway", None)
            if gw is not None and _gid and _kind is not None and not (
                39000 <= _kind <= 39005
            ):
                try:
                    gw.save_event(relay_url, _gid, ev)
                except Exception as e:
                    print(f"❌ groups gateway: {e}")
            return

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

        # gnostr-startup-2: plain events (including the kind-4 rumor row in
        # the events table) go through the batched buffer — ONE
        # executemany+commit per flush window instead of one fsync per event.
        # The DM gateway's gw.save_event below deliberately stays per-event:
        # dm_conversations rollups (unread_count, last_activity, direction)
        # are per-rumor bookkeeping, and DM volume (one poll per minute,
        # limit 50) never contributes to the ingestion storm.
        self._buffer_event(ev)

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

        elif kind == 10063:
            # NIP-B7: author's Blossom server list (NIPW-P1 read-side media
            # recovery). Forwarded whole; media_resolver parses + caches it.
            GLib.idle_add(self.emit, "blossom-servers-updated", json.dumps(ev))

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
            # Invariant (main.on_event_received hydrates the row via
            # get_event_by_id, thread_view via get_replies): the event must
            # be committed BEFORE the signal fires. _flush_and_emit commits
            # the buffer first, then emits — batching stays intact because
            # the flush drains every event queued so far, not just this one.
            GLib.idle_add(
                self._flush_and_emit,
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
        # Import-from-profile merge (spec §A): entries keep roles/order, new
        # relays append with defaults. The legacy set path stays for fakes
        # that carry no entry model.
        if getattr(self, "relay_entries", None) is not None:
            self.import_relays_from_profile(new_list)
            return
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
        """Publish a signed DM rumor per spec §A routing:
        dm-role relays first (list order = priority); else the existing
        NIP-17 peer-10050 targeting (async, never blocks the UI thread);
        if the peer has no 10050, fall back to write-role relays. With no
        entry model at all (legacy state) the peer-targeting flow — including
        the dm-unreachable abort — is unchanged (acceptance criterion 3)."""
        entries = getattr(self, "relay_entries", None)
        dm_urls = gnostr.relay_entries.urls_for_role(entries, "dm") if entries else []
        if dm_urls:
            # Explicit user intent: DMs only through dm-role relays.
            sent = self.publish_to_relays(event, dm_urls)
            self._track_publish(event, "DM", sent_urls=sent, candidates=dm_urls)
            return

        def _deliver(relays):
            if relays:
                sent = self.publish_to_relays(event, relays)
                # DM-3: retry candidates are the peer's 10050 relays — a
                # rejection on one retries the untried ones (connecting them
                # on demand), and only a full round of rejections settles the
                # publish as failed.
                self._track_publish(event, "DM", sent_urls=sent, candidates=relays)
                return
            write_urls = (
                gnostr.relay_entries.urls_for_role(entries, "write") if entries else []
            )
            if write_urls:
                # Spec chain last resort: peer has no 10050 -> write relays.
                self._log_relay(
                    f"DM {event['id'][:8]}: {peer_pubkey[:8]} has no "
                    "kind-10050 — falling back to write relays"
                )
                sent = self.publish_to_relays(event, write_urls)
                self._track_publish(event, "DM", sent_urls=sent, candidates=write_urls)
                return
            self._log_relay(
                f"DM {event['id'][:8]} aborted: {peer_pubkey[:8]} has no kind-10050"
            )
            GLib.idle_add(self.emit, "dm-unreachable", peer_pubkey, event["id"])

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
            f"[d] EVENT {event['id'][:8]} sent to {len(sent)}/{len(urls)} DM relay(s)"
        )
        return sent

    # --- NIP-29 group routing (spec nipw-communities-v1 §3) -----------------

    def subscribe_on_relay(self, url, sub_id, filters, snapshot=False):
        """Open a subscription on ONE specific relay (group relays are the
        group's own hint URL — group REQs must never fan out to read-role
        relays). Connects the relay on demand. Returns True if dispatched."""
        if url not in self.active_relays:
            self.add_relay_connection(url)
        r = self.active_relays.get(url)
        if r is None:
            return False
        r.subscribe(sub_id, filters, snapshot=snapshot)
        return True

    def close_sub_on_relay(self, url, sub_id):
        r = self.active_relays.get(url)
        if r is not None:
            r.close_sub(sub_id)

    def publish_to_relay(self, url, event, label="Group event"):
        """Publish to exactly one relay (group 9021/9022/posts stay on the
        group relay — never general relays). Registers the OK-ack like
        publish(); `publish-result` resolves as usual, so the join state
        machine can drive off OK callbacks."""
        if url not in self.active_relays:
            self.add_relay_connection(url)
        sent = self.publish_to_relays(event, {url})
        self._track_publish(event, label, sent_urls=sent, candidates={url})
        return bool(sent)

    def dispatch_signed_to_relay(self, event, url, label="Group event"):
        """Sign an unsigned event through the active backend (local inline,
        bunker on a worker thread — same split as _dispatch_signed) and
        publish it to ONE relay only. Group events must never fan out to
        the write relays."""
        backend = getattr(self, "signer", None)
        if backend is not None and not getattr(backend, "is_local", True):
            def _worker():
                signed = backend.sign_event(event)
                if signed:
                    self.publish_to_relay(url, signed, label)

            threading.Thread(target=_worker, daemon=True).start()
            return True
        signed = self._sign(event)
        if signed:
            return self.publish_to_relay(url, signed, label)
        return False

    def get_groups_gateway(self):
        """Lazy SQLiteGroupsGateway (same pattern as the DM gateway). Tests
        that never call this keep _groups_gateway None and the client stays
        filesystem-free."""
        gw = getattr(self, "_groups_gateway", None)
        if gw is None:
            from gnostr.gateway.groups_gateway import SQLiteGroupsGateway

            gw = SQLiteGroupsGateway()
            gw.init_db()
            self._groups_gateway = gw
        return gw

    def _role_tag(self, url):
        """Compact role tag for a relay's activity-log lines (spec §A.6):
        read→r, write→w, dm→d, e.g. '[rw]'. Empty string when the entry model
        is absent (legacy log lines stay byte-identical)."""
        entries = getattr(self, "relay_entries", None)
        if not entries:
            return ""
        entry = gnostr.relay_entries.entry_for(entries, url)
        if not entry:
            return ""
        abbr = {"read": "r", "write": "w", "dm": "d"}
        return "[" + "".join(abbr[r] for r in entry.get("roles") or []) + "]"

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
        # Log relay connect/disconnect so the activity pane shows
        # connectivity (with the entry's role tag, spec §A.6).
        tag = self._role_tag(url)
        self._log_relay(f"{tag + ' ' if tag else ''}{url} {status.status}")
        # RELSET-3 §B: per-relay facts feed the Settings details expander.
        # An ERROR/WARNING transition also records the last error for that
        # relay; CONNECTED clears a stale one. Defensive getattr: ast-
        # extracted fakes in the older suites predate these dicts — the
        # emit below must fire for them exactly as before.
        store = getattr(self, "relay_status", None)
        if store is not None:
            store[url] = status
            if status.status == "CONNECTED":
                getattr(self, "relay_last_error", {}).pop(url, None)
            elif status.status in ("ERROR", "WARNING"):
                getattr(self, "relay_last_error", {})[url] = status.details
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

    def fetch_blossom_servers(self, pubkey):
        """NIP-B7: request an author's kind-10063 Blossom server list
        (NIPW-P1 read-side media recovery). The answer arrives via the
        'blossom-servers-updated' signal; media_resolver TTL-caches it per
        author (1 h) and dedups in-flight requests, so no cache is needed
        here."""
        if not pubkey:
            return
        for r in self.active_relays.values():
            r.request_once(
                f"blossom_{pubkey[:8]}",
                {"kinds": [10063], "authors": [pubkey], "limit": 1},
            )

    def fetch_thread(self, root_id):
        f1 = {"ids": [root_id]}
        f2 = {"kinds": [1], "#e": [root_id], "limit": 50}
        f3 = {"kinds": [6, 7], "#e": [root_id], "limit": 100}
        # NIP-01: f"thread_{root_id}" is 71 chars — relays REQ it but reject
        # its CLOSE ("bad close: invalid subscription id length"), leaking the
        # sub. The clamp keeps the thread_ prefix and hashes the id body.
        self.subscribe(nip01_sub_id(f"thread_{root_id}"), [f1, f2, f3])

    def close(self):
        # gnostr-startup-2: a batch below the size threshold may still be in
        # the buffer — flush so "persisted" means persisted at shutdown.
        try:
            self._flush_event_buffer()
        except Exception:
            pass
        for r in self.active_relays.values():
            r.close()

    # --- trust-shield-v1 (NIP-85 WoT spam collapse; default OFF) -----------

    def _on_contacts_for_trust(self, _client, pk):
        if pk == self.my_pubkey:
            self._trust_follows = None  # E2 cache is stale

    def on_trust_setting_changed(self, key=None):
        """Trust-shield GSettings change (MainWindow holds the watch ref).
        A provider/relay switch resets all session state and purges other
        providers' cached ranks (spec §5.2/§7)."""
        if key == "trust-shield" and trust_settings.trust_get("trust-shield"):
            self._trust_dead = False
        if key in ("trust-shield-provider", "trust-shield-relay"):
            self._trust_dead = False
            self._trust_zero_rounds = 0
            self._trust_fallback_used = False
            self._trust_no_rank.clear()
            self._trust_pending.clear()
            provider = trust_settings.trust_get("trust-shield-provider")
            if provider:
                self.db.purge_other_trust_ranks(provider)

    def trust_shield_decide(self, author_pk):
        """Feed-pipeline hook (spec §3.1/§3.3): 'normal' | 'collapse'.
        Always 'normal' while the flag is off, unset, or the provider relay
        proved unreachable this session (§1.4). Side effect: a rendered
        author with no fresh rank row joins the pending fetch set (§2 —
        lazy, rendered authors only, never a bulk pull)."""
        if self._trust_dead or not self._trust_active():
            return trust_shield.NORMAL
        if author_pk == self.my_pubkey:
            return trust_shield.NORMAL  # E1
        follows = self._trust_follows_set()
        if author_pk in follows:
            return trust_shield.NORMAL  # E2
        if author_pk in self._trust_revealed:
            return trust_shield.NORMAL  # 'Show post' (session)
        provider = trust_settings.trust_get("trust-shield-provider")
        rank = None
        row = self.db.get_trust_rank(provider, author_pk)
        if row is not None:
            rank = row["rank"]  # stale rank keeps deciding (no flicker)
            if trust_shield.is_stale(
                row["rank_updated"],
                trust_settings.trust_get("trust-shield-max-age-days"),
                time.time(),
            ):
                self._trust_note_pending(author_pk)
        else:
            self._trust_note_pending(author_pk)  # E3: absent != 0
        return trust_shield.TrustShieldPolicy(
            trust_settings.trust_get("trust-shield-threshold"),
            follows,
            self.my_pubkey,
        ).decide(author_pk, rank)

    def trust_shield_reveal(self, author_pk):
        """'Show post': reveal this author for the session (not persisted —
        a fresh launch re-collapses, spec §3.1)."""
        self._trust_revealed.add(author_pk)

    def _trust_active(self):
        return bool(
            trust_settings.trust_get("trust-shield")
            and trust_settings.trust_get("trust-shield-provider")
            and trust_settings.trust_get("trust-shield-relay")
        )

    def _trust_follows_set(self):
        if self._trust_follows is None and self.my_pubkey:
            self._trust_follows = set(self.db.get_following_list(self.my_pubkey))
        return self._trust_follows or set()

    def _trust_note_pending(self, author_pk):
        if author_pk in self._trust_no_rank:
            return  # provider already answered 'no rank' this session
        self._trust_pending.add(author_pk)
        if len(self._trust_pending) >= 25:
            self._trust_flush()  # §4: batch size 25
        elif self._trust_flush_source is None:
            self._trust_flush_source = GLib.timeout_add_seconds(
                30, self._trust_flush_tick
            )

    def _trust_flush_tick(self):
        self._trust_flush_source = None
        self._trust_flush()
        return False

    def _trust_flush(self):
        """Fire one batch REQ (max 50 subjects) at the provider relay only
        (§4). Single flight: while a batch is open, pending just grows."""
        if self._trust_batch or self._trust_dead or not self._trust_active():
            return
        subjects = list(self._trust_pending)[:50]
        if not subjects:
            return
        self._trust_seq += 1
        self._trust_batch = {"subjects": set(subjects), "fallback": False}
        self._open_ephemeral(
            trust_settings.trust_get("trust-shield-relay"),
            f"trustshield_{self._trust_seq}",
            trust_shield.rank_req_filters(
                trust_settings.trust_get("trust-shield-provider"), subjects
            ),
            self._trust_batch_done,
        )

    def _trust_batch_done(self, events, ok):
        batch = self._trust_batch
        self._trust_batch = None
        if batch is None:
            return
        provider = trust_settings.trust_get("trust-shield-provider")
        if not ok:
            # §1.4: unreachable / no EOSE within 15 s -> shield inert for
            # this session. Silent by spec; reset on provider/relay change.
            self._trust_dead = True
            self._log_relay(
                "trust-shield: provider relay unreachable; shield off this session"
            )
            return
        saved = 0
        for ev in events:
            r = trust_shield.parse_rank_event(ev, provider)
            if r is None:
                continue  # malformed: drop the EVENT, never the subject (R7)
            self.db.upsert_trust_rank(
                provider, r["subject"], r["rank"], r["created_at"]
            )
            saved += 1
        subjects = batch["subjects"]
        if saved:
            self._trust_zero_rounds = 0
        elif not batch["fallback"] and not self._trust_fallback_used:
            # Q1 escalation: two consecutive #d-empty EOSE answers from an
            # answering relay -> retry ONCE with the limit:500 fallback REQ
            # (client-side d filtering). Partial data is acceptable.
            self._trust_zero_rounds += 1
            if self._trust_zero_rounds >= 2:
                self._trust_fallback_used = True
                self._trust_zero_rounds = 0
                self._trust_pending.update(subjects)
                self._trust_seq += 1
                self._trust_batch = {"subjects": set(subjects), "fallback": True}
                self._open_ephemeral(
                    trust_settings.trust_get("trust-shield-relay"),
                    f"trustshield_{self._trust_seq}",
                    {
                        "authors": [provider],
                        "kinds": [trust_shield.RANK_KIND],
                        "limit": 500,
                    },
                    self._trust_batch_done,
                )
                return
        for s in subjects:
            self._trust_pending.discard(s)
            if self.db.get_trust_rank(provider, s) is None:
                self._trust_no_rank.add(s)  # answered 'no rank': no refetch
        if saved:
            self.emit("trust-ranks-updated", provider)
        if self._trust_pending:
            GLib.idle_add(self._trust_flush)  # >50 subjects: next batch

    def _open_ephemeral(self, url, sub_id, filters, on_done, timeout=15):
        """One-shot query on a dedicated short-lived connection: connect,
        REQ (snapshot), collect, then on_done(events, ok) on EOSE (ok) or
        timeout / connect failure / policy refusal (not ok); socket closed
        either way. Never touches active_relays. Snapshot REQs must wait
        for CONNECTED (subscribe() drops them while down)."""
        state = {"events": [], "done": False, "relay": None}

        def finish(ok):
            if state["done"]:
                return
            state["done"] = True
            r = state["relay"]
            if r is not None:
                try:
                    r.close()
                except Exception:
                    pass
            GLib.idle_add(on_done, state["events"], bool(ok))

        proxy_on = proxy_get("proxy-enabled")
        if not url_allowed(url, proxy_on) or onion_inert(url, proxy_on, self.tor_state):
            finish(False)
            return

        def on_status(_url, status):
            if state["done"]:
                return
            if status == ConnectionState.CONNECTED:
                state["relay"].subscribe(sub_id, filters, snapshot=True)
            elif status == ConnectionState.DISCONNECTED:
                finish(False)  # died before EOSE

        relay = NostrRelay(
            url,
            lambda ev: state["events"].append(ev),
            on_status,
            on_eose=lambda sid, _url: finish(True) if sid == sub_id else None,
            on_connect_error=lambda _err: finish(False),
        )
        state["relay"] = relay
        relay.start()

        def on_timeout():
            finish(False)
            return False

        GLib.timeout_add_seconds(timeout, on_timeout)

    def trust_shield_discover(self, on_done):
        """§1.2 provider discovery (Settings dialog only — never automatic).
        REQ kind-10040 for self + follows across connected relays plus
        purplepag.es, aggregate the top-5 providers by distinct-subject
        listing count, enrich with kind-0 profiles, then on_done(candidates)
        on the main thread. Works while the flag is off (user is configuring).
        """
        if not self.my_pubkey:
            GLib.idle_add(on_done, [])
            return
        subjects = list(dict.fromkeys([self.my_pubkey] + list(self._trust_follows_set())))
        urls = list(self.active_relays.keys())
        if "wss://purplepag.es" not in urls:
            urls.append("wss://purplepag.es")
        chunks = [subjects[i : i + 50] for i in range(0, len(subjects), 50)]
        found = {}
        seq = self._trust_seq
        self._trust_seq += 1
        pending = {"n": len(chunks) * len(urls)}

        def after_queries():
            cands = trust_shield.rank_candidates(found)[:5]
            if not cands:
                GLib.idle_add(on_done, [])
                return
            remaining = {"n": len(cands)}

            def got_profile(events, _ok, cand):
                for ev in events:
                    if ev.get("kind") == 0 and ev.get("pubkey") == cand["provider"]:
                        try:
                            prof = json.loads(ev.get("content") or "{}")
                        except Exception:
                            prof = {}
                        cand["name"] = (
                            prof.get("display_name")
                            or prof.get("name")
                            or cand["provider"][:8]
                        )
                        cand["about"] = prof.get("about") or ""
                        cand["website"] = prof.get("website") or ""
                        break
                else:
                    cand["name"] = cand["provider"][:8]
                remaining["n"] -= 1
                if remaining["n"] == 0:
                    GLib.idle_add(on_done, cands)

            for cand in cands:
                cand["listed_by"] = len(cand["listed_by"])
                url = cand["relay"] or next(
                    (u for u in self.relay_urls if u.startswith("wss://")),
                    "wss://purplepag.es",
                )
                self._open_ephemeral(
                    url,
                    f"tsprof_{seq}_{cand['provider'][:8]}",
                    {"kinds": [0], "authors": [cand["provider"]], "limit": 1},
                    lambda events, ok, c=cand: got_profile(events, ok, c),
                    timeout=10,
                )

        def got_chunk(events, _ok, chunk_set):
            for ev in events:
                if ev.get("kind") != trust_shield.DISCOVERY_KIND:
                    continue
                author = ev.get("pubkey")
                if author not in chunk_set:
                    continue
                entries = trust_shield.parse_discovery_entries(ev)
                if entries:
                    found.setdefault(author, []).extend(entries)
            pending["n"] -= 1
            if pending["n"] <= 0:
                after_queries()

        for i, chunk in enumerate(chunks):
            for url in urls:
                self._open_ephemeral(
                    url,
                    f"tsdisc_{seq}_{i}_{abs(hash(url)) % 10**8}",
                    {"kinds": [trust_shield.DISCOVERY_KIND], "authors": chunk, "limit": 200},
                    lambda events, ok, ch=frozenset(chunk): got_chunk(events, ok, ch),
                    timeout=15,
                )
