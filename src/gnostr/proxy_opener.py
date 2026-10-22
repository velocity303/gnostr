# src/gnostr/proxy_opener.py
"""Shared SOCKS5 urllib opener (spec gnostr-tor-onion-support-v1 §4 card 4).

One opt-in proxy config (the proxy-* GSettings keys in proxy_settings) routes
every gnostr urllib HTTP(S) path — LNURL/LUD-16 lookups, NIP-11 relay-info
docs, and renderer.ImageLoader media fetches — through the same SOCKS5
endpoint the relay WS uses. With proxy-remote-dns (default true) the hostname
is handed to the proxy verbatim (socks5h semantics), so `.onion` targets never
touch a local resolver.

Proxy disabled (the default) ⇒ a plain opener: direct connections, unchanged
behaviour. Proxy enabled but python-socks missing ⇒ RuntimeError: the flatpak
manifest ships the wheel (tor-v1-deps) and a silent direct fallback would
leak the user IP — hard-fail is the spec rule (§3), never rewrite the URL.
"""

import http.client
import threading
import urllib.request

from . import proxy_settings

_lock = threading.Lock()
_cache = {}  # (host, port, rdns) -> OpenerDirector


def _make_proxy(host, port, rdns):
    """python-socks sync proxy for one endpoint. rdns=True is socks5h: the
    proxy resolves the hostname (mandatory for `.onion`). python-socks has
    no `socks5h` URL scheme — rdns=True on a socks5:// URL is its socks5h."""
    try:
        from python_socks.sync import Proxy
    except ImportError as exc:
        raise RuntimeError(
            "SOCKS proxy enabled but python-socks is not installed"
        ) from exc
    return Proxy.from_url(f"socks5://{host}:{port}", rdns=rdns)


def _read(settings, key):
    """Config read from a dict-like override (tests) or GSettings (app)."""
    if settings is None:
        return proxy_settings.proxy_get(key)
    defaults = {
        "proxy-enabled": proxy_settings.DEFAULT_PROXY_ENABLED,
        "proxy-host": proxy_settings.DEFAULT_PROXY_HOST,
        "proxy-port": proxy_settings.DEFAULT_PROXY_PORT,
        "proxy-remote-dns": proxy_settings.DEFAULT_PROXY_REMOTE_DNS,
    }
    return settings.get(key, defaults[key])


def _proxied_handlers(proxy):
    """(HTTPHandler, HTTPSHandler) whose connections dial the origin host
    THROUGH `proxy` (a python-socks-like object with connect(dest_host,
    dest_port, timeout) -> socket). TLS wraps after the SOCKS connect, so
    wss-equivalent TLS to clearnet hosts keeps full verification."""

    class _ProxiedHTTPConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = proxy.connect(
                dest_host=self.host, dest_port=self.port, timeout=self.timeout
            )

    class _ProxiedHTTPSConnection(http.client.HTTPSConnection):
        def connect(self):
            self.sock = proxy.connect(
                dest_host=self.host, dest_port=self.port, timeout=self.timeout
            )
            # Direct origin connection through SOCKS (no CONNECT tunnel),
            # so server_hostname is the origin host — cert verification intact.
            # (_context is http.client.HTTPSConnection's SSLContext.)
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)

    class ProxiedHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(_ProxiedHTTPConnection, req)

    class ProxiedHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(_ProxiedHTTPSConnection, req)

    return ProxiedHTTPHandler(), ProxiedHTTPSHandler()


def proxy_opener(settings=None):
    """OpenerDirector honouring the SOCKS5 proxy config.

    `settings` is an optional dict-like of the four proxy-* keys (tests);
    the app passes nothing and reads GSettings via proxy_settings. Disabled
    (default) returns a plain opener; enabled returns a cached SOCKS5h opener.

    Env http_proxy/ALL_PROXY cannot leak around the endpoint: ProxyHandler({})
    is passed to build_opener, which skips the default env-reading handler
    (the empty instance registers no proxy_open_* methods, so no proxy
    indirection happens at all — requests go straight to the SOCKS handlers).
    """
    enabled = _read(settings, "proxy-enabled")
    if not enabled:
        return urllib.request.build_opener()

    host = str(_read(settings, "proxy-host"))
    port = int(_read(settings, "proxy-port"))
    rdns = bool(_read(settings, "proxy-remote-dns"))
    key = (host, port, rdns)
    with _lock:
        opener = _cache.get(key)
        if opener is None:
            proxy = _make_proxy(host, port, rdns)
            http_h, https_h = _proxied_handlers(proxy)
            opener = urllib.request.build_opener(
                http_h, https_h, urllib.request.ProxyHandler({})
            )
            _cache[key] = opener
        return opener
