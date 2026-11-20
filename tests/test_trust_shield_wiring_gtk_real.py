"""Real-GTK Trust Shield wiring harness (NIPW-P2-FIX, FLX1 device log).

The FLX1 log showed, on every toggle of the Trust Shield switch:

  TypeError: RelayPreferencesWindow._build_trust_group.<locals>.<lambda>()
  takes 1 positional argument but 2 were given

GObject notify:: signals emit (object, GParamSpec). The 1-parameter lambda
died inside the signal machinery every time, so trust_set("trust-shield",
...) NEVER ran and the switch never persisted. PyGObject swallows handler
exceptions (prints them and continues), so the mocked tier-1 suite stayed
green — only a REAL notify:: emission catches this bug class (same class
as NIPW-P1 Bug A, tests/test_blossom_wiring_gtk_real.py).

Harness: ast-extract the real _build_trust_group from dialogs.py, run it
against real Adw widgets with a recording trust_settings stub, then flip
the real Adw.SwitchRow with set_active() — a real property change emits
notify::active — and assert trust_set recorded the new value. A swallowed
TypeError is additionally surfaced through sys.unraisablehook.

Runs standalone (NOT under the pytest suite: conftest mocks gi).
Skips cleanly with exit code 77 (meson "skip") when real GTK4/Adw or a
display is unavailable.

Usage:  xvfb-run -a python3 tests/test_trust_shield_wiring_gtk_real.py
"""

import ast
import os
import sys
import types
import importlib.util

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk, Adw, GLib
except Exception as e:
    print("SKIP: real GTK4/Adw unavailable (%s)" % e)
    sys.exit(77)

if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    print("SKIP: no display (run under xvfb-run)")
    sys.exit(77)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src", "gnostr")

# trust_settings calls recorded by the stub (key, value) in order.
trust_calls = []
# Exceptions PyGObject swallowed inside signal handlers.
unraisable = []


def _unraisable_hook(record):
    unraisable.append("%s: %s" % (record.exc_type.__name__, record.exc_value))


sys.unraisablehook = _unraisable_hook


def _make_ts_stub():
    """Recording stand-in for gnostr.trust_settings (the real module pulls
    nostr_utils/ecdsa which the platform python lacks; the harness only
    needs to observe what the wiring writes)."""
    ts = types.ModuleType("gnostr.trust_settings")
    defaults = {
        "trust-shield": False,
        "trust-shield-provider": "",
        "trust-shield-relay": "",
        "trust-shield-threshold": 25,
        "trust-shield-max-age-days": 7,
    }
    ts.trust_get = lambda key, fallback=None: defaults[key]
    ts.trust_set = lambda key, value: trust_calls.append((key, value)) or True
    ts.provider_hex_or_none = lambda v: None
    ts.relay_hint_or_none = lambda v: None
    return ts


def _extract_method(filename, cls, name):
    """Compile one method out of a source file (house ast-extraction
    pattern: dialogs.py imports client/ecdsa/libsecret the platform
    python lacks — only the wiring method itself is under test)."""
    with open(os.path.join(SRC, filename)) as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == name:
                    mod = ast.Module(body=[sub], type_ignores=[])
                    ast.fix_missing_locations(mod)
                    ns = {"Gtk": Gtk, "Adw": Adw, "__package__": "gnostr"}
                    exec(compile(mod, filename, "exec"), ns)
                    return ns[name]
    raise AssertionError("%s.%s not found in %s" % (cls, name, filename))


class _FakePage:
    def __init__(self):
        self.groups = []

    def add(self, g):
        self.groups.append(g)


class _FakeClient:
    def trust_shield_discover(self, _cb):
        raise AssertionError("discovery must not fire during construction")


def _build():
    """Run the real _build_trust_group against real Adw widgets."""
    gp = types.ModuleType("gnostr")
    gp.__path__ = [SRC]
    ts = _make_ts_stub()
    gp.trust_settings = ts
    sys.modules["gnostr"] = gp
    sys.modules["gnostr.trust_settings"] = ts

    build = _extract_method(
        "dialogs.py", "RelayPreferencesWindow", "_build_trust_group"
    )
    self_ = types.SimpleNamespace(client=_FakeClient())
    page = _FakePage()
    build(self_, page)
    return self_, page


def check_construction_writes_nothing():
    """The initial set_active (syncing from GSettings) happens before the
    notify:: connect — construction must not echo a write back."""
    trust_calls.clear()
    self_, page = _build()
    assert trust_calls == [], "construction wrote %r" % trust_calls
    assert isinstance(self_.trust_enabled_row, Adw.SwitchRow)
    assert page.groups, "group never added to the page"
    print("PASS: construction syncs state without writing back")


def check_toggle_emits_real_notify():
    """set_active() on a real Adw.SwitchRow emits notify::active with
    (object, GParamSpec) — the connected handler must survive that call
    signature AND persist the new value through trust_set."""
    trust_calls.clear()
    unraisable.clear()
    self_, _page = _build()

    self_.trust_enabled_row.set_active(True)
    assert (
        "trust-shield",
        True,
    ) in trust_calls, "toggling ON never reached trust_set (calls=%r, swallowed=%r)" % (
        trust_calls,
        unraisable,
    )

    self_.trust_enabled_row.set_active(False)
    assert (
        "trust-shield",
        False,
    ) in trust_calls, "toggling OFF never reached trust_set (calls=%r, swallowed=%r)" % (
        trust_calls,
        unraisable,
    )

    assert not unraisable, "signal handler raised (swallowed): %r" % unraisable
    print("PASS: real notify::active emission persists trust-shield both ways")


def main():
    Adw.init()
    checks = [
        check_construction_writes_nothing,
        check_toggle_emits_real_notify,
    ]
    failed = 0
    for check in checks:
        try:
            check()
        except Exception as e:
            failed += 1
            print("FAIL: %s: %s" % (check.__name__, e))
    if failed:
        return 1
    print("PASS: all %d Trust Shield wiring checks green" % len(checks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
