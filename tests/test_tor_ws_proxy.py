"""Tor/.onion relay WebSocket transport (spec gnostr-tor-onion-support-v1
§4 card tor-v1-ws + §3 leak rules).

Contracts:
- W1 socks_proxy_kwargs: {} when proxy disabled; otherwise the websocket-client
  SOCKS kwargs with proxy_type 'socks5h' (remote DNS — a .onion hostname must
  never reach a local resolver). 'socks5' only when proxy-remote-dns was
  explicitly disabled. Reads GSettings through proxy_get (hard fallback).
- W2 (spec V3): the kwargs feed websocket-client's proxy_info and yield
  proxy_protocol == 'socks5h'.
- W3 host_is_onion is hostname-based, not string-suffix:
  'ws://x.onion.evil.example' must be False (clearnet lookalike).
- W4 scheme policy url_allowed: wss:// always legal; ws:// legal iff the host
  is .onion AND the proxy is enabled. .onion + proxy down = hard fail (never
  a rewrite, never a clearnet fallback).
- W5 NostrRelay.start passes the proxy kwargs to run_forever (connect-time
  read, so a Settings change lands on the next reconnect).
- W6 add_relay_connection enforces the policy: a disallowed endpoint never
  instantiates a NostrRelay, logs the refusal verbatim (URL unchanged), and
  surfaces a visible WARNING status.

Real client methods are ast-extracted and bound to fakes (house pattern from
test_relay_usage_economy.py) because the conftest gi mocks make the imported
NostrClient/NostrRelay classes unusable.
"""

import ast
import collections
import os
import threading
import types

import pytest

from gnostr import proxy_settings as ps
from gnostr.util.tor_state import TorState, host_is_onion, onion_inert

_HERE = os.path.dirname(os.path.abspath(__file__))
_CLIENT_PATH = os.path.normpath(os.path.join(_HERE, "..", "src", "gnostr", "client.py"))

TEST_ONION = "aoqqpp7tzyil4hlq3umoos6atft6jvrqtosq2xy53sdgiesvgg4jncad.onion"


# ---------------------------------------------------------------------------
# helpers (house ast-extraction pattern)
# ---------------------------------------------------------------------------


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
        "GLib": types.SimpleNamespace(idle_add=lambda fn, *a: fn(*a)),
        "proxy_settings": ps,
    }
    if extra_ns:
        ns.update(extra_ns)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), path, "exec"), ns)
    return ns[method_name]


class _RecordingWS:
    """websocket.WebSocketApp stand-in: records run_forever kwargs."""

    instances = []

    def __init__(
        self, url, on_open=None, on_message=None, on_error=None, on_close=None
    ):
        self.url = url
        self.rf_kwargs = None
        _RecordingWS.instances.append(self)

    def run_forever(self, **kwargs):
        self.rf_kwargs = kwargs


def _fake_relay(url="wss://relay.test"):
    relay = types.SimpleNamespace(
        url=url,
        on_event=lambda ev: None,
        on_status=lambda *a: None,
        on_ok=lambda *a: None,
        on_eose=None,
        ws=None,
        is_connected=False,
        live_subs={},
        _backoff=0.0,
        _last_attempt=0.0,
        _last_traffic=0.0,
        _closed_notified=False,
        _idle_closed=False,
    )
    conn_state = __import__(
        "gnostr.util.connection_state", fromlist=["ConnectionState"]
    ).ConnectionState
    start = _extract(
        _CLIENT_PATH,
        "NostrRelay",
        "start",
        {
            "websocket": types.SimpleNamespace(WebSocketApp=_RecordingWS),
            "threading": threading,
            "ConnectionState": conn_state,
            "time": __import__("time"),
            "json": __import__("json"),
            "traceback": __import__("traceback"),
            # merged start() reads proxy kwargs through proxy_settings.*
            "proxy_settings": ps,
        },
    )
    relay.start = start.__get__(relay)
    return relay


class _FakeClient:
    """Fake Nostr self for the extracted add_relay_connection."""

    def __init__(self, proxy_enabled=False):
        self.active_relays = {}
        self.relay_urls = set()
        self.relay_log = collections.deque(maxlen=200)
        self.statuses = []  # (url, status) from _handle_status
        self.created = []  # NostrRelay instantiation log
        self._proxy_enabled = proxy_enabled
        # merged add_relay_connection also runs the tor-v1 skip gate, which
        # reads self.tor_state; REACHABLE keeps this file focused on the
        # scheme policy (the skip gate has its own vectors in test_tor_state)
        self.tor_state = TorState.REACHABLE

    def _log_relay(self, line):
        self.relay_log.append(line)

    def _handle_status(self, url, status):
        self.statuses.append((url, status.status))

    def _handle_event(self, ev):
        pass

    def _handle_ok(self, *a):
        pass

    def _handle_eose(self, *a):
        pass

    def emit(self, *a):
        pass


def _bind_add_relay_connection(client):
    conn_state = __import__(
        "gnostr.util.connection_state", fromlist=["ConnectionState"]
    ).ConnectionState

    class FakeNostrRelay:
        def __init__(self, url, *a, **kw):
            client.created.append(url)

        def start(self):
            pass

    fn = _extract(
        _CLIENT_PATH,
        "NostrClient",
        "add_relay_connection",
        {
            "NostrRelay": FakeNostrRelay,
            "ConnectionState": conn_state,
            # merged add_relay_connection = scheme policy (this file) +
            # tor-v1 skip gate (test_tor_state); the exec namespace needs
            # every bare name the merged body references. proxy_get is
            # captured AFTER the test monkeypatches ps.proxy_get.
            "proxy_get": ps.proxy_get,
            "onion_inert": onion_inert,
            "host_is_onion": host_is_onion,
        },
    )
    client.add_relay_connection = fn.__get__(client)


# ---------------------------------------------------------------------------
# W1 — socks_proxy_kwargs
# ---------------------------------------------------------------------------


def test_kwargs_empty_when_proxy_disabled():
    assert ps.socks_proxy_kwargs({"proxy-enabled": False}) == {}
    # defaults (schema unavailable) are proxy-enabled False
    assert ps.socks_proxy_kwargs({}) == {}


def test_kwargs_socks5h_when_enabled():
    kw = ps.socks_proxy_kwargs(
        {"proxy-enabled": True, "proxy-host": "10.0.0.1", "proxy-port": 9150}
    )
    assert kw == {
        "http_proxy_host": "10.0.0.1",
        "http_proxy_port": 9150,
        "proxy_type": "socks5h",
    }


def test_kwargs_local_dns_only_when_remote_dns_disabled():
    kw = ps.socks_proxy_kwargs({"proxy-enabled": True, "proxy-remote-dns": False})
    assert kw["proxy_type"] == "socks5"


def test_kwargs_reads_gsettings(monkeypatch):
    class FakeSettings:
        values = {
            "proxy-enabled": True,
            "proxy-host": "127.0.0.1",
            "proxy-port": 9050,
            "proxy-remote-dns": True,
        }

        def get_boolean(self, k):
            return self.values[k]

        def get_int(self, k):
            return self.values[k]

        def get_string(self, k):
            return self.values[k]

    monkeypatch.setattr(ps, "_settings", lambda: FakeSettings())
    assert ps.socks_proxy_kwargs() == {
        "http_proxy_host": "127.0.0.1",
        "http_proxy_port": 9050,
        "proxy_type": "socks5h",
    }


# ---------------------------------------------------------------------------
# W2 — spec V3: websocket-client accepts the kwargs and reports socks5h
# ---------------------------------------------------------------------------


def test_v3_websocket_proxy_info_protocol():
    websocket = pytest.importorskip("websocket")
    from websocket._http import proxy_info

    kw = ps.socks_proxy_kwargs(
        {"proxy-enabled": True, "proxy-host": "127.0.0.1", "proxy-port": 9050}
    )
    info = proxy_info(**kw)
    assert info.proxy_protocol == "socks5h"
    assert (info.proxy_host, info.proxy_port) == ("127.0.0.1", 9050)


# ---------------------------------------------------------------------------
# W3 — host_is_onion: hostname-based, not string-suffix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        (f"ws://{TEST_ONION}", True),
        (f"ws://{TEST_ONION}:8011", True),
        (f"ws://{TEST_ONION.upper()}", True),  # case-insensitive
        ("wss://relay.example", False),
        ("ws://x.onion.evil.example", False),  # clearnet lookalike
        ("ws://onion", False),
        ("wss://example.com/onion", False),  # path, not host
        ("not a url", False),
    ],
)
def test_host_is_onion(url, expected):
    assert ps.host_is_onion(url) is expected


# ---------------------------------------------------------------------------
# W4 — scheme policy url_allowed
# ---------------------------------------------------------------------------


def test_wss_clearnet_always_allowed():
    assert ps.url_allowed("wss://nos.lol", False) is True
    assert ps.url_allowed("wss://nos.lol", True) is True


def test_ws_clearnet_never_allowed():
    # plaintext ws to a clearnet host is a leak, proxy on or off
    assert ps.url_allowed("ws://relay.example", False) is False
    assert ps.url_allowed("ws://relay.example", True) is False


def test_ws_onion_allowed_only_with_proxy():
    assert ps.url_allowed(f"ws://{TEST_ONION}", True) is True
    # .onion + proxy down: hard fail. The caller must surface an error —
    # never rewrite the URL, never fall back to a clearnet endpoint.
    assert ps.url_allowed(f"ws://{TEST_ONION}", False) is False


def test_ws_onion_lookalike_rejected_even_with_proxy():
    assert ps.url_allowed("ws://x.onion.evil.example", True) is False


def test_non_ws_schemes_rejected():
    assert ps.url_allowed(f"http://{TEST_ONION}", True) is False
    assert ps.url_allowed("ftp://relay.example", True) is False


# ---------------------------------------------------------------------------
# W5 — NostrRelay.start feeds the kwargs to run_forever
# ---------------------------------------------------------------------------


def test_start_passes_proxy_kwargs_to_run_forever(monkeypatch):
    expected = {
        "http_proxy_host": "127.0.0.1",
        "http_proxy_port": 9050,
        "proxy_type": "socks5h",
    }
    monkeypatch.setattr(ps, "socks_proxy_kwargs", lambda: dict(expected))
    _RecordingWS.instances.clear()
    relay = _fake_relay(f"ws://{TEST_ONION}")
    relay.start()
    ws = _RecordingWS.instances[-1]
    # run_forever spawns on a thread; join deterministically.
    deadline = __import__("time").monotonic() + 2.0
    while ws.rf_kwargs is None and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.005)
    assert ws.rf_kwargs == expected
    # The URL the socket was constructed with is the configured one verbatim
    # (no rewrite, no clearnet substitution).
    assert ws.url == f"ws://{TEST_ONION}"


def test_start_no_kwargs_when_proxy_disabled(monkeypatch):
    monkeypatch.setattr(ps, "socks_proxy_kwargs", lambda: {})
    _RecordingWS.instances.clear()
    relay = _fake_relay("wss://relay.test")
    relay.start()
    ws = _RecordingWS.instances[-1]
    deadline = __import__("time").monotonic() + 2.0
    while ws.rf_kwargs is None and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.005)
    assert ws.rf_kwargs == {}


# ---------------------------------------------------------------------------
# W6 — add_relay_connection enforces the policy (hard fail, visible)
# ---------------------------------------------------------------------------


def test_clearnet_ws_refused_no_relay_created(monkeypatch):
    monkeypatch.setattr(ps, "proxy_get", lambda key, fallback=None: False)
    client = _FakeClient()
    _bind_add_relay_connection(client)
    client.add_relay_connection("ws://relay.example")
    assert client.created == []  # never instantiated
    assert client.active_relays == {}
    # visible error: WARNING status + a log line quoting the URL verbatim
    assert [s for _, s in client.statuses] == ["WARNING"]
    assert any("ws://relay.example" in line for line in client.relay_log)


def test_onion_ws_with_proxy_down_hard_fails(monkeypatch):
    # §3 leak rule: .onion endpoint + proxy down = visible failure, NOT a
    # silent clearnet fallback and NOT a URL rewrite.
    monkeypatch.setattr(ps, "proxy_get", lambda key, fallback=None: False)
    client = _FakeClient()
    _bind_add_relay_connection(client)
    url = f"ws://{TEST_ONION}"
    client.add_relay_connection(url)
    assert client.created == []
    assert [s for _, s in client.statuses] == ["WARNING"]
    assert any(url in line for line in client.relay_log)


def test_onion_ws_with_proxy_up_connects(monkeypatch):
    monkeypatch.setattr(ps, "proxy_get", lambda key, fallback=None: True)
    client = _FakeClient()
    _bind_add_relay_connection(client)
    url = f"ws://{TEST_ONION}"
    client.add_relay_connection(url)
    assert client.created == [url]
    assert client.statuses == []


def test_wss_clearnet_connects_unchanged(monkeypatch):
    monkeypatch.setattr(ps, "proxy_get", lambda key, fallback=None: False)
    client = _FakeClient()
    _bind_add_relay_connection(client)
    client.add_relay_connection("wss://nos.lol")
    assert client.created == ["wss://nos.lol"]
    assert client.statuses == []
