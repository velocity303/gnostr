"""Real-GTK Blossom-wiring harness (NIPW-P1-FIX, FLX1 Bug A + Bug B).

The mocked tier-1 suite stayed green while the FLX1 device log showed two
device-only failures of the NIPW-P1 media resolver:

  Bug A — main.py connects media_resolver.on_blossom_event directly to the
  GObject signal 'blossom-servers-updated' (declared `(str,)` in client.py).
  PyGObject prepends the emitting instance to every handler call, so the
  1-parameter handler raised TypeError inside the signal machinery on every
  kind-10063 arrival: the server cache never filled and every resolve fell
  through to its full relay-wait timeout. Mocks never emit real signals, so
  only a real GObject emit catches this class of bug.

  Bug B — a media on_ready delivered twice for the same Gtk.Box (failed
  fetch -> placeholder, later rebuild/recovery -> content) appended an
  already-parented child: the gtk_box_append Gtk-CRITICAL burst (9x in one
  second in the FLX1 2026-11 log).

This harness runs both against REAL GObject/Gtk: a real signal emit must
populate the resolver cache, and a double delivery must leave exactly one
child on a real Gtk.Box with zero gtk_box_append criticals.

Runs standalone (NOT under the pytest suite: conftest mocks gi).
Skips cleanly with exit code 77 (meson "skip") when real GTK4 or a display
is unavailable.

Usage:  xvfb-run -a python3 tests/test_blossom_wiring_gtk_real.py
"""

import ast
import json
import os
import re
import sys
import types
import importlib.util

try:
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, GObject
except Exception as e:
    print("SKIP: real GTK4 unavailable (%s)" % e)
    sys.exit(77)

if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    print("SKIP: no display (run under xvfb-run)")
    sys.exit(77)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src", "gnostr")

PK = "a" * 64
DEAD_URL = "https://dead.example/" + "0" * 64 + ".png"

_criticals = []


def _log_handler(*args):
    # PyGObject passes (domain, priority, message); be tolerant of extras.
    try:
        domain, priority, message = args[0], args[1], args[2]
        if int(priority) >= int(GLib.LogLevelFlags.CRITICAL):
            _criticals.append("%s: %s" % (domain, message))
    except Exception:
        pass
    return False  # not handled: keep default printing too


def _load(name, path, pkg):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = pkg
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_resolver():
    """Load media_resolver standalone (no gnostr/__init__ -> no client/
    ecdsa/libsecret needed): synthetic package + its two real deps."""
    gp = types.ModuleType("gnostr")
    gp.__path__ = [SRC]
    sys.modules["gnostr"] = gp
    _load("gnostr.proxy_settings", os.path.join(SRC, "proxy_settings.py"), "gnostr")
    _load("gnostr.proxy_opener", os.path.join(SRC, "proxy_opener.py"), "gnostr")
    return _load(
        "gnostr.media_resolver", os.path.join(SRC, "media_resolver.py"), "gnostr"
    )


def _extract_func(filename, name, cls=None):
    """Compile one FunctionDef out of a source file so it can be exec'd
    against real Gtk with stub collaborators (house ast-extraction pattern;
    renderer.py imports Gst/humanize which the platform python lacks).
    `cls` disambiguates when a deprecated module-level wrapper shares the
    name with the real method."""
    with open(os.path.join(SRC, filename)) as f:
        tree = ast.parse(f.read())
    if cls:
        tree = ast.Module(
            body=[
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == cls
            ],
            type_ignores=[],
        )
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            node.decorator_list = []  # drop @staticmethod: exec as a plain func
            mod = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(mod)
            ns = {"Gtk": Gtk}
            exec(compile(mod, filename, "exec"), ns)
            return ns[name]
    raise AssertionError("%s not found in %s" % (name, filename))


# --- Bug A: real GObject emit must reach the resolver -------------------------


def check_signal_reaches_resolver():
    mr = _load_resolver()
    mr.reset_state()

    # Tie the harness declaration to the production one: client.py declares
    # the signal as (str,) — instance prepended by GObject at emit time.
    with open(os.path.join(SRC, "client.py")) as f:
        src = f.read()
    m = re.search(
        r'"blossom-servers-updated":\s*\(GObject\.SignalFlags\.\w+,\s*None,\s*\(([^)]*)\)\)',
        src,
    )
    assert m, "blossom-servers-updated signal declaration not found in client.py"
    assert (
        m.group(1).strip() == "str,"
    ), "signal spec changed to (%s) — update this harness to match" % m.group(1)

    class EmittingClient(GObject.Object):
        __gsignals__ = {
            "blossom-servers-updated": (GObject.SignalFlags.RUN_FIRST, None, (str,))
        }

    ev = json.dumps(
        {"kind": 10063, "pubkey": PK, "tags": [["r", "https://blossom.test"]]}
    )
    client = EmittingClient()
    client.connect("blossom-servers-updated", mr.on_blossom_event)
    client.emit("blossom-servers-updated", ev)

    entry = mr._servers.get(PK)
    assert entry is not None, (
        "resolver cache empty after a real signal emit — the handler raised "
        "inside the signal machinery (Bug A: arity must be (instance, payload))"
    )
    assert entry[0] == ["https://blossom.test"], "cache holds %r" % (entry[0],)
    mr.reset_state()
    print("PASS: real 'blossom-servers-updated' emit populated the resolver cache")


# --- Bug B: double delivery must leave exactly one child on a real box --------


def _texture():
    pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 2, 2)
    return Gdk.Texture.new_for_pixbuf(pb)


def _children(box):
    out = []
    c = box.get_first_child()
    while c is not None:
        out.append(c)
        c = c.get_next_sibling()
    return out


def check_image_double_delivery():
    captured = {}

    class StubLoader:
        @staticmethod
        def _request_image(url, callback, size=None, author_pk=None):
            captured["cb"] = callback

    load = _extract_func("renderer.py", "load_image_into_widget")
    ns = load.__globals__
    ns["ImageLoader"] = StubLoader

    box = Gtk.Box()
    spinner = Gtk.Spinner()
    box.append(spinner)
    load(DEAD_URL, box, spinner)
    cb = captured["cb"]

    cb(None)  # first delivery: fetch failed -> image-missing placeholder
    kids = _children(box)
    assert len(kids) == 1 and isinstance(kids[0], Gtk.Image), (
        "after failed delivery box holds %r" % kids
    )

    cb(_texture())  # second delivery: recovery texture — the FLX1 double-fire
    kids = _children(box)
    assert len(kids) == 1, "double delivery left %d children: %r" % (len(kids), kids)
    assert isinstance(kids[0], Gtk.Picture), "final child is %r" % kids[0]
    assert spinner.get_parent() is None, "spinner still parented after delivery"
    print("PASS: image on_ready double delivery left exactly one Gtk.Picture")


def check_video_double_delivery():
    captured = {}

    class StubPlayer:
        @staticmethod
        def _fetch_player(
            url, callback, original_url=None, autoplay=False, author_pk=None
        ):
            captured["cb"] = callback

        @staticmethod
        def _start_position_timer(container, video):
            pass

    load = _extract_func("renderer.py", "load_and_play", cls="VideoPlayer")
    ns = load.__globals__
    ns["VideoPlayer"] = StubPlayer
    StubPlayer.load_and_play = staticmethod(load)  # retry path self-reference

    box = Gtk.Box()
    spinner = Gtk.Spinner()
    box.append(spinner)
    load("https://dead.example/clip.mp4", box, spinner)
    cb = captured["cb"]

    cb(None)  # first delivery: build failed -> video-symbolic placeholder
    kids = _children(box)
    assert len(kids) == 1 and isinstance(kids[0], Gtk.Image), (
        "after failed delivery box holds %r" % kids
    )

    rebuilt = Gtk.Box()  # stand-in player widget
    cb(rebuilt)  # second delivery: rebuilt player onto the same box
    kids = _children(box)
    assert len(kids) == 1, "double delivery left %d children: %r" % (len(kids), kids)
    assert kids[0] is rebuilt, "final child is %r" % kids[0]
    assert spinner.get_parent() is None, "spinner still parented after delivery"
    print("PASS: video on_ready double delivery left exactly one player")


def main():
    GLib.log_set_handler(
        None,
        GLib.LogLevelFlags.LEVEL_ERROR | GLib.LogLevelFlags.LEVEL_CRITICAL,
        _log_handler,
        None,
    )
    checks = [
        check_signal_reaches_resolver,
        check_image_double_delivery,
        check_video_double_delivery,
    ]
    failed = 0
    for check in checks:
        try:
            check()
        except Exception as e:
            failed += 1
            print("FAIL: %s: %s" % (check.__name__, e))
    if _criticals:
        failed += 1
        print("FAIL: Gtk criticals were logged during the run:")
        for line in _criticals:
            print("  " + line)
    if failed:
        return 1
    print("PASS: all %d Blossom-wiring checks green" % len(checks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
