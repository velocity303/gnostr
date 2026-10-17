"""DM freshness poll tests (DM-UX: new DMs surface without a manual refresh).

Root cause this guards: RelayConnection.subscribe CLOSEs the previous sub_id
when a different one opens, so visiting the feed closes dm_sub and inbound
DMs only appear on a manual refresh. The fix is a periodic snapshot REQ
(client.start_dm_poll / _dm_poll_tick) — one REQ per relay per minute, far
under relay rate limits, auto-closed on EOSE, never replayed on reconnect.

Follows the ast-extraction pattern from test_dm_ui_wiring.py: client.py is
exec'd with a recording GLib stub so the real methods run against a fake
self without GTK or sockets.

Covers:
- ensure_dm arms the poll (interval + callback scheduled once)
- a tick REQs kind-4 addressed to us with a since window and a unique sub_id
- repeated ticks get distinct sub_ids (REQ dedup keys on sub_id)
- tick with no pubkey self-removes (returns False)
- stop_dm_poll removes the GLib source; set_keys(None) stops the poll
- main.py wires dm-conversations-updated -> update_dm_badge (source scan)
"""

import ast
import types

import pytest

_CLIENT_PATH = "src/gnostr/client.py"
_MAIN_PATH = "src/gnostr/main.py"


def _client_ns():
    """exec client.py, then swap in a recording GLib stub."""
    src = open(_CLIENT_PATH, encoding="utf-8").read()
    ns = {"__name__": "gnostr.client", "__package__": "gnostr"}
    exec(compile(ast.parse(src), _CLIENT_PATH, "exec"), ns)
    rec = {"timeouts": [], "removed": []}
    ns["GLib"] = types.SimpleNamespace(
        idle_add=lambda fn, *a: fn(*a),
        timeout_add=lambda ms, fn, *a: (rec["timeouts"].append((ms, fn)), 7)[1],
        source_remove=lambda sid: rec["removed"].append(sid),
        get_user_config_dir=lambda: "/tmp/gnostr-test",
    )
    return ns, rec


def _bind(ns, method_name, fake):
    src = open(_CLIENT_PATH, encoding="utf-8").read()
    cls = next(
        n
        for n in ast.parse(src).body
        if isinstance(n, ast.ClassDef) and n.name == "NostrClient"
    )
    fn = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name
    )
    exec(compile(ast.Module(body=[fn], type_ignores=[]), _CLIENT_PATH, "exec"), ns)
    return ns[method_name].__get__(fake)


class FakePollClient:
    def __init__(self, pubkey="a" * 64):
        self.my_pubkey = pubkey
        self._dm_poll_source = None
        self.requests = []
        self.subscribed = []
        self.calls = []

    def request_once(self, sub_id, filters):
        self.requests.append((sub_id, filters))

    def subscribe_dm(self):
        self.calls.append("subscribe_dm")

    def publish_dm_relay_list(self):
        self.calls.append("publish_dm_relay_list")


def test_ensure_dm_arms_the_poll():
    ns, rec = _client_ns()
    fake = FakePollClient()
    for name in ("ensure_dm", "start_dm_poll", "stop_dm_poll", "_dm_poll_tick"):
        setattr(fake, name, _bind(ns, name, fake))
    fake._ensure_dm_service = lambda: None
    fake.ensure_dm()
    assert "subscribe_dm" in fake.calls
    assert len(rec["timeouts"]) == 1
    ms, fn = rec["timeouts"][0]
    assert ms == ns["DM_POLL_INTERVAL"] * 1000
    assert fn == fake._dm_poll_tick or callable(fn)
    assert fake._dm_poll_source == 7


def test_ensure_dm_without_key_does_not_arm_poll():
    ns, rec = _client_ns()
    fake = FakePollClient(pubkey=None)
    for name in ("ensure_dm", "start_dm_poll", "stop_dm_poll"):
        setattr(fake, name, _bind(ns, name, fake))
    fake._ensure_dm_service = lambda: None
    fake.ensure_dm()
    assert rec["timeouts"] == []


def test_tick_reqs_kind4_with_since_window_and_unique_ids():
    ns, rec = _client_ns()
    fake = FakePollClient()
    fake._dm_poll_tick = _bind(ns, "_dm_poll_tick", fake)
    import time as _time

    before = int(_time.time())
    assert fake._dm_poll_tick() is True
    assert len(fake.requests) == 1
    sub_id, filt = fake.requests[0]
    assert sub_id.startswith("dm_poll_")
    assert filt["kinds"] == [4]
    assert filt["#p"] == [fake.my_pubkey]
    assert filt["limit"] == 50
    window = ns["DM_POLL_INTERVAL"] + ns["DM_POLL_SINCE_SLACK"]
    assert before - window - 2 <= filt["since"] <= before - window + 2
    # second tick: new sub_id (REQ dedup keys on sub_id; a repeat is dropped)
    fake._dm_poll_tick()
    assert fake.requests[1][0] != sub_id


def test_tick_without_pubkey_self_removes():
    ns, rec = _client_ns()
    fake = FakePollClient(pubkey=None)
    fake._dm_poll_tick = _bind(ns, "_dm_poll_tick", fake)
    fake._dm_poll_source = 7
    assert fake._dm_poll_tick() is False
    assert fake._dm_poll_source is None
    assert fake.requests == []


def test_stop_dm_poll_removes_source():
    ns, rec = _client_ns()
    fake = FakePollClient()
    fake.stop_dm_poll = _bind(ns, "stop_dm_poll", fake)
    fake._dm_poll_source = 7
    fake.stop_dm_poll()
    assert rec["removed"] == [7]
    assert fake._dm_poll_source is None
    # idempotent: second stop is a no-op
    fake.stop_dm_poll()
    assert rec["removed"] == [7]


def test_set_keys_none_stops_poll():
    ns, rec = _client_ns()
    fake = FakePollClient()
    stopped = []
    fake.stop_dm_poll = lambda: stopped.append(True)
    fake._dm_poll_source = 7
    src = open(_CLIENT_PATH, encoding="utf-8").read()
    cls = next(
        n
        for n in ast.parse(src).body
        if isinstance(n, ast.ClassDef) and n.name == "NostrClient"
    )
    fn = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "set_keys"
    )
    exec(compile(ast.Module(body=[fn], type_ignores=[]), _CLIENT_PATH, "exec"), ns)
    ns["set_keys"].__get__(fake)(None, None)
    assert stopped == [True]
    assert fake.my_pubkey is None


def test_main_window_badge_wired_to_conversations_signal():
    src = open(_MAIN_PATH, encoding="utf-8").read()
    assert '"dm-conversations-updated"' in src
    assert "update_dm_badge" in src
