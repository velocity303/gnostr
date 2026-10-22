"""Shared SOCKS5 urllib opener tests (spec gnostr-tor-onion-support-v1 §4 card 4).

Tier-1, headless: the opener is exercised end-to-end against a real local
HTTP server through a fake SOCKS proxy object (connect() -> plain socket),
so the handler plumbing is proven without python-socks or tor. The socks5h
remote-resolution contract is asserted at the Proxy.from_url boundary
(rdns=True), the LUD-16 onion scheme rule (V5) lives here, and the hard-fail
rule (proxy on + python-socks missing => RuntimeError, never a silent
direct fallback) is guarded too.
"""

import ast
import http.server
import os
import socket
import threading
import urllib.error
import urllib.request
from unittest.mock import Mock

import pytest

from gnostr import profile_nips, proxy_opener

_REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_RENDERER = os.path.join(_REPO, "src", "gnostr", "renderer.py")

ONION = "aoqqpp7tzyil4hlq3umoos6atft6jvrqtosq2xy53sdgiesvgg4jncad.onion"


class FakeProxy:
    """Stands in for python_socks.sync.Proxy: connect(dest, port, timeout)
    returns a socket. Here it dials directly so the plumbing is testable;
    `reply` (when set) is returned instead of a real socket."""

    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply

    def connect(self, dest_host, dest_port, timeout=None):
        self.calls.append((dest_host, dest_port))
        if self.reply is not None:
            return self.reply
        return socket.create_connection((dest_host, dest_port), timeout)


def _settings(**over):
    s = {
        "proxy-enabled": True,
        "proxy-host": "127.0.0.1",
        "proxy-port": 9050,
        "proxy-remote-dns": True,
    }
    s.update(over)
    return s


def _conn_class(handler, scheme):
    """Recover the proxied http.client connection class a handler instance
    would use, by intercepting do_open (the classes are closures inside
    _proxied_handlers and not exported)."""
    captured = {}
    orig = urllib.request.AbstractHTTPHandler.do_open

    def spy(self, http_class, req, **kw):
        captured["cls"] = http_class
        raise urllib.error.URLError("stop")

    urllib.request.AbstractHTTPHandler.do_open = spy
    try:
        handler.https_open(
            urllib.request.Request(f"{scheme}://x.example/", method="GET")
        )
    except urllib.error.URLError:
        pass
    finally:
        urllib.request.AbstractHTTPHandler.do_open = orig
    return captured["cls"]


@pytest.fixture
def cleared_cache():
    proxy_opener._cache.clear()
    yield
    proxy_opener._cache.clear()


# -- disabled path -------------------------------------------------------------


def test_disabled_returns_plain_opener():
    opener = proxy_opener.proxy_opener({"proxy-enabled": False})
    assert isinstance(opener, urllib.request.OpenerDirector)
    kinds = {type(h).__name__ for h in opener.handlers}
    assert "ProxiedHTTPHandler" not in kinds
    assert "ProxiedHTTPSHandler" not in kinds


def test_default_settings_disabled(monkeypatch):
    # No compiled schema (dev run) -> proxy_get fallbacks -> disabled.
    # conftest's gi mock makes Gio.Settings.new return a Mock, so force the
    # documented no-schema path (same as test_proxy_settings.py).
    monkeypatch.setattr(proxy_opener.proxy_settings, "_settings", lambda: None)
    opener = proxy_opener.proxy_opener()
    kinds = {type(h).__name__ for h in opener.handlers}
    assert "ProxiedHTTPHandler" not in kinds


# -- enabled path: end-to-end through a fake proxy ------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"pong:" + self.path.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - silence test server
        pass


@pytest.fixture
def http_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


def test_enabled_routes_http_through_proxy(http_server, monkeypatch, cleared_cache):
    port = http_server.server_address[1]
    fake = FakeProxy()
    monkeypatch.setattr(proxy_opener, "_make_proxy", lambda h, p, r: fake)

    opener = proxy_opener.proxy_opener(_settings())
    with opener.open(f"http://127.0.0.1:{port}/hello", timeout=5) as r:
        assert r.read() == b"pong:/hello"
    # The origin host:port was dialed THROUGH the proxy object, not directly.
    assert fake.calls == [("127.0.0.1", port)]


def test_enabled_opener_ignores_env_proxy(monkeypatch, cleared_cache):
    # Env http_proxy must never leak a tor-routed request around the
    # endpoint. ProxyHandler({}) makes build_opener skip the default
    # env-reading handler entirely (an empty ProxyHandler registers no
    # proxy_open_* methods) — functional guard: no ProxyHandler in the
    # http chain at all, and the request lands on the SOCKS handler.
    monkeypatch.setenv("http_proxy", "http://leak.example:8080")
    fake = FakeProxy()
    monkeypatch.setattr(proxy_opener, "_make_proxy", lambda h, p, r: fake)
    opener = proxy_opener.proxy_opener(_settings())
    http_chain = opener.handle_open["http"]
    assert urllib.request.ProxyHandler not in [type(h) for h in http_chain]
    assert type(http_chain[0]).__name__ == "ProxiedHTTPHandler"


def test_opener_cached_per_endpoint(monkeypatch, cleared_cache):
    made = []

    def make(h, p, r):
        made.append((h, p, r))
        return FakeProxy()

    monkeypatch.setattr(proxy_opener, "_make_proxy", make)
    a = proxy_opener.proxy_opener(_settings())
    b = proxy_opener.proxy_opener(_settings())
    c = proxy_opener.proxy_opener(_settings(**{"proxy-remote-dns": False}))
    assert a is b
    assert a is not c
    assert made == [("127.0.0.1", 9050, True), ("127.0.0.1", 9050, False)]


def test_https_connect_wraps_socket_after_socks_connect():
    """TLS to a host through the proxy: SOCKS connect FIRST to the origin
    host:port, then wrap_socket with server_hostname = origin host (full
    cert verification intact — no CONNECT tunnel, no verification bypass)."""
    mock_sock = Mock()
    fake = FakeProxy(reply=mock_sock)
    _, https_h = proxy_opener._proxied_handlers(fake)
    conn_cls = _conn_class(https_h, "https")

    conn = conn_cls(ONION, 443)
    conn._context = Mock()
    conn.timeout = 5
    conn.connect()
    assert fake.calls == [(ONION, 443)]
    conn._context.wrap_socket.assert_called_once_with(mock_sock, server_hostname=ONION)


# -- socks5h semantics ----------------------------------------------------------


def test_make_proxy_requests_remote_dns(monkeypatch):
    ps_sync = pytest.importorskip(
        "python_socks.sync", reason="CI venv may lack the flatpak-only dep"
    )

    captured = {}

    class FakeFromUrl:
        @classmethod
        def from_url(cls, url, **kw):
            captured["url"] = url
            captured.update(kw)
            return "PROXY"

    monkeypatch.setattr(ps_sync.Proxy, "from_url", FakeFromUrl.from_url)
    out = proxy_opener._make_proxy("127.0.0.1", 9050, True)
    assert out == "PROXY"
    # python-socks has no socks5h scheme; rdns=True on socks5:// IS socks5h:
    # the hostname goes to the proxy verbatim, never to a local resolver.
    assert captured["url"] == "socks5://127.0.0.1:9050"
    assert captured["rdns"] is True


def test_make_proxy_local_dns_when_rdns_off(monkeypatch):
    ps_sync = pytest.importorskip(
        "python_socks.sync", reason="CI venv may lack the flatpak-only dep"
    )

    captured = {}

    class FakeFromUrl:
        @classmethod
        def from_url(cls, url, **kw):
            captured.update(kw)
            return "PROXY"

    monkeypatch.setattr(ps_sync.Proxy, "from_url", FakeFromUrl.from_url)
    proxy_opener._make_proxy("127.0.0.1", 9050, False)
    assert captured["rdns"] is False


def test_make_proxy_hard_fails_without_python_socks(monkeypatch):
    # Spec §3: a silent direct fallback would leak the user IP — hard-fail.
    import builtins

    real_import = builtins.__import__

    def blocked(name, *a, **kw):
        if name.startswith("python_socks"):
            raise ImportError("no python_socks")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(RuntimeError):
        proxy_opener._make_proxy("127.0.0.1", 9050, True)


# -- LUD-16 onion rule (V5) ------------------------------------------------------


def test_lud16_onion_uses_http_scheme():
    # LUD-16 normative: onion domains are fetched over plain http://.
    assert profile_nips.resolve_lnurl_endpoint(f"alice@{ONION}") == (
        f"http://{ONION}/.well-known/lnurlp/alice"
    )


def test_lud16_onion_case_insensitive():
    assert profile_nips.resolve_lnurl_endpoint(f"alice@{ONION.upper()}").startswith(
        "http://"
    )


def test_lud16_clearnet_still_https():
    assert (
        profile_nips.resolve_lnurl_endpoint("bob@example.com")
        == "https://example.com/.well-known/lnurlp/bob"
    )


def test_lud16_lookalike_domain_stays_https():
    # domain suffix check only: x.onion.evil.example is NOT an onion host.
    assert profile_nips.resolve_lnurl_endpoint("bob@x.onion.evil.example").startswith(
        "https://"
    )


# -- renderer media wiring ---------------------------------------------------------


def test_renderer_worker_fetch_uses_shared_opener():
    tree = ast.parse(open(_RENDERER, encoding="utf-8").read())
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_worker_fetch"
    )
    body = ast.unparse(fn)
    assert "proxy_opener.proxy_opener()" in body
    # The direct urlopen call site must be gone — that was the leak (§3 T7).
    assert "urlopen" not in body
