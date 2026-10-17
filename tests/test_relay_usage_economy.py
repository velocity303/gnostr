"""Relay usage economy (issue #26 'Relay Usage Optimizations').

The issue's core finding: popular relays cap REQ messages per IP (nos.lol:
200 REQ / 10 s, then an automated ban), and every heavy client in the survey
shares the same failure — CLOSED is unhandled, reconnects replay the full
pending REQ set with backoff reset to zero, idle relays hold sockets.

Contracts covered here (all against the REAL NostrRelay/NostrClient methods,
extracted via ast and bound to a fake self — house pattern from
test_feed_freshness.py / test_follow_sync.py):

- R1 token bucket: REQ pacing at REQ_RATE_LIMIT/s with a REQ_BURST ceiling,
  so a burst of one-shot fetches cannot spike past a relay's per-IP cap.
- R2 queue dedup: the same sub_id queued twice is ONE REQ.
- R3 CLOSED handling: the refused sub is dropped from active/queued/snapshot
  sets (never replayed) and a rate-limit reason is surfaced once.
- R4 reconnect backoff: restart() is gated by randomized exponential backoff
  and resets on a successful CONNECTED.
- R5 live-sub replay: a reconnect re-issues the live subscription (the flap
  that used to silently stop DM/feed delivery), and snapshots are not replayed.
- R6 idle parking: close_if_idle() parks a silent relay with no live sub and
  leaves a streaming relay alone; wake() ends the park.

Under the conftest gi mocks NostrRelay evaluates against a Mock base, so the
methods are extracted from source and bound to a lightweight fake self.
"""

import ast
import json
import os
import time
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CLIENT_PATH = os.path.normpath(os.path.join(_HERE, "..", "src", "gnostr", "client.py"))

REQ_RATE_LIMIT = 18
REQ_BURST = 5
REQ_DEDUP_WINDOW = 2.0
RECONNECT_BACKOFF_START = 2.0
RECONNECT_BACKOFF_MAX = 60.0
IDLE_RELAY_TIMEOUT = 300.0

_METHODS = (
    "start",
    "restart",
    "wake",
    "close_if_idle",
    "subscribe",
    "request_once",
    "_enqueue",
    "process_queue",
    "_refill_tokens",
    "_next_send_wait",
)


def _extract(path, target, method_name, extra_ns=None):
    """Extract a real function from source (class method or module-level
    function) without importing the module under the mocked GObject."""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == target:
            fn = next(
                (
                    n
                    for n in node.body
                    if isinstance(n, ast.FunctionDef) and n.name == method_name
                ),
                None,
            )
            break
        if isinstance(node, ast.FunctionDef) and node.name == method_name:
            fn = node
            break
    if fn is None:
        raise LookupError(f"{target}.{method_name} not found in {path}")
    ns = {
        "json": json,
        "time": time,
        "random": __import__("random"),
        "gnostr": __import__("gnostr"),
        "GLib": types.SimpleNamespace(idle_add=lambda fn, *a: fn(*a)),
        "REQ_RATE_LIMIT": REQ_RATE_LIMIT,
        "REQ_BURST": REQ_BURST,
        "REQ_DEDUP_WINDOW": REQ_DEDUP_WINDOW,
        "RECONNECT_BACKOFF_START": RECONNECT_BACKOFF_START,
        "RECONNECT_BACKOFF_MAX": RECONNECT_BACKOFF_MAX,
        "IDLE_RELAY_TIMEOUT": IDLE_RELAY_TIMEOUT,
    }
    if extra_ns:
        ns.update(extra_ns)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), path, "exec"), ns)
    return ns[method_name]


class _FakeWS:
    """Minimal websocket-app stand-in: records sends, exposes the handlers
    start() installed so tests can drive open/message/close directly."""

    instances = []
    auto_open = True  # tests can suppress on_open to simulate a failed connect

    def __init__(
        self, url, on_open=None, on_message=None, on_error=None, on_close=None
    ):
        self.sent = []
        self.closed = False
        self._cb = {"open": on_open, "message": on_message, "close": on_close}
        _FakeWS.instances.append(self)

    def send(self, data):
        self.sent.append(json.loads(data))

    def close(self):
        self.closed = True
        if self._cb["close"]:
            self._cb["close"](self, None, None)

    def run_forever(self):
        if _FakeWS.auto_open:
            self._cb["open"](self)

    def fire(self, msg):
        self._cb["message"](self, json.dumps(msg))

    def reqs(self):
        return [m for m in self.sent if m[0] == "REQ"]

    def closes(self):
        return [m for m in self.sent if m[0] == "CLOSE"]


def _fake_relay(url="wss://relay.test", tokens=None, connected=True):
    """Real NostrRelay methods bound to a fake self with websocket/GLib
    stubbed. start() opens the fake WS synchronously (run_forever calls
    on_open inline)."""
    relay = types.SimpleNamespace(
        url=url,
        on_event=lambda ev: None,
        on_status=lambda *a: None,
        on_ok=lambda *a: None,
        on_eose=None,
        ws=None,
        is_connected=False,
        sub_id=None,
        active_sub_ids=set(),
        request_queue=[],
        queued_sub_ids=set(),
        _recent_reqs={},
        live_subs={},
        is_processing_queue=False,
        snapshot_ids=set(),
        _tokens=float(REQ_BURST if tokens is None else tokens),
        _last_refill=time.monotonic(),
        _backoff=RECONNECT_BACKOFF_START,
        _last_attempt=0.0,
        _last_traffic=time.monotonic(),
        _closed_notified=False,
        _idle_closed=False,
    )
    ws_mod = types.SimpleNamespace(WebSocketApp=_FakeWS)
    conn_state = __import__(
        "gnostr.util.connection_state", fromlist=["ConnectionState"]
    ).ConnectionState
    for name in _METHODS:
        fn = _extract(
            _CLIENT_PATH,
            "NostrRelay",
            name,
            {
                "websocket": ws_mod,
                "threading": __import__("threading"),
                "ConnectionState": conn_state,
            },
        )
        setattr(relay, name, fn.__get__(relay))
    if connected:
        relay.start()
    return relay


def _drain(relay):
    """process_queue spawns a worker thread; join it deterministically."""
    deadline = time.monotonic() + 5.0
    while relay.is_processing_queue and time.monotonic() < deadline:
        time.sleep(0.005)


# ---------------------------------------------------------------------------
# R1 — token bucket paces REQs (no burst past a relay's per-IP cap)
# ---------------------------------------------------------------------------


def test_token_bucket_consumes_and_paces():
    relay = _fake_relay()
    # Burst allowance is finite: REQ_BURST sends are free, the next one waits.
    waits = [relay._next_send_wait() for _ in range(REQ_BURST)]
    assert all(w == 0.0 for w in waits), "burst allowance should be free"
    wait = relay._next_send_wait()
    assert wait > 0.0, "bucket empty must force a wait, not a send"
    # The wait is the time to accrue one token at REQ_RATE_LIMIT/s (1e-3
    # tolerance absorbs monotonic() jitter across the burst loop).
    assert abs(wait - 1.0 / REQ_RATE_LIMIT) < 1e-3


def test_token_bucket_refill_is_capped_at_burst():
    relay = _fake_relay(tokens=0.0)
    relay._last_refill = time.monotonic() - 10_000  # long idle
    relay._refill_tokens()
    assert relay._tokens <= float(REQ_BURST), "idle must not bank unbounded tokens"


def test_queue_worker_paces_a_burst_of_requests():
    """10 one-shot REQs queued at once: the burst allowance goes out
    immediately, the rest are paced. The old flat 0.1 s-per-REQ code took
    1.0 s; an unpaced flood takes ~0.001 s. Pacing lands between."""
    relay = _fake_relay()
    t0 = time.monotonic()
    for i in range(10):
        relay.request_once(f"meta_{i}", {"kinds": [0], "limit": 1})
    _drain(relay)
    elapsed = time.monotonic() - t0
    assert len(relay.ws.reqs()) == 10
    assert elapsed >= 0.05, f"burst of 10 REQs took only {elapsed:.3f}s — flooded"
    assert (
        elapsed < 10 / REQ_RATE_LIMIT + 0.15
    ), f"burst took {elapsed:.3f}s — slower than the rate cap allows"


# ---------------------------------------------------------------------------
# R2 — queue dedup: the same sub_id queued twice is one REQ
# ---------------------------------------------------------------------------


def test_duplicate_queued_sub_id_is_one_req():
    relay = _fake_relay()
    relay.request_once("meta_deadbeef", {"kinds": [0], "limit": 1})
    relay.request_once("meta_deadbeef", {"kinds": [0], "limit": 1})
    relay.request_once("meta_deadbeef", {"kinds": [0], "limit": 1})
    _drain(relay)
    reqs = relay.ws.reqs()
    assert len(reqs) == 1, f"expected 1 REQ, got {len(reqs)}"
    assert reqs[0][1] == "meta_deadbeef"
    assert not relay.queued_sub_ids, "queue dedup set must clear on send"


def test_dedup_window_expires_so_ttl_refetch_still_works():
    relay = _fake_relay()
    relay.request_once("meta_deadbeef", {"kinds": [0], "limit": 1})
    _drain(relay)
    # Age the dedup stamp past the window: a deliberate later re-fetch goes out.
    relay._recent_reqs["meta_deadbeef"] = time.monotonic() - (REQ_DEDUP_WINDOW + 1)
    relay.request_once("meta_deadbeef", {"kinds": [0], "limit": 1})
    _drain(relay)
    assert len(relay.ws.reqs()) == 2


# ---------------------------------------------------------------------------
# R3 — CLOSED is handled: refused sub is dropped, never replayed
# ---------------------------------------------------------------------------


def test_closed_drops_subscription_and_queue_entry():
    relay = _fake_relay()
    relay.subscribe("sub_following", {"kinds": [1], "limit": 50})
    relay.request_once("meta_deadbeef", {"kinds": [0], "limit": 1})
    _drain(relay)
    assert "sub_following" in relay.active_sub_ids
    relay.ws.fire(["CLOSED", "sub_following", "rate-limited: too many REQs"])
    # Dropped everywhere so nothing replays it (the ban spiral in #26).
    assert "sub_following" not in relay.active_sub_ids
    assert "sub_following" not in relay.live_subs
    assert "sub_following" not in relay.snapshot_ids
    assert all(sid != "sub_following" for sid, _ in relay.request_queue)


def test_closed_rate_limit_notice_surfaced_once(capsys):
    relay = _fake_relay()
    relay.subscribe("sub_following", {"kinds": [1], "limit": 50})
    relay.ws.fire(["CLOSED", "sub_following", "rate-limited: slow down"])
    relay.ws.fire(["CLOSED", "sub_following", "rate-limited: slow down"])
    out = capsys.readouterr().out
    assert "rate-limited" in out
    assert out.count("rate-limited: slow down") == 1, "must surface once per connection"


def test_closed_non_rate_limit_is_quiet(capsys):
    relay = _fake_relay()
    relay.subscribe("sub_following", {"kinds": [1], "limit": 50})
    relay.ws.fire(["CLOSED", "sub_following", "client sent invalid subscription"])
    assert "rate-limited" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# R4 — reconnect uses randomized exponential backoff, resets on CONNECTED
# ---------------------------------------------------------------------------


def test_restart_is_gated_by_backoff():
    relay = _fake_relay()
    first_ws = relay.ws
    relay.ws.fire(["CLOSED", "x", "bye"])  # harmless
    # Simulate the socket dropping.
    relay.is_connected = False
    n_before = len(_FakeWS.instances)
    relay.restart()  # last attempt was ~now; backoff window is open
    assert len(_FakeWS.instances) == n_before, "restart must wait out the backoff"
    # Age the last attempt past the backoff window -> next restart reconnects.
    # auto_open off: the attempt is in flight (no CONNECTED yet), so the
    # grown backoff must be observable.
    relay._last_attempt = time.monotonic() - (RECONNECT_BACKOFF_MAX * 2)
    _FakeWS.auto_open = False
    try:
        relay.restart()
    finally:
        _FakeWS.auto_open = True
    assert len(_FakeWS.instances) == n_before + 1
    assert relay._backoff >= RECONNECT_BACKOFF_START * 2, "backoff must grow"
    assert relay._backoff <= RECONNECT_BACKOFF_MAX, "backoff must be capped"
    assert relay.ws is not first_ws


def test_backoff_resets_on_successful_connect():
    relay = _fake_relay()
    relay._backoff = 32.0
    relay.is_connected = False
    relay.start()  # fake run_forever fires on_open inline
    assert relay._backoff == RECONNECT_BACKOFF_START


def test_restart_never_wakes_a_parked_relay():
    relay = _fake_relay()
    relay._idle_closed = True
    relay.is_connected = False
    n_before = len(_FakeWS.instances)
    relay.restart()
    assert len(_FakeWS.instances) == n_before, "idle park must survive the sweep"


# ---------------------------------------------------------------------------
# R5 — live subscription survives a flap; snapshots do not replay
# ---------------------------------------------------------------------------


def test_reconnect_replays_live_subscription():
    relay = _fake_relay()
    relay.subscribe("dm_sub", {"kinds": [4], "#p": ["abc"], "limit": 50})
    assert relay.live_subs.get("dm_sub")
    # Flap: socket dies, then restart() reconnects (backoff aged out).
    relay.is_connected = False
    relay._last_attempt = time.monotonic() - (RECONNECT_BACKOFF_MAX * 2)
    relay.restart()
    _drain(relay)
    reqs = relay.ws.reqs()
    assert [r[1] for r in reqs] == ["dm_sub"], "live sub must be re-issued"
    assert reqs[0][2] == {"kinds": [4], "#p": ["abc"], "limit": 50}


def test_snapshot_sub_is_not_replayed_on_reconnect():
    relay = _fake_relay()
    relay.subscribe("sub_global", {"kinds": [1], "limit": 20}, snapshot=True)
    relay.is_connected = False
    relay._last_attempt = time.monotonic() - (RECONNECT_BACKOFF_MAX * 2)
    relay.restart()
    _drain(relay)
    assert relay.ws.reqs() == [], "snapshot subs already closed at EOSE"


def test_closed_live_sub_is_not_replayed():
    relay = _fake_relay()
    relay.subscribe("sub_following", {"kinds": [1], "limit": 50})
    relay.ws.fire(["CLOSED", "sub_following", "rate-limited"])
    relay.is_connected = False
    relay._last_attempt = time.monotonic() - (RECONNECT_BACKOFF_MAX * 2)
    relay.restart()
    _drain(relay)
    assert relay.ws.reqs() == [], "a refused sub must not be re-REQ'd on reconnect"


def test_subscribe_while_down_records_live_sub_for_replay():
    """Startup ordering: switch_feed runs before relays connect. The sub used
    to be a silent no-op; now it is recorded and replayed at CONNECTED."""
    relay = _fake_relay(connected=False)
    relay.subscribe("sub_following", {"kinds": [1], "limit": 50})
    assert relay.live_subs.get("sub_following")
    relay.start()  # connect now
    _drain(relay)
    assert [r[1] for r in relay.ws.reqs()] == ["sub_following"]


# ---------------------------------------------------------------------------
# R6 — idle parking + wake
# ---------------------------------------------------------------------------


def test_idle_relay_with_no_live_sub_is_parked():
    relay = _fake_relay()
    relay._last_traffic = time.monotonic() - (IDLE_RELAY_TIMEOUT + 1)
    assert relay.close_if_idle() is True
    assert relay._idle_closed is True
    assert relay.ws.closed is True


def test_streaming_relay_is_never_parked():
    relay = _fake_relay()
    relay.subscribe("sub_following", {"kinds": [1], "limit": 50})
    relay._last_traffic = time.monotonic() - (IDLE_RELAY_TIMEOUT + 1)
    assert relay.close_if_idle() is False, "live sub = the user is streaming"
    assert relay.ws.closed is False


def test_recent_traffic_keeps_relay_connected():
    relay = _fake_relay()
    assert relay.close_if_idle() is False


def test_inbound_traffic_stamps_last_traffic():
    relay = _fake_relay()
    relay._last_traffic = time.monotonic() - (IDLE_RELAY_TIMEOUT + 1)
    relay.ws.fire(["EOSE", "sub_following"])
    assert time.monotonic() - relay._last_traffic < 1.0


def test_wake_ends_the_park_and_sends_queued_reqs():
    relay = _fake_relay()
    relay._last_traffic = time.monotonic() - (IDLE_RELAY_TIMEOUT + 1)
    relay.close_if_idle()
    assert relay._idle_closed
    # Explicit activity on a parked relay wakes it; the queued REQ drains
    # through process_queue in on_open.
    relay.request_once("meta_deadbeef", {"kinds": [0], "limit": 1})
    assert relay._idle_closed is False
    _drain(relay)
    assert [r[1] for r in relay.ws.reqs()] == ["meta_deadbeef"]


def test_idle_park_does_not_emit_disconnected_status():
    """A deliberate park is not a failure — the status bar must not flip red."""
    statuses = []
    relay = _fake_relay()
    relay.on_status = lambda url, st: statuses.append(st)
    relay._last_traffic = time.monotonic() - (IDLE_RELAY_TIMEOUT + 1)
    relay.close_if_idle()
    assert statuses == [], "parking must be silent in the connection status"


# ---------------------------------------------------------------------------
# Client-level sweep: check_connections drives backoff + idle parking
# ---------------------------------------------------------------------------


def _extract_client_method(name, extra_ns=None):
    return _extract(_CLIENT_PATH, "NostrClient", name, extra_ns)


def test_check_connections_parks_idle_and_restarts_down_relays():
    parked = _fake_relay(url="wss://idle.test")
    parked._last_traffic = time.monotonic() - (IDLE_RELAY_TIMEOUT + 1)
    down = _fake_relay(url="wss://down.test")
    down.is_connected = False
    down._last_attempt = time.monotonic() - (RECONNECT_BACKOFF_MAX * 2)
    streaming = _fake_relay(url="wss://live.test")
    streaming.subscribe("sub_following", {"kinds": [1], "limit": 50})
    streaming._last_traffic = time.monotonic() - (IDLE_RELAY_TIMEOUT + 1)

    client = types.SimpleNamespace(
        active_relays={
            "wss://idle.test": parked,
            "wss://down.test": down,
            "wss://live.test": streaming,
        },
        _log_relay=lambda line: None,
    )
    _extract_client_method("check_connections").__get__(client)()

    assert parked._idle_closed is True, "idle relay should be parked"
    assert streaming._idle_closed is False, "streaming relay must stay up"
    assert down.is_connected is True, "down relay should reconnect via backoff"


def test_check_connections_respects_backoff_window():
    down = _fake_relay(url="wss://down.test")
    down.is_connected = False
    down._last_attempt = time.monotonic()  # just tried
    n_before = len(_FakeWS.instances)
    client = types.SimpleNamespace(
        active_relays={"wss://down.test": down}, _log_relay=lambda line: None
    )
    _extract_client_method("check_connections").__get__(client)()
    assert len(_FakeWS.instances) == n_before, "must not hammer a dropping relay"


# ---------------------------------------------------------------------------
# Constants sanity: the pacing must stay under the documented relay cap
# ---------------------------------------------------------------------------


def test_rate_limit_stays_under_relay_cap():
    """nos.lol bans at 200 REQ / 10 s per IP. A sustained gnostr burst must
    stay under it with headroom."""
    assert REQ_RATE_LIMIT * 10 < 200
    assert REQ_RATE_LIMIT + REQ_BURST < 200  # worst-case 10 s window
    assert IDLE_RELAY_TIMEOUT > 0
    assert RECONNECT_BACKOFF_MAX > RECONNECT_BACKOFF_START
