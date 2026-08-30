"""Real-GTK feed harness (workstream #0, tier 2).

Exercises ui.feed_view.FeedView against a REAL Gtk.ScrolledWindow, a
REAL service.feed_model.FeedModel, and a REAL database.Database
(isolated via XDG_DATA_HOME). The risk surface here is the scroll
plumbing + model glue (adjustment math, anchor capture/restore, pill,
sentinel) — the widget-agnostic logic is already covered headless in
tests/test_feed_model.py.

Runs standalone (NOT under the pytest suite: conftest mocks gi).
Skips cleanly with exit code 77 (autotools/meson "skip") when real
GTK4/Adw or a display is unavailable, so it is safe to register in
meson unconditionally.

Usage:  xvfb-run -a python3 tests/test_feed_gtk_real.py
"""
import os
import sys
import tempfile
import importlib.util
import types

# Isolated user-data dir BEFORE any GLib use so we never touch the
# developer's real gnostr.db.
_TMPDIR = tempfile.mkdtemp(prefix="gnostr-gtk-test-")
os.environ["XDG_DATA_HOME"] = os.path.join(_TMPDIR, "data")

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk, Adw, GLib
except Exception as e:  # no PyGObject / GTK4 / Adw on this interpreter
    print("SKIP: real GTK4/Adw unavailable (%s)" % e)
    sys.exit(77)

if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    print("SKIP: no display (run under xvfb-run)")
    sys.exit(77)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src", "gnostr")
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))


def _load(name, path, pkg):
    """Load one gnostr module by file path WITHOUT executing
    gnostr/__init__.py (which drags in client -> ecdsa, a dep the
    system GTK interpreter may not have)."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = pkg
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_modules():
    gp = types.ModuleType("gnostr")
    gp.__path__ = [SRC]
    sys.modules["gnostr"] = gp
    up = types.ModuleType("gnostr.ui")
    up.__path__ = [os.path.join(SRC, "ui")]
    sys.modules["gnostr.ui"] = up
    sp = types.ModuleType("gnostr.service")
    sp.__path__ = [os.path.join(SRC, "service")]
    sys.modules["gnostr.service"] = sp
    _load(
        "gnostr.service.feed_model",
        os.path.join(SRC, "service", "feed_model.py"),
        "gnostr.service",
    )
    _load(
        "gnostr.database", os.path.join(SRC, "database.py"), "gnostr"
    )
    return _load(
        "gnostr.ui.feed_view", os.path.join(SRC, "ui", "feed_view.py"), "gnostr.ui"
    )


class StubMainWindow:
    """Minimal stand-in for MainWindow: real event_widgets dict + a
    lightweight fixed-height PostWidget (the real PostWidget drags in
    the renderer/GStreamer chain, which is out of scope here — it is
    unchanged by this workstream)."""

    pub_key = "owner"
    db = None

    def __init__(self):
        self.event_widgets = {}

    def make_post_widget(self, event_id):
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        w.append(Gtk.Label(label=event_id[-8:]))
        w.set_size_request(-1, 60)
        w.event_id = event_id
        self.event_widgets[event_id] = w


FAILURES = []
_CHECKS = {"n": 0}


def check(cond, msg):
    _CHECKS["n"] += 1
    if cond:
        print("  ok  -", msg)
    else:
        print("  FAIL-", msg)
        FAILURES.append(msg)


def main():
    fv = _load_modules()
    from gnostr.database import Database
    from gnostr.service.feed_model import FeedModel

    db = Database()
    OWN = "owner"
    AUTHORS = ["aa" + str(i).zfill(63) for i in range(3)]
    db.save_contacts(OWN, AUTHORS)
    # 60 posts, 3 authors, 10s apart (newest = p59).
    for i in range(60):
        db.save_event(
            {
                "id": "e%064x" % i,
                "pubkey": AUTHORS[i % 3],
                "created_at": 1_700_000_000 + i * 10,
                "kind": 1,
                "content": "post %d" % i,
                "tags": [],
                "sig": "sig",
            }
        )

    mw = StubMainWindow()
    mw.db = db
    view = fv.FeedView(mw)
    win = Gtk.Window()
    win.set_default_size(480, 240)
    win.set_child(view)
    win.present()

    def pump(ms=80):
        """Spin the default main context for ~ms so layout + GLib.idle
        callbacks (anchor restore) run. Manual iteration (no nested
        Gtk.main()) so repeated pumps can't re-enter the main loop."""
        import time as _time

        ctx = GLib.MainContext.default()
        deadline = _time.monotonic() + ms / 1000.0
        while _time.monotonic() < deadline:
            while ctx.pending():
                ctx.iteration(False)
            _time.sleep(0.002)

    def box_ids():
        ids = []
        child = view.posts_box.get_first_child()
        while child is not None:
            if child is not view._sentinel:
                ids.append(getattr(child, "event_id", None))
            child = child.get_next_sibling()
        return ids

    def scroll_bottom():
        va = view._vadj
        va.set_value(max(0.0, va.get_upper() - va.get_page_size()))
        pump(60)
        view.on_scroll_changed(va)
        pump(100)

    def wheel_bottom():
        """Simulate a real wheel-down scroll toward the bottom.

        A one-shot jump to the bottom is a no-op once already pinned there
        (value doesn't change -> the trigger doesn't re-fire). A wheel
        scroll moves the value incrementally, so each nudge re-fires the
        near-bottom handler and pages the feed toward end-of-DB. Returns
        True when the feed is exhausted."""
        va = view._vadj
        bottom = max(0.0, va.get_upper() - va.get_page_size())
        va.set_value(min(bottom, va.get_value() + 150.0))
        pump(60)
        view.on_scroll_changed(va)
        pump(100)
        return view.feed_model is not None and view.feed_model.exhausted

    def scroll_top():
        va = view._vadj
        va.set_value(0.0)
        pump(60)
        view.on_scroll_changed(va)
        pump(100)

    print("[1] load_first")
    m = FeedModel(owner_pubkey=OWN, database=db, page_size=12, max_window=20)
    view.feed_model = m
    m.load_first()
    view.sync_window()
    pump(120)
    check(
        m.event_ids == ["e%064x" % i for i in range(59, 47, -1)],
        "first page = 12 newest, newest-first",
    )
    check(box_ids() == m.event_ids, "box order matches model window")
    check(
        m.at_top is True and m.exhausted is False,
        "at_top=True, not exhausted (60 > page_size)",
    )
    check(
        view._sentinel.get_visible() is False,
        "sentinel hidden while not exhausted",
    )
    check(
        len(mw.event_widgets) == 12,
        "widgets hydrated for all 12 window ids (got %d)" % len(mw.event_widgets),
    )

    print("[2] scroll to bottom -> load_older (evicts top, keeps window bound)")
    scroll_bottom()
    check(
        len(m.event_ids) <= 20,
        "window bounded at max_window=20 (got %d)" % len(m.event_ids),
    )
    check(
        m.event_ids[-1] < "e%064x" % 36,
        "window advanced to an older page (ends at %s)" % m.event_ids[-1][-8:],
    )
    check(
        m.at_top is False and m.can_load_newer(),
        "not at top after down-scroll; re-pull possible",
    )
    check(box_ids() == m.event_ids, "box matches model after load_older")

    print("[3] scroll to top -> load_newer re-pulls evicted newest")
    scroll_top()
    check(m.at_top is True, "re-pull reached the absolute top (at_top=True)")
    check(m.event_ids[0] == "e%064x" % 59, "newest post p59 re-pulled to window top")
    check(
        "e%064x" % 55 in m.event_ids,
        "re-pulled page covers the previously evicted range",
    )
    check(len(m.event_ids) <= 20, "window still bounded after re-pull")
    check(box_ids() == m.event_ids, "box matches model after re-pull")

    print("[4] live event -> pill (buffered, not at top) then flush at top")
    scroll_bottom()
    check(m.at_top is False, "precondition: not at top")
    live_id = "e%064x" % 100
    db.save_event(
        {
            "id": live_id,
            "pubkey": AUTHORS[0],
            "created_at": 1_700_000_00100,
            "kind": 1,
            "content": "live",
            "tags": [],
            "sig": "sig",
        }
    )
    row = db.get_event_by_id(live_id)
    inserted = m.prepend_new([row])
    view.buffered_new(inserted)
    check(inserted == [live_id], "live event buffered")
    check(live_id not in m.event_ids, "buffered event NOT in window yet")
    check(
        view._pill.get_visible() is True and "1 new post" in view._pill.get_label(),
        "pill visible with count 1 (got %r)" % view._pill.get_label(),
    )
    # At the top, the MainWindow path flushes inline.
    scroll_top()
    if m.new_ids:
        view.flush_new()
        pump(100)
    check(live_id in m.event_ids, "live event flushed into window at top")
    check(
        live_id not in m.new_ids and view._pill.get_visible() is False,
        "buffer drained and pill hidden after flush",
    )

    print("[5] scroll to end-of-DB -> sentinel appears")
    for _ in range(40):
        if wheel_bottom():
            break
    check(m.exhausted is True, "reached end-of-DB (exhausted)")
    check(view._sentinel.get_visible() is True, "sentinel visible at end-of-DB")
    check(len(m.event_ids) > 0, "window non-empty at end-of-DB")

    print("[6] window invariant: ids unique")
    check(
        len(set(m.event_ids)) == len(m.event_ids), "no duplicate ids in window"
    )

    win.destroy()
    if FAILURES:
        print("FAILED: %d check(s):" % len(FAILURES))
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print("REAL-GTK FEED HARNESS: ALL CHECKS PASSED (%d checks)" % _CHECKS["n"])
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        import traceback

        traceback.print_exc()
        print("FAILED: harness exception: %s" % e)
        sys.exit(1)
