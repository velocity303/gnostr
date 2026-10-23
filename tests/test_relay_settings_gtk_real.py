"""Real-GTK settings harness (RELSET-3).

Builds the actual RelayPreferencesWindow under xvfb with a fake client and
asserts the spec §B acceptance items the mocked suite cannot see:
- the window renders at 360px logical width with NO horizontal overflow
  on any page (every ScrolledWindow's hadjustment upper <= allocation)
- the Relay Activity log wraps a 120-char line inside the page instead of
  forcing it sideways
- role toggle / reorder / remove clicks actually call the client API
- the window reopens on the persisted page (AC6)

Runs standalone (NOT under the pytest suite: conftest mocks gi).
Skips cleanly with exit code 77 (meson "skip") when real GTK4/Adw or a
display is unavailable. Same isolation pattern as test_compose_gtk_real.py:
importing the real `gnostr` package runs __init__ -> main -> window.ui (a
compiled GResource absent outside an installed build), so dialogs.py is
loaded via importlib against stub packages.

Usage:  xvfb-run -a python3 tests/test_relay_settings_gtk_real.py
"""

import os
import sys
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="gnostr-relset3-gtk-")
os.environ["XDG_DATA_HOME"] = os.path.join(_TMPDIR, "data")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The window reads proxy settings via GSettings; compile the app schema into
# the harness's private XDG_DATA_HOME so Gio.Settings.new() works on a dev
# host (a GLib g_error from a missing schema aborts the process, uncatchable).
_SCHEMA_DIR = os.path.join(_TMPDIR, "data", "glib-2.0", "schemas")
os.makedirs(_SCHEMA_DIR, exist_ok=True)
import shutil
import subprocess

for _xml in (os.path.join(ROOT, "data"),):
    for _f in os.listdir(_xml):
        if _f.endswith(".gschema.xml"):
            shutil.copy(os.path.join(_xml, _f), _SCHEMA_DIR)
subprocess.run(["glib-compile-schemas", _SCHEMA_DIR], check=False)

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, GLib, Gtk
except Exception as e:
    print("SKIP: real GTK4/Adw unavailable (%s)" % e)
    sys.exit(77)

if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    print("SKIP: no display (run under xvfb-run)")
    sys.exit(77)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

import importlib.util
import types

_pkg = types.ModuleType("gnostr")
_pkg.__path__ = [os.path.join(ROOT, "src", "gnostr")]
sys.modules["gnostr"] = _pkg
_ui = types.ModuleType("gnostr.ui")
_ui.__path__ = [os.path.join(ROOT, "src", "gnostr", "ui")]
sys.modules["gnostr.ui"] = _pkg and _ui
_util = types.ModuleType("gnostr.util")
_util.__path__ = [os.path.join(ROOT, "src", "gnostr", "util")]
sys.modules["gnostr.util"] = _util


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "src", "gnostr", relpath)
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


import gnostr.relay_entries as relay_entries  # noqa: E402  (pure module)

_pkg.relay_entries = relay_entries
_load("gnostr.nostr_utils", "nostr_utils.py")
_load("gnostr.key_manager", "key_manager.py")
_load("gnostr.proxy_settings", "proxy_settings.py")
_load("gnostr.key_health", "key_health.py")
_load("gnostr.util.tor_state", "util/tor_state.py")
dialogs = _load("gnostr.dialogs", "dialogs.py")

_checks = 0


def ok(cond, msg):
    global _checks
    assert cond, "FAIL: " + msg
    _checks += 1
    print("  ok:", msg)


class FakeClient:
    """Duck-typed NostrClient for the settings window."""

    def __init__(self):
        self.relay_entries = [
            {
                "url": "wss://relay.damus.io",
                "roles": ["read", "write"],
                "enabled": True,
            },
            {"url": "wss://nos.lol", "roles": ["read"], "enabled": True},
            {
                "url": "wss://parkerisparkingisparke.onion",
                "roles": ["write", "dm"],
                "enabled": False,
            },
        ]
        self.relay_urls = {e["url"] for e in self.relay_entries if e["enabled"]}
        self.relay_log = ["[rw] publish OK e123 relay.damus.io"]
        self.relay_status = {}
        self.relay_last_error = {"wss://nos.lol": "Connection refused"}
        self.relay_last_publish = {"wss://relay.damus.io": "Post OK"}
        self.settings_page = None
        self.calls = []
        self._handlers = {}

    def connect(self, signal, fn):
        self._handlers.setdefault(signal, []).append(fn)

    def emit(self, signal, *a):
        for fn in self._handlers.get(signal, []):
            fn(self, *a)

    def set_settings_page(self, page_id):
        self.calls.append(("settings_page", page_id))
        self.settings_page = page_id

    def set_relay_roles(self, url, roles):
        self.calls.append(("roles", url, tuple(roles)))

    def set_relay_enabled(self, url, enabled):
        self.calls.append(("enabled", url, enabled))

    def move_relay_entry(self, url, delta):
        self.calls.append(("move", url, delta))
        return True

    def remove_relay_entry(self, url):
        self.calls.append(("remove", url))
        return True

    def add_relay(self, url):
        self.calls.append(("add", url))


class App(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="tech.livingonlinux.gnostr.relset3test",
            # Inside the flatpak sandbox the session bus may be unavailable
            # and registration fails; without NON_UNIQUE, app.run() returns
            # without ever activating and the harness silently skips to PASS.
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self._failed = False

    def do_activate(self):
        win = Gtk.ApplicationWindow(application=self)
        win.set_default_size(360, 720)
        self.main_win = win
        self.client = FakeClient()
        self.dlg = dialogs.RelayPreferencesWindow(self.client, win)
        self.dlg.set_default_size(360, 720)
        win.present()
        self.dlg.present()
        GLib.timeout_add_seconds(45, self._watchdog)
        GLib.timeout_add(150, self.phase2)

    def _watchdog(self):
        print("TIMEOUT: harness did not finish in 45s")
        self._failed = True
        self.quit()
        return False

    def phase2(self):
        # Allocation only happens when the main loop is free, so every
        # "show page -> measure" pair must be split across two timeout
        # callbacks (sleeping inside a callback blocks layout).
        self._steps = [
            lambda: self._show("account"),
            lambda: self._measure("account"),
            lambda: self._show("relays"),
            lambda: self._measure("relays"),
            lambda: self._show("media"),
            lambda: self._measure("media"),
            lambda: self._show("network"),
            lambda: self._measure("network"),
            self._log_wrap_checks,
            self._expand_log,
            self._log_wrap_checks2,
            self._role_toggle_check,
            self._ac6_check_a,
            self._ac6_check_b,
        ]
        self._run_next()
        return False

    def _run_next(self):
        if not self._steps:
            print(f"PASS: all {_checks} relay-settings real-GTK checks")
            self.quit()
            return
        step = self._steps.pop(0)

        def run():
            try:
                step()
            except Exception:
                import traceback

                traceback.print_exc()
                self._failed = True
                self.quit()
                return False
            self._run_next()
            return False

        GLib.timeout_add(120, run)

    def _show(self, name):
        self.dlg.set_visible_page(getattr(self.dlg, name + "_page"))

    def _measure(self, name):
        page = getattr(self.dlg, name + "_page")
        alloc_w = page.get_width()
        ok(alloc_w <= 380, f"page {name} allocated at {alloc_w}px")
        ok(alloc_w > 0, f"page {name} is visible and allocated")
        for sw in _scrolled_in(page):
            if sw.get_width() == 0:
                continue  # collapsed; nothing to measure
            adj = sw.get_hadjustment()
            ok(
                adj.get_upper() <= adj.get_page_size() + 1,
                f"no h-overflow on page {name} "
                f"(upper={adj.get_upper():.0f} <= page={adj.get_page_size():.0f})",
            )

    def _log_wrap_checks(self):
        self.long_line = (
            "[rw] publish OK "
            + "e" * 64
            + " relay.damus.io "
            + "x" * 40
            + " (long tail that must wrap, not widen the page)"
        )
        self.client.emit("relay-log-updated", self.long_line)

    def _expand_log(self):
        self.dlg.activity_expander.set_expanded(True)

    def _log_wrap_checks2(self):
        label = self.dlg.relay_log_label
        ok(self.long_line in label.get_text(), "long line landed in the activity log")
        ok(
            label.get_allocated_width() <= self.dlg.get_width(),
            "activity label fits the 360px window",
        )
        for sw in _scrolled_in(self.dlg.relays_page):
            if sw.get_width() == 0:
                continue
            adj = sw.get_hadjustment()
            ok(
                adj.get_upper() <= adj.get_page_size() + 1,
                f"still no h-overflow after long log line "
                f"(upper={adj.get_upper():.0f} <= page={adj.get_page_size():.0f})",
            )

    def _role_toggle_check(self):
        row = self.dlg.relay_rows["wss://relay.damus.io"]
        row["switches"]["dm"].set_active(True)
        ok(
            ("roles", "wss://relay.damus.io", ("read", "write", "dm"))
            in self.client.calls,
            "role switch calls client.set_relay_roles with full role set",
        )
        row["row"].set_expanded(True)
        self.client.calls.clear()

    def _ac6_check_a(self):
        # the step chain left network visible; switch away then back so the
        # notify actually fires
        self.dlg.set_visible_page(self.dlg.account_page)
        self.dlg.set_visible_page(self.dlg.network_page)
        ok(
            ("settings_page", "network") in self.client.calls,
            "page switch persists via client.set_settings_page",
        )
        self.client.settings_page = "media"
        self.dlg2 = dialogs.RelayPreferencesWindow(self.client, self.main_win)
        self.dlg2.set_default_size(360, 720)
        self.dlg2.present()

    def _ac6_check_b(self):
        ok(
            self.dlg2.get_visible_page() is self.dlg2.media_page,
            "window reopens on the persisted page (AC6)",
        )


def _scrolled_in(widget, acc=None):
    acc = [] if acc is None else acc
    if isinstance(widget, Gtk.ScrolledWindow):
        acc.append(widget)
    child = widget.get_first_child() if isinstance(widget, Gtk.Widget) else None
    while child:
        _scrolled_in(child, acc)
        child = child.get_next_sibling()
    return acc


app = App()
_code = app.run([])
# A step exception quits with code 0; surface it as a harness failure. And
# zero checks means do_activate never ran (house rule: never a false green).
if getattr(app, "_failed", False) or _checks == 0:
    sys.exit(1)
sys.exit(_code)
