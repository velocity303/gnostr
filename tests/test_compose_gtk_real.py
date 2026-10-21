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
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk
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
CHECKS_RUN = [0]


def check(name, cond, detail=""):
    CHECKS_RUN[0] += 1
    if cond:
        print(f"  ok: {name}", flush=True)
    else:
        FAILS.append(name)
        print(f"  FAIL: {name} {detail}", flush=True)


app = Gtk.Application(
    application_id="test.compose.gtkreal",
    # Inside the flatpak sandbox the session bus may be unavailable and
    # registration fails; without NON_UNIQUE, app.run() returns without ever
    # activating and the whole harness silently skips to PASS.
    flags=Gio.ApplicationFlags.NON_UNIQUE,
)


def _run_checks(app):
    ctx = GLib.MainContext.default()

    def pump():
        while ctx.pending():
            ctx.iteration(False)

    win = Gtk.Window()
    box = RichComposeBox(placeholder="test")
    win.set_child(box)
    win.present()
    pump()

    buf = box.text_view.get_buffer()

    # emphasis buttons insensitive with no selection (locked design decision)
    check(
        "buttons insensitive without selection",
        all(not b.get_sensitive() for b in box._feature_buttons.values()),
    )

    # type text, select "hello", press bold -> **hello**
    buf.set_text("hello world")
    buf.select_range(buf.get_iter_at_offset(0), buf.get_iter_at_offset(5))
    pump()
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

    # placeholder overlay (GtkTextView placeholder-text is 4.24+; the phone
    # is 4.22 — the overlay label must track buffer emptiness)
    check("placeholder hidden when text present", not box._placeholder_label.get_visible())
    box.set_text("")
    check("placeholder visible when empty", box._placeholder_label.get_visible())
    box.set_text("x")
    check("placeholder hides on typing", not box._placeholder_label.get_visible())
    box.set_text("   ")
    check(
        "placeholder hidden for whitespace-only",
        not box._placeholder_label.get_visible(),
    )
    box.set_text("")

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
    check(
        "clear_media clears chips",
        box._chips == [] and not box._chip_scroll.get_visible(),
    )
    # upload error path must not raise and must not touch text
    box._on_upload_done(None, "Upload server error (500)")
    check(
        "error path keeps text",
        box.get_text() == "https://cdn/x.png\nhttps://cdn/y.mp4",
    )

    # -- phase 3: preview chips --------------------------------------------
    box.set_text("")
    box._on_upload_done(
        {"url": "https://cdn/z.png", "mime": "image/png", "dim": None, "sha256": ""},
        None,
    )
    check("chip row visible after upload", box._chip_scroll.get_visible())
    check("one chip per upload", len(box._chips) == 1)
    check("URL line in text", box.get_text() == "https://cdn/z.png")
    box._on_chip_remove(None, "https://cdn/z.png")
    check(
        "chip remove strips URL line", box.get_text() == "", f"got {box.get_text()!r}"
    )
    check("chip remove drops imeta tag", box.media_tags == [])
    check("chip row hides when empty", not box._chip_scroll.get_visible())

    # -- phase 3: pasted-path detection -------------------------------------
    import tempfile
    import zlib

    tmpdir = tempfile.mkdtemp()
    real = os.path.join(tmpdir, "shot.png")

    # Real 1x1 PNG (the phone's glycin loader rejects magic-only fakes;
    # the thumbnail check actually decodes this file).
    def _chunk(ctype, data):
        c = ctype + data
        return (
            len(data).to_bytes(4, "big")
            + c
            + zlib.crc32(c).to_bytes(4, "big")
        )

    ihdr = (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"
    # 1 scanline: filter byte 0 + one RGBA pixel (color type 6 = 4 bytes)
    idat = zlib.compress(b"\x00\xff\x00\x00\xff")
    with open(real, "wb") as f:
        f.write(
            b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", idat)
            + _chunk(b"IEND", b"")
        )

    calls = []
    box._start_upload = calls.append
    check(
        "pasted path swallowed -> upload",
        box._on_insert_text(None, None, real, len(real)) is True and calls == [real],
        f"calls={calls}",
    )
    check(
        "plain text not swallowed",
        box._on_insert_text(None, None, "hello", 5) is False,
    )
    check(
        "missing file not swallowed",
        box._on_insert_text(None, None, "/tmp/nope.png", 12) is False,
    )
    check(
        "url text not swallowed",
        box._on_insert_text(None, None, "https://x/y.png", 15) is False,
    )
    check(
        "as_media_path rejects url",
        compose_box._as_media_path("https://x/y.png") is None,
    )
    check("as_media_path accepts file", compose_box._as_media_path(real) == real)
    # programmatic set_text of a media path must NOT re-trigger an upload
    box.set_text(real)
    check("set_text guarded against hook", calls == [real], f"calls={calls}")
    box.set_text("")

    # -- phase 4: multi-file fan-out ---------------------------------------
    calls.clear()
    box._start_uploads([real, real, "/tmp/other.jpg"])
    check(
        "multi-file: one upload per path",
        calls == [real, real, "/tmp/other.jpg"],
        f"calls={calls}",
    )

    # pending counter: button stays insensitive until the LAST upload lands
    # (_start_upload is what disables it; the fake above never ran, so mimic it)
    box._pending = 2
    box._media_btn.set_sensitive(False)
    box._on_upload_done(
        {"url": "https://cdn/m1.png", "mime": "image/png", "dim": None, "sha256": ""},
        None,
    )
    check(
        "button still busy after first of two",
        box._pending == 1 and not box._media_btn.get_sensitive(),
    )
    box._on_upload_done(
        {"url": "https://cdn/m2.png", "mime": "image/png", "dim": None, "sha256": ""},
        None,
    )
    check(
        "button re-enabled after last",
        box._pending == 0 and box._media_btn.get_sensitive(),
    )
    box.clear_media()
    box.set_text("")

    # -- phase 4: chip thumbnails -------------------------------------------
    box._add_media_chip("https://cdn/thumb.png", local_path=real)
    chip_box = box._chips[-1][0]
    thumb_img = chip_box.get_first_child()
    check("chip starts with placeholder icon", thumb_img is not None)
    # the async decode lands via GLib.idle_add — pump the main context
    import time as _time

    deadline = _time.time() + 5
    while _time.time() < deadline:
        pump()
        if thumb_img.get_storage_type() == Gtk.ImageType.PAINTABLE:
            break
    check(
        "image chip gets pixbuf thumbnail",
        thumb_img.get_storage_type() == Gtk.ImageType.PAINTABLE,
        f"storage={thumb_img.get_storage_type()}",
    )
    box._on_chip_remove(None, "https://cdn/thumb.png")

    box._add_media_chip("https://cdn/clip.mp4")
    vimg = box._chips[-1][0].get_first_child()
    check(
        "video chip gets static icon",
        vimg.get_storage_type() == Gtk.ImageType.ICON_NAME,
    )
    box._on_chip_remove(None, "https://cdn/clip.mp4")

    # -- phase 4: raw image-bytes paste --------------------------------------
    class _FakeGValue:
        def __init__(self, obj):
            self._obj = obj

        def get_object(self):
            return self._obj

    class _FakeClip:
        def __init__(self, obj):
            self._obj = obj

        def read_value_finish(self, _res):
            return _FakeGValue(self._obj)

    pixbuf = GdkPixbuf.Pixbuf.new_from_file(real)
    paste_calls = []

    def _capture_upload(path, cleanup=False, thumb=None):
        paste_calls.append((path, cleanup, thumb))

    box._start_upload = _capture_upload
    box._on_paste_image(_FakeClip(pixbuf), None, None)
    check("raw paste fired one upload", len(paste_calls) == 1, f"{paste_calls}")
    if paste_calls:
        tmp_path, cleanup, thumb = paste_calls[0]
        check("raw paste temp file exists at upload time", os.path.isfile(tmp_path))
        check("raw paste temp marked for cleanup", cleanup is True)
        check(
            "raw paste chip thumb pre-decoded",
            thumb is not None and thumb.get_width() == 40,
        )
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # FileList paste with MULTIPLE media files fans out (capped)
    list_calls = []
    box._start_upload = lambda p, **kw: list_calls.append(p)
    gfiles = [Gio.File.new_for_path(real) for _ in range(6)]
    fl = Gdk.FileList.new_from_list(gfiles)
    box._on_paste_files(_FakeClip(fl), None, None)
    check(
        "file-list paste capped at MAX_ATTACHMENTS",
        len(list_calls) == compose_box.MAX_ATTACHMENTS,
        f"got {len(list_calls)}",
    )

    # drag-drop of a FileList fans out too
    drop_calls = []
    box._start_upload = lambda p, **kw: drop_calls.append(p)
    check(
        "FileList drop accepted",
        box._on_drop(None, Gdk.FileList.new_from_list(gfiles), 0, 0) is True,
    )
    check("FileList drop fans out", len(drop_calls) == compose_box.MAX_ATTACHMENTS)

    box.clear_media()
    win.destroy()
    app.quit()


def run(app):
    # PyGObject swallows exceptions raised inside signal handlers (prints a
    # traceback, app.run still returns 0) — a mid-run crash used to exit 0 as
    # a false green. Any surprise must surface as a FAILED check instead.
    try:
        _run_checks(app)
    except Exception as exc:  # noqa: BLE001
        check("harness ran to completion", False, f"{type(exc).__name__}: {exc}")
        app.quit()


app.connect("activate", run)
app.run(None)
# A harness that never activated (bus/GTK surprise) must never report PASS.
if CHECKS_RUN[0] == 0:
    print("FAILED: activate never ran — zero checks executed")
    sys.exit(1)
if FAILS:
    print(f"FAILED: {len(FAILS)} checks: {FAILS}")
    sys.exit(1)
print(f"PASS: all {CHECKS_RUN[0]} compose-box real-GTK checks")
