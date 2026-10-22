# src/gnostr/proxy_settings.py
"""SOCKS5 proxy GSettings access (spec gnostr-tor-onion-support-v1 §4).

Opt-in Tor transport: `proxy-enabled` defaults false; when true, relay WS,
LNURL/NIP-11 lookups and media fetches route through the SOCKS5 endpoint
below with remote DNS resolution (socks5h semantics) so `.onion` hostnames
never touch a local resolver. Consumers (tor-v1-ws / tor-v1-http cards)
read via proxy_get(); the Settings dialog writes via proxy_set().

Same hard-fallback contract as uploader.settings_get: a dev run or a
missing compiled schema must never break the app — every read returns the
fallback, every write returns False so the UI can report "could not
persist" instead of crashing.
"""

from urllib.parse import urlsplit

APP_ID = "tech.livingonlinux.gnostr"

DEFAULT_PROXY_ENABLED = False
DEFAULT_PROXY_HOST = "127.0.0.1"
DEFAULT_PROXY_PORT = 9050
DEFAULT_PROXY_REMOTE_DNS = True

# key -> GVariant type code (must match data/tech.livingonlinux.gnostr.gschema.xml)
_PROXY_KEYS = {
    "proxy-enabled": "b",
    "proxy-host": "s",
    "proxy-port": "i",
    "proxy-remote-dns": "b",
}

_DEFAULTS = {
    "proxy-enabled": DEFAULT_PROXY_ENABLED,
    "proxy-host": DEFAULT_PROXY_HOST,
    "proxy-port": DEFAULT_PROXY_PORT,
    "proxy-remote-dns": DEFAULT_PROXY_REMOTE_DNS,
}


def _settings():
    """Gio.Settings for the app schema, or None when unavailable (dev run,
    unpackaged tree, missing compiled schema)."""
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio

        return Gio.Settings.new(APP_ID)
    except Exception:
        return None


def proxy_get(key, fallback=None):
    """GSettings read with a hard fallback. Unknown keys raise KeyError —
    that is a programmer error, not a runtime condition."""
    if fallback is None:
        fallback = _DEFAULTS[key]
    else:
        _PROXY_KEYS[key]  # KeyError guard for unknown keys
    s = _settings()
    if s is None:
        return fallback
    try:
        kind = _PROXY_KEYS[key]
        if kind == "b":
            return s.get_boolean(key)
        if kind == "i":
            return s.get_int(key)
        return s.get_string(key) or fallback
    except Exception:
        return fallback


def host_is_onion(url):
    """True iff the URL's *hostname* ends in '.onion' (case-insensitive).

    Hostname-based on purpose (spec gnostr-tor-onion-support-v1 §3): a raw
    string suffix check would accept 'ws://x.onion.evil.example' and dial
    it on clearnet — the exact leak the policy exists to prevent.
    """
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    return host.lower().endswith(".onion")


def url_allowed(url, proxy_enabled):
    """Scheme policy (spec §3 leak rules, card tor-v1-ws): wss:// is always
    legal; ws:// (unencrypted) is legal iff the host is .onion AND the
    SOCKS proxy is enabled — an onion endpoint with the proxy down is a
    hard fail, never a URL rewrite and never a clearnet fallback.
    """
    try:
        scheme = (urlsplit(url).scheme or "").lower()
    except ValueError:
        return False
    if scheme == "wss":
        return True
    if scheme == "ws":
        return host_is_onion(url) and bool(proxy_enabled)
    return False


def socks_proxy_kwargs(settings=None):
    """kwargs for WebSocketApp.run_forever (spec §8 contract).

    {} when the proxy is disabled (direct connection); otherwise the
    websocket-client SOCKS kwargs. proxy_type is 'socks5h' (remote DNS:
    the hostname goes to tor verbatim, Python's getaddrinfo never sees a
    .onion host) unless proxy-remote-dns was explicitly turned off.

    `settings` is an optional plain dict of key->value overrides (tests);
    None reads GSettings through proxy_get with its hard fallback.
    """
    if settings is None:
        values = {key: proxy_get(key) for key in _DEFAULTS}
    else:
        values = dict(_DEFAULTS)
        values.update(settings)
    if not values["proxy-enabled"]:
        return {}
    return {
        "http_proxy_host": str(values["proxy-host"]),
        "http_proxy_port": int(values["proxy-port"]),
        "proxy_type": "socks5h" if values["proxy-remote-dns"] else "socks5",
    }


def proxy_set(key, value):
    """GSettings write for the Proxy preferences. Returns True on success;
    False when the schema is unavailable (dev run) so the UI can tell the
    user the change could not be persisted."""
    s = _settings()
    if s is None:
        return False
    try:
        kind = _PROXY_KEYS[key]
        if kind == "b":
            s.set_boolean(key, bool(value))
        elif kind == "i":
            s.set_int(key, int(value))
        else:
            s.set_string(key, str(value))
        return True
    except Exception:
        return False


def watch_changes(callback):
    """Call callback(key) on any proxy-* change (tor-v1 §A.2.1 item 2).
    Returns the Gio.Settings object — the caller MUST keep the ref alive (a
    dropped ref unregisters the handler). None when the schema is
    unavailable; the caller treats that as 'no live change notifications'
    and re-reads via proxy_get() at the next natural trigger."""
    s = _settings()
    if s is None:
        return None
    try:
        s.connect("changed", lambda _s, key: callback(key) if key in _PROXY_KEYS else None)
    except Exception:
        return None
    return s
