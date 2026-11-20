# src/gnostr/trust_settings.py
"""trust-shield-v1 GSettings access (spec trust-shield-v1 §7).

Same hard-fallback contract as proxy_settings: a dev run or missing
compiled schema never breaks the app — reads return the fallback, writes
return False. `trust-shield` defaults FALSE: flag-off is byte-for-byte
the pre-feature behavior (every consumer checks this first).
"""

from gnostr.nostr_utils import npub_to_hex
from gnostr.trust_shield import normalize_relay_hint

APP_ID = "tech.livingonlinux.gnostr"

DEFAULT_TRUST_SHIELD = False
DEFAULT_TRUST_PROVIDER = ""
DEFAULT_TRUST_RELAY = ""
DEFAULT_TRUST_THRESHOLD = 25
DEFAULT_TRUST_MAX_AGE_DAYS = 7

# key -> GVariant type code (must match data/tech.livingonlinux.gnostr.gschema.xml)
_TRUST_KEYS = {
    "trust-shield": "b",
    "trust-shield-provider": "s",
    "trust-shield-relay": "s",
    "trust-shield-threshold": "i",
    "trust-shield-max-age-days": "i",
}

_DEFAULTS = {
    "trust-shield": DEFAULT_TRUST_SHIELD,
    "trust-shield-provider": DEFAULT_TRUST_PROVIDER,
    "trust-shield-relay": DEFAULT_TRUST_RELAY,
    "trust-shield-threshold": DEFAULT_TRUST_THRESHOLD,
    "trust-shield-max-age-days": DEFAULT_TRUST_MAX_AGE_DAYS,
}


def _settings():
    """Gio.Settings for the app schema, or None when unavailable."""
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio

        return Gio.Settings.new(APP_ID)
    except Exception:
        return None


def trust_get(key, fallback=None):
    """GSettings read with a hard fallback. Unknown keys raise KeyError."""
    if fallback is None:
        fallback = _DEFAULTS[key]
    else:
        _TRUST_KEYS[key]  # KeyError guard for unknown keys
    s = _settings()
    if s is None:
        return fallback
    try:
        kind = _TRUST_KEYS[key]
        if kind == "b":
            return s.get_boolean(key)
        if kind == "i":
            return s.get_int(key)
        return s.get_string(key) or fallback
    except Exception:
        return fallback


def trust_set(key, value):
    """GSettings write for the Trust Shield preferences. False when the
    schema is unavailable so the UI can report 'could not persist'."""
    s = _settings()
    if s is None:
        return False
    try:
        kind = _TRUST_KEYS[key]
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
    """Call callback(key) on any trust-shield-* change. Returns the
    Gio.Settings ref the caller must keep alive (see proxy_settings)."""
    s = _settings()
    if s is None:
        return None
    try:
        s.connect(
            "changed",
            lambda _s, key: callback(key) if key in _TRUST_KEYS else None,
        )
    except Exception:
        return None
    return s


def provider_hex_or_none(raw):
    """Accept npub1... or 64-hex; return the hex pubkey or None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        from gnostr.trust_shield import _HEX64

        if _HEX64.match(raw):
            return raw
    except Exception:
        pass
    if raw.startswith("npub"):
        try:
            return npub_to_hex(raw)
        except Exception:
            return None
    return None


def relay_hint_or_none(raw):
    """Validate a user-entered provider relay URL (§7 save validation)."""
    return normalize_relay_hint(raw)
