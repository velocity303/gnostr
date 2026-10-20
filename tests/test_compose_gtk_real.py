"""Real-GTK compose-box harness (rich editor phase 1).

Exercises RichComposeBox against a REAL Gtk.TextBuffer under xvfb: selection
sensitivity, marker wrap/unwrap, caret placement, link insert. The mocked
suite cannot catch buffer-API drift (the GTK 4.22 lesson from the DM views).

Runs standalone (NOT under the pytest suite: conftest mocks gi).
Skips cleanly with exit code 77 (meson "skip") when real GTK4/Adw or a
display is unavailable.

Usage:  xvfb-run -a python3 tests/test_compose_gtk_real.py
"""

import os
import sys
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="gnostr-compose-test-")
os.environ["XDG_DATA_HOME"] = os.path.join(_TMPDIR, "data")

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk, Adw
except Exception as e:
    print("SKIP: real GTK4/Adw unavailable (%s)" % e)
    sys.exit(77)

if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    print("SKIP: no display (run under xvfb-run)")
    sys.exit(77)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

# Load ONLY markup.py + compose_box.py (+ icons.py) via importlib against
# stub packages: importing the real `gnostr` package runs __init__, which
# pulls in window.ui (a compiled GResource only present in an installed
# build). Same isolation pattern as test_clipboard_gtk_real.py.
import importlib.util
import types

_pkg = types.ModuleType("gnostr")
_pkg.__path__ = [os.path.join(ROOT, "src", "gnostr")]
sys.modules["gnostr"] = _pkg
_ui = types.ModuleType("gnostr.ui")
_ui.__path__ = [os.path.join(ROOT, "src", "gnostr", "ui")]
sys.modules["gnostr.ui"] = _ui


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "src", "gnostr", relpath)
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load("gnostr.markup", "markup.py")
_load("gnostr.uploader", "uploader.py")
_load("gnostr.ui.icons", "ui/icons.py")
compose_box = _load("gnostr.ui.compose_box", "ui/compose_box.py")
RichComposeBox = compose_box.RichComposeBox

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok: {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL: {name} {detail}")


app = Gtk.Application(application_id="test.compose.gtkreal")


def run(_app):
    win = Gtk.Window()
    box = RichComposeBox(placeholder="test")
    win.set_child(box)
    win.present()
    while Gtk.events_pending():
        Gtk.main_iteration()

    buf = box.text_view.get_buffer()

    # emphasis buttons insensitive with no selection (locked design decision)
    check(
        "buttons insensitive without selection",
        all(not b.get_sensitive() for b in box._feature_buttons.values()),
    )

    # type text, select "hello", press bold -> **hello**
    buf.set_text("hello world")
    buf.select_range(buf.get_iter_at_offset(0), buf.get_iter_at_offset(5))
    while Gtk.events_pending():
        Gtk.main_iteration()
    check(
        "buttons sensitive with selection",
        all(b.get_sensitive() for b in box._feature_buttons.values()),
    )
    box._on_feature_clicked(None, "bold")
    check(
        "bold wraps selection",
        box.get_text() == "**hello** world",
        f"got {box.get_text()!r}",
    )
    check(
        "selection covers wrapped text",
        box.get_text()[
            buf.get_selection_bounds()[0]
            .get_offset() : buf.get_selection_bounds()[1]
            .get_offset()
        ]
        == "**hello**",
    )

    # press bold again on the exact wrapped selection -> unwrap (toggle)
    box._on_feature_clicked(None, "bold")
    check(
        "bold toggles off", box.get_text() == "hello world", f"got {box.get_text()!r}"
    )

    # italic + code
    buf.set_text("ab cd")
    buf.select_range(buf.get_iter_at_offset(3), buf.get_iter_at_offset(5))
    box._on_feature_clicked(None, "italic")
    check(
        "italic wraps selection", box.get_text() == "ab _cd_", f"got {box.get_text()!r}"
    )
    buf.set_text("xy")
    buf.select_range(buf.get_iter_at_offset(0), buf.get_iter_at_offset(2))
    box._on_feature_clicked(None, "code")
    check("code wraps selection", box.get_text() == "`xy`", f"got {box.get_text()!r}")

    # link insert at caret
    buf.set_text("link: ")
    buf.place_cursor(buf.get_iter_at_offset(6))
    box._link_entry.set_text("https://example.com")
    box._on_link_insert()
    check(
        "link inserted at caret",
        box.get_text() == "link: https://example.com",
        f"got {box.get_text()!r}",
    )

    # get/set round-trip
    box.set_text("round trip")
    check("set/get round-trip", box.get_text() == "round trip")

    # media upload completion path (no network: call the idle handler
    # directly, exactly as GLib.idle_add would from the worker thread)
    box.set_text("")
    box._on_upload_done(
        {"url": "https://cdn/x.png", "mime": "image/png", "dim": "8x8", "sha256": "aa"},
        None,
    )
    check(
        "upload inserts URL",
        box.get_text() == "https://cdn/x.png",
        f"got {box.get_text()!r}",
    )
    check(
        "upload records imeta tag",
        box.media_tags
        == [["imeta", "url=https://cdn/x.png", "m=image/png", "dim=8x8", "x=aa"]],
        f"got {box.media_tags!r}",
    )
    box._on_upload_done(
        {"url": "https://cdn/y.mp4", "mime": "video/mp4", "dim": None, "sha256": ""},
        None,
    )
    check(
        "second upload appends on new line",
        box.get_text() == "https://cdn/x.png\nhttps://cdn/y.mp4",
        f"got {box.get_text()!r}",
    )
    check(
        "second imeta minimal",
        box.media_tags[-1] == ["imeta", "url=https://cdn/y.mp4", "m=video/mp4"],
    )
    box.clear_media()
    check("clear_media empties tags", box.media_tags == [])
    # upload error path must not raise and must not touch text
    box._on_upload_done(None, "Upload server error (500)")
    check(
        "error path keeps text",
        box.get_text() == "https://cdn/x.png\nhttps://cdn/y.mp4",
    )

    win.destroy()
    app.quit()


app.connect("activate", run)
app.run(None)
if FAILS:
    print(f"FAILED: {len(FAILS)} checks: {FAILS}")
    sys.exit(1)
print("PASS: all compose-box real-GTK checks")
