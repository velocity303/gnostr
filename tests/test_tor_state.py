"""tor-v1-status (spec gnostr-tor-onion-support-v1 §A.2.5 assertions).

Covers: host_is_onion vectors, SOCKS5 greeting probe against mock servers
(including the sent-bytes capture), the skip gate (inert onion relays are
never instantiated and never touch status-changed), the DOWN->REACHABLE->DOWN
transition (connect-once / close-once / toast dedup), and the sidebar icon
visibility + css-class contract.

client.py/main.py classes subclass the conftest-mocked GObject/Adw, so the
real methods are ast-extracted and bound to lightweight fakes (house
pattern, see tests/AGENTS.md).
"""

import ast
import os
import socket
import threading
import types

import pytest

from gnostr.util.tor_state import (
    TorState,
    host_is_onion,
    onion_inert,
    probe_socks5,
    probe_socks5_detail,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_CLIENT_PATH = os.path.normpath(os.path.join(_HERE, "..", "src", "gnostr", "client.py"))
_MAIN_PATH = os.path.normpath(os.path.join(_HERE, "..", "src", "gnostr", "main.py"))
_SIDEBAR_PATH = os.path.normpath(
    os.path.join(_HERE, "..", "src", "gnostr", "ui", "sidebar.py")
)


def _extract(src_path, class_name, method_name, extra_ns=None):
    """Bind a real method from source without importing the GTK-bound class."""
    src = open(src_path, encoding="utf-8").read()
    tree = ast.parse(src)
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name
    )
    fn = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name
    )
    mod = ast.Module(body=[fn], type_ignores=[])
    ns = {
        "json": __import__("json"),
        "time": __import__("time"),
        "threading": threading,
        "gnostr": __import__("gnostr"),
        "GLib": types.SimpleNamespace(
            idle_add=lambda fn, *a: fn(*a),
            timeout_add_seconds=lambda s, fn: 1,
            source_remove=lambda sid: None,
            SOURCE_REMOVE=False,
        ),
    }
    if extra_ns:
        ns.update(extra_ns)
    exec(compile(mod, src_path, "exec"), ns)
    return ns[method_name]


# ---------------------------------------------------------------- host_is_onion


@pytest.mark.parametrize(
    "url,expected",
    [
        ("ws://aoqqtesttesttesttesttesttesttesttesttesttest.onion", True),
        ("ws://x.onion:8011", True),
        ("wss://relay.example", False),
        ("ws://x.onion.evil.example", False),
        ("wss://X.ONION", True),  # case-insensitive
        ("not a url", False),
        ("", False),
    ],
)
def test_host_is_onion(url, expected):
    assert host_is_onion(url) is expected


# ------------------------------------------------------------------- the probe


class _MockSocks:
    """One-shot TCP server that reads the greeting and replies with `reply`
    (None = close immediately). Records the bytes the probe sent."""

    def __init__(self, reply):
        self.reply = reply
        self.received = b""
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.sock.listen(1)
        self.sock.settimeout(5)
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            try:
                self.received = conn.recv(3)
            except OSError:
                pass
            if self.reply is not None:
                try:
                    conn.sendall(self.reply)
                except OSError:
                    pass

    def stop(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


def test_probe_ok_and_sent_bytes():
    srv = _MockSocks(b"\x05\x00")
    try:
        assert probe_socks5("127.0.0.1", srv.port, timeout=3) is True
        srv.thread.join(timeout=5)
        # capture assertion (A.2.5): greeting-only, never CONNECT
        assert srv.received == b"\x05\x01\x00"
    finally:
        srv.stop()


def test_probe_auth_required_is_down_with_reason():
    srv = _MockSocks(b"\x05\x02")
    try:
        ok, reason = probe_socks5_detail("127.0.0.1", srv.port, timeout=3)
        assert ok is False and reason == "auth"
    finally:
        srv.stop()


def test_probe_closed_immediately():
    srv = _MockSocks(None)
    try:
        assert probe_socks5("127.0.0.1", srv.port, timeout=3) is False
    finally:
        srv.stop()


def test_probe_refused_port():
    # Bind-then-close a port so a connect to it is refused.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert probe_socks5("127.0.0.1", port, timeout=2) is False


# ------------------------------------------------------------------ skip gate


ONION = "ws://aoqqtesttesttesttesttesttesttesttesttesttest.onion"
CLEAR1 = "wss://relay.example"
CLEAR2 = "wss://nos.lol"
CLEAR_SORTED = sorted([CLEAR1, CLEAR2])  # iteration order in the gate tests


class FakeClient:
    """Fake self for ast-extracted NostrClient methods (tor slice)."""

    def __init__(self, proxy_enabled=False, tor_state=TorState.DISABLED):
        self.relay_urls = {ONION, CLEAR1, CLEAR2}
        self.active_relays = {}
        self.emitted = []  # (signal, arg)
        self.relay_log = []
        self.tor_state = tor_state
        self._tor_probe_reason = "ok"
        self._tor_probe_inflight = False
        self._tor_retry_source = None
        self._tor_onion_failures = 0
        self._proxy_enabled = proxy_enabled
        self.probe_calls = 0

    def emit(self, sig, arg=None):
        self.emitted.append((sig, arg))
        return False

    # NostrRelay callback slots (the counting FakeRelay ignores them)
    _handle_event = staticmethod(lambda *a: None)
    _handle_status = staticmethod(lambda *a: None)
    _handle_ok = staticmethod(lambda *a: None)
    _handle_eose = staticmethod(lambda *a: None)

    def _log_relay(self, line):
        self.relay_log.append(line)

    def probe_tor(self):
        self.probe_calls += 1

    def _on_relay_connect_error(self, url, err):
        pass


@pytest.fixture
def gate_ns():
    """Namespace for the real add_relay_connection: NostrRelay is a counting
    stand-in (the WebSocketApp-constructor-count assertion from A.2.5)."""
    made = []

    class FakeRelay:
        def __init__(self, url, *a, **kw):
            made.append(url)
            self.url = url
            self.closed = False
            self._idle_closed = False

        def start(self):
            pass

        def close(self):
            self.closed = True

    ns = {
        "NostrRelay": FakeRelay,
        "proxy_get": lambda key, fallback=None: (
            FakeClient._proxy if key == "proxy-enabled" else "x"
        ),
        "onion_inert": onion_inert,
        "host_is_onion": host_is_onion,
    }
    return ns, made


def _bind(client, ns, name):
    fn = _extract(_CLIENT_PATH, "NostrClient", name, extra_ns=ns)
    return fn.__get__(client)


def test_skip_gate_proxy_disabled_only_clearnet_connect(gate_ns):
    ns, made = gate_ns
    FakeClient._proxy = False
    c = FakeClient(proxy_enabled=False)
    c.add_relay_connection = _bind(c, ns, "add_relay_connection")
    for url in sorted(c.relay_urls):
        c.add_relay_connection(url)
    # A.2.5: constructor call count == 2 (clearnet only); onion never
    # instantiated -> no connect attempts, no status-changed from it.
    assert made == CLEAR_SORTED
    assert ONION not in c.active_relays
    assert not any(s == "status-changed" for s, _ in c.emitted)


def test_skip_gate_enabled_but_down_still_inert(gate_ns):
    ns, made = gate_ns
    FakeClient._proxy = True
    c = FakeClient(proxy_enabled=True, tor_state=TorState.DOWN)
    c.add_relay_connection = _bind(c, ns, "add_relay_connection")
    for url in sorted(c.relay_urls):
        c.add_relay_connection(url)
    assert made == CLEAR_SORTED


def test_skip_gate_reachable_connects_onion(gate_ns):
    ns, made = gate_ns
    FakeClient._proxy = True
    c = FakeClient(proxy_enabled=True, tor_state=TorState.REACHABLE)
    c.add_relay_connection = _bind(c, ns, "add_relay_connection")
    for url in sorted(c.relay_urls):
        c.add_relay_connection(url)
    assert ONION in made


# ------------------------------------------------- transitions + toast dedup


def _bind_state_methods(c):
    ns = {
        "host_is_onion": host_is_onion,
        "TorState": TorState,
        "TOR_RETRY_INTERVAL": 30,
        "GLib": types.SimpleNamespace(
            idle_add=lambda fn, *a: fn(*a),
            timeout_add_seconds=lambda s, fn: 1,
            source_remove=lambda sid: None,
            SOURCE_REMOVE=False,
        ),
    }
    for name in (
        "_set_tor_state",
        "_connect_inert_onion_relays",
        "_close_onion_relays",
        "_start_tor_retry",
        "_cancel_tor_retry",
    ):
        setattr(c, name, _bind(c, ns, name))
    return c


def test_down_to_reachable_to_down_cycle(gate_ns):
    ns, made = gate_ns
    FakeClient._proxy = True
    c = _bind_state_methods(FakeClient(proxy_enabled=True, tor_state=TorState.DOWN))
    c.add_relay_connection = _bind(c, ns, "add_relay_connection")

    # DOWN -> REACHABLE: exactly the inert onion relay connects, once.
    c._set_tor_state(TorState.REACHABLE)
    assert made == [ONION]
    assert ONION in c.active_relays
    assert ("tor-state-changed", "reachable") in c.emitted
    assert any("onion relays enabled" in l for l in c.relay_log)

    # REACHABLE -> DOWN: relay closed, one skip log line, state signal.
    onion_relay = c.active_relays[ONION]
    c._set_tor_state(TorState.DOWN)
    assert ONION not in c.active_relays
    assert onion_relay.closed is True
    assert any("proxy down" in l and "1 onion relay(s) skipped" in l for l in c.relay_log)
    assert c.emitted.count(("tor-state-changed", "down")) == 1

    # No further connect attempts while DOWN (gate holds).
    c.add_relay_connection(ONION)
    assert made == [ONION]  # still exactly one instantiation


def test_reachable_no_duplicate_signal_on_reprobe():
    c = _bind_state_methods(FakeClient(tor_state=TorState.REACHABLE))
    c._set_tor_state(TorState.REACHABLE)
    assert ("tor-state-changed", "reachable") not in c.emitted  # no-op re-probe


def test_auth_reason_change_reemits_for_tooltip():
    c = _bind_state_methods(FakeClient(tor_state=TorState.DOWN))
    c._tor_probe_reason = "refused"
    c._set_tor_state(TorState.DOWN, reason="auth")
    assert ("tor-state-changed", "down") in c.emitted


# ------------------------------------------------------ toast dedup (main.py)


class FakeWindow:
    def __init__(self):
        self.toasts = []
        self._tor_toast_shown = False
        self.icon_calls = []

    def add_toast(self, title):
        self.toasts.append(title)

    def _refresh_tor_icon(self):
        self.icon_calls.append(1)


class _Toast:
    def __init__(self, title=None, timeout=None):
        self.title = title
        self.timeout = timeout


def _bind_toast(win):
    ns = {
        "Adw": types.SimpleNamespace(Toast=_Toast),
        "host_is_onion": host_is_onion,
    }
    fn = _extract(_MAIN_PATH, "MainWindow", "on_tor_state_changed", extra_ns=ns)
    win.client = FakeClient()
    return fn.__get__(win)


def test_skip_toast_once_per_episode():
    win = FakeWindow()
    win.on_tor_state_changed = _bind_toast(win)
    c = win.client  # relay_urls has 1 onion

    win.on_tor_state_changed(c, "down")
    win.on_tor_state_changed(c, "down")  # repeated probe: no second toast
    assert len(win.toasts) == 1
    assert "1 onion relay(s)" in win.toasts[0].title
    assert win.toasts[0].timeout == 5

    win.on_tor_state_changed(c, "reachable")
    assert len(win.toasts) == 2  # "Tor connected" toast
    assert "Tor connected" in win.toasts[1].title

    win.on_tor_state_changed(c, "down")  # re-armed by the REACHABLE transition
    assert len(win.toasts) == 3


def test_no_toast_without_onion_relays():
    win = FakeWindow()
    win.on_tor_state_changed = _bind_toast(win)
    c = win.client
    c.relay_urls = {CLEAR1, CLEAR2}
    win.on_tor_state_changed(c, "down")
    assert win.toasts == []


# ------------------------------------------------------- sidebar icon contract


class FakeIcon:
    def __init__(self):
        self.visible = False
        self.classes = set()
        self.tooltip = None

    def set_visible(self, v):
        self.visible = v

    def add_css_class(self, c):
        self.classes.add(c)

    def remove_css_class(self, c):
        self.classes.discard(c)

    def set_tooltip_text(self, t):
        self.tooltip = t


def _bind_icon(sidebar):
    fn = _extract(_SIDEBAR_PATH, "Sidebar", "update_tor_icon")
    return fn.__get__(sidebar)


def test_icon_hidden_without_proxy_or_onion():
    sb = types.SimpleNamespace(tor_icon=FakeIcon())
    sb.update_tor_icon = _bind_icon(sb)
    sb.update_tor_icon("disabled", False, 0)
    assert sb.tor_icon.visible is False


def test_icon_disabled_dim_label():
    sb = types.SimpleNamespace(tor_icon=FakeIcon())
    sb.update_tor_icon = _bind_icon(sb)
    sb.update_tor_icon("disabled", False, 1)
    assert sb.tor_icon.visible is True
    assert "dim-label" in sb.tor_icon.classes


def test_icon_down_warning_and_auth_tooltip():
    sb = types.SimpleNamespace(tor_icon=FakeIcon())
    sb.update_tor_icon = _bind_icon(sb)
    sb.update_tor_icon("down", True, 1, reason="auth")
    assert "warning" in sb.tor_icon.classes
    assert "authentication" in sb.tor_icon.tooltip
    sb.update_tor_icon("down", True, 1, reason="refused")
    assert "not reachable" in sb.tor_icon.tooltip


def test_icon_reachable_plain_with_hostport():
    sb = types.SimpleNamespace(tor_icon=FakeIcon())
    sb.update_tor_icon = _bind_icon(sb)
    sb.update_tor_icon("reachable", True, 1, host="127.0.0.1", port=9050)
    assert sb.tor_icon.classes == set()
    assert sb.tor_icon.tooltip == "Tor proxy reachable (127.0.0.1:9050)"


# ------------------------------------------------------------- gate predicate


def test_onion_inert_matrix():
    assert onion_inert(ONION, False, TorState.DISABLED) is True
    assert onion_inert(ONION, True, TorState.DOWN) is True
    assert onion_inert(ONION, True, TorState.REACHABLE) is False
    # clearnet is never inert
    assert onion_inert(CLEAR1, False, TorState.DISABLED) is False
    assert onion_inert(CLEAR1, True, TorState.DOWN) is False
