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
