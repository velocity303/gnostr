#!/usr/bin/env python3
"""Real-GTK check: DMThreadView composer is a RichComposeBox (compact).

Runs inside the gnostr flatpak on the Librem 5 (needs real Adw + Wayland).
DMThreadView is loaded via importlib with a stubbed host so no GResource /
full app is required.
"""

import importlib.util
import os
import sys

SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "gnostr"
)

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402


def _load(modname, rel):
    spec = importlib.util.spec_from_file_location(modname, os.path.join(SRC, rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, SRC)
# avoid running the app __init__ (needs GResource); register bare namespace
# packages so relative imports inside dm_thread_view resolve.
import types  # noqa: E402

gnostr_pkg = types.ModuleType("gnostr")
gnostr_pkg.__path__ = [SRC]
sys.modules["gnostr"] = gnostr_pkg
ui_pkg = types.ModuleType("gnostr.ui")
ui_pkg.__path__ = [os.path.join(SRC, "ui")]
sys.modules["gnostr.ui"] = ui_pkg

_load("gnostr.nostr_utils", "nostr_utils.py")
compose_box_mod = _load("gnostr.ui.compose_box", "ui/compose_box.py")
dm_thread_mod = _load("gnostr.ui.dm_thread_view", "ui/dm_thread_view.py")

FAILURES = []
CHECKS_RUN = [0]


def check(name, ok, detail=""):
    CHECKS_RUN[0] += 1
    print(
        ("PASS: " if ok else "FAIL: ") + name + (f" ({detail})" if detail else ""),
        flush=True,
    )
    if not ok:
        FAILURES.append(name)


class StubClient:
    def __init__(self):
        self.published = []

    def connect(self, *args):
        pass  # the view wires dm-conversations-updated / publish-result / dm-unreachable

    def publish_dm(self, peer, text, extra_tags=None):
        self.published.append((peer, text, extra_tags))
        return None  # falsy: no optimistic bubble path needed here

    def _sign(self, ev):
        return ev


class StubDB:
    def get_profile(self, pk):
        return {}

    def get_dm_events(self, *a, **k):
        return []


class StubMain:
    def __init__(self):
        self.client = StubClient()
        self.db = StubDB()
        self.pub_key = "a" * 64

    def update_dm_badge(self):
        pass  # opening the thread marks read + refreshes the sidebar badge


app = Adw.Application(
    application_id="test.dm.composer",
    # Session bus is absent inside the flatpak sandbox; without NON_UNIQUE,
    # registration fails and app.run() returns WITHOUT activating — the whole
    # harness silently skipped to PASS (same bug class as the compose harness).
    flags=Gio.ApplicationFlags.NON_UNIQUE,
)


def on_activate(a):
    win = Gtk.Window()
    win.set_default_size(360, 640)
    try:
        view = dm_thread_mod.DMThreadView(StubMain(), "c" * 64)
    except Exception as e:
        check("DMThreadView constructs", False, f"{type(e).__name__}: {e}")
        a.quit()
        return
    win.set_child(view)
    win.present()

    check(
        "composer is RichComposeBox",
        isinstance(view.compose, compose_box_mod.RichComposeBox),
    )
    check("Send button exists", view.btn_send is not None)
    check("Send disabled while blank", not view.btn_send.get_sensitive())

    buf = view.compose.text_view.get_buffer()
    buf.set_text("hi there")
    check("Send enabled after typing", view.btn_send.get_sensitive())

    # marker bar present (bold/italic/code/link/media buttons)
    check("media_tags attr", isinstance(view.compose.media_tags, list))

    # send path: goes through publish_dm with extra_tags kwarg
    view._on_send()
    stub = view.main_window.client
    check(
        "publish_dm got text + extra_tags",
        stub.published
        and stub.published[0][1] == "hi there"
        and stub.published[0][2] == [],
        f"published={stub.published}",
    )
    check("composer cleared after send", view.compose.get_text() == "")
    check("Send re-disabled after send", not view.btn_send.get_sensitive())

    win.destroy()
    a.quit()


app.connect("activate", on_activate)
rc = app.run([])
if CHECKS_RUN[0] == 0:
    print("FAILED: activate never ran — zero checks executed")
    sys.exit(1)
if FAILURES:
    print(f"FAILED: {len(FAILURES)} checks")
    sys.exit(1)
print("PASS: all dm-thread composer checks")
sys.exit(0)
