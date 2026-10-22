"""Proxy GSettings tests (spec gnostr-tor-onion-support-v1 §4, card tor-v1-settings).

Tier-1: proxy_settings round-trip against a fake Gio.Settings, hard-fallback
when the schema is unavailable, and the gschema/dialog contract read off the
source (the conftest gi mocks make real Adw widgets unintrospectable — same
AST pattern as tests/test_relay_log_wrap.py).
"""

import ast
import os
import xml.etree.ElementTree as ET

from gnostr import proxy_settings as ps

_REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_GSCHEMA = os.path.join(_REPO, "data", "tech.livingonlinux.gnostr.gschema.xml")
_DIALOGS = os.path.join(_REPO, "src", "gnostr", "dialogs.py")


class FakeSettings:
    """Minimal Gio.Settings stand-in: dict-backed get/set per GVariant type."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.writes = []

    def get_boolean(self, key):
        return self.values[key]

    def get_int(self, key):
        return self.values[key]

    def get_string(self, key):
        return self.values[key]

    def set_boolean(self, key, v):
        self.values[key] = bool(v)
        self.writes.append((key, bool(v)))

    def set_int(self, key, v):
        self.values[key] = int(v)
        self.writes.append((key, int(v)))

    def set_string(self, key, v):
        self.values[key] = str(v)
        self.writes.append((key, str(v)))


# -- gschema contract ---------------------------------------------------------


def _schema_keys():
    tree = ET.parse(_GSCHEMA)
    schema = tree.find(".//schema")
    assert schema is not None
    assert schema.get("id") == "tech.livingonlinux.gnostr"
    return {k.get("name"): k for k in schema.findall("key")}


def test_gschema_has_proxy_keys_with_spec_defaults():
    keys = _schema_keys()
    expected = {
        "proxy-enabled": ("b", "false"),
        "proxy-host": ("s", "'127.0.0.1'"),
        "proxy-port": ("i", "9050"),
        "proxy-remote-dns": ("b", "true"),
    }
    for name, (vtype, default) in expected.items():
        assert name in keys, f"missing GSettings key {name}"
        assert keys[name].get("type") == vtype, f"{name} type drift"
        d = keys[name].find("default")
        assert d is not None and d.text.strip() == default, f"{name} default drift"


def test_proxy_defaults_match_gschema():
    """proxy_settings._DEFAULTS must agree with the compiled schema — the
    dialog pre-fills from proxy_get and a dev run must show schema defaults."""
    keys = _schema_keys()
    assert ps.DEFAULT_PROXY_ENABLED is False
    assert keys["proxy-enabled"].find("default").text.strip() == "false"
    assert ps.DEFAULT_PROXY_HOST == "127.0.0.1"
    assert ps.DEFAULT_PROXY_PORT == 9050
    assert ps.DEFAULT_PROXY_REMOTE_DNS is True
    assert ps._PROXY_KEYS == {
        "proxy-enabled": "b",
        "proxy-host": "s",
        "proxy-port": "i",
        "proxy-remote-dns": "b",
    }


# -- proxy_get / proxy_set round-trip -----------------------------------------


def test_proxy_get_reads_typed_values(monkeypatch):
    fake = FakeSettings(
        {
            "proxy-enabled": True,
            "proxy-host": "10.0.0.1",
            "proxy-port": 9150,
            "proxy-remote-dns": False,
        }
    )
    monkeypatch.setattr(ps, "_settings", lambda: fake)
    assert ps.proxy_get("proxy-enabled") is True
    assert ps.proxy_get("proxy-host") == "10.0.0.1"
    assert ps.proxy_get("proxy-port") == 9150
    assert ps.proxy_get("proxy-remote-dns") is False


def test_proxy_get_falls_back_without_schema(monkeypatch):
    # dev run / missing compiled schema: every key returns its default,
    # never raises (uploader.settings_get contract).
    monkeypatch.setattr(ps, "_settings", lambda: None)
    assert ps.proxy_get("proxy-enabled") is False
    assert ps.proxy_get("proxy-host") == "127.0.0.1"
    assert ps.proxy_get("proxy-port") == 9050
    assert ps.proxy_get("proxy-remote-dns") is True
    assert ps.proxy_get("proxy-port", 9999) == 9999


def test_proxy_get_falls_back_on_key_error(monkeypatch):
    class Broken:
        def get_int(self, key):
            raise Exception("no such key")

    monkeypatch.setattr(ps, "_settings", lambda: Broken())
    assert ps.proxy_get("proxy-port") == 9050


def test_proxy_get_unknown_key_raises():
    try:
        ps.proxy_get("nonsense-key")
        raise AssertionError("unknown key must raise KeyError")
    except KeyError:
        pass


def test_proxy_set_writes_typed_values(monkeypatch):
    fake = FakeSettings()
    monkeypatch.setattr(ps, "_settings", lambda: fake)
    assert ps.proxy_set("proxy-enabled", True) is True
    assert ps.proxy_set("proxy-host", "10.0.0.1") is True
    assert ps.proxy_set("proxy-port", "9150") is True  # coerced to int
    assert ps.proxy_set("proxy-remote-dns", False) is True
    assert fake.writes == [
        ("proxy-enabled", True),
        ("proxy-host", "10.0.0.1"),
        ("proxy-port", 9150),
        ("proxy-remote-dns", False),
    ]


def test_proxy_set_returns_false_without_schema(monkeypatch):
    # UI uses the False return to toast "could not persist" instead of
    # silently pretending the change landed.
    monkeypatch.setattr(ps, "_settings", lambda: None)
    assert ps.proxy_set("proxy-enabled", True) is False


# -- dialog wiring (AST: gi is mocked, widgets can't be constructed) ----------


def _dialog_method(name):
    tree = ast.parse(open(_DIALOGS, encoding="utf-8").read())
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "RelayPreferencesWindow"
    )
    return next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name
    )


def test_proxy_group_is_built_in_init():
    src = ast.dump(_dialog_method("__init__"))
    assert "_build_proxy_group" in src, "Proxy group must be built in __init__"


def test_proxy_group_binds_all_four_keys():
    body = ast.dump(_dialog_method("_build_proxy_group"))
    for key in ("proxy-enabled", "proxy-host", "proxy-port", "proxy-remote-dns"):
        assert key in body, f"_build_proxy_group must prefill from {key}"


def test_proxy_save_writes_all_four_keys():
    body = ast.dump(_dialog_method("on_save_proxy_settings"))
    for key in ("proxy-enabled", "proxy-host", "proxy-port", "proxy-remote-dns"):
        assert key in body, f"save handler must persist {key}"


def test_proxy_group_uses_no_adw_stringlist():
    # Librem 5 compat guard (house rule): Adw.StringList is 1.6+ and absent
    # from the phone runtime; the Proxy group must not introduce it.
    body = ast.dump(_dialog_method("_build_proxy_group"))
    assert "StringList" not in body
