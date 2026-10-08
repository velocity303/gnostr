"""Real-GTK clipboard harness (issue #33).

Exercises ExportKeyDialog.on_copy against the REAL Gdk clipboard under
xvfb and reads the value back with read_text_async — the exact path that
was silently broken when the code called the GTK3-only
Gdk.Clipboard.set_text (AttributeError swallowed by the signal handler).

Runs standalone (NOT under the pytest suite: conftest mocks gi).
Skips cleanly with exit code 77 (meson "skip") when real GTK4/Adw or a
display is unavailable.

Usage:  xvfb-run -a python3 tests/test_clipboard_gtk_real.py
"""
import os
import sys
import types
import importlib.util
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="gnostr-clip-test-")
os.environ["XDG_DATA_HOME"] = os.path.join(_TMPDIR, "data")

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk, Adw, Gdk, GLib
except Exception as e:
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
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = pkg
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_dialogs():
    gp = types.ModuleType("gnostr")
    gp.__path__ = [SRC]
    sys.modules["gnostr"] = gp
    # dialogs.py imports these at module level but only uses them inside
    # methods we don't exercise here; stub them so the system GTK
    # interpreter (no ecdsa/libsecret) can still load the module.
    km = types.ModuleType("gnostr.key_manager")
    km.KeyManager = object
    sys.modules["gnostr.key_manager"] = km
    nu = types.ModuleType("gnostr.nostr_utils")
    sys.modules["gnostr.nostr_utils"] = nu
    return _load("gnostr.dialogs", os.path.join(SRC, "dialogs.py"), "gnostr")


def _read_clipboard_sync(clipboard):
    """read_text_async + main loop; returns (text, error)."""
    box = {}
    loop = GLib.MainLoop()

    def cb(src, res, _):
        try:
            box["text"] = src.read_text_finish(res)
        except Exception as e:
            box["err"] = str(e)
        loop.quit()

    clipboard.read_text_async(None, cb, None)
    loop.run()
    return box.get("text"), box.get("err")


def main():
    Adw.init()
    dialogs = _load_dialogs()

    dlg = dialogs.ExportKeyDialog(parent=None)
    blob = "ncryptsec1qg9testblob" + "x" * 100
    dlg.result_label.set_text(blob)

    # The exact signal-handler path the Copy button runs.
    dlg.on_copy(dlg.copy_btn)

    if dlg.copy_btn.get_label() != "Copied!":
        print("FAIL: button label is %r, expected 'Copied!'" % dlg.copy_btn.get_label())
        return 1

    text, err = _read_clipboard_sync(Gdk.Display.get_default().get_clipboard())
    if err:
        print("FAIL: clipboard read_text_async error: %s" % err)
        return 1
    if text != blob:
        print("FAIL: clipboard holds %r (len=%s), expected the ncryptsec blob (len=%d)"
              % ((text[:40] + "...") if text else text,
                 len(text) if text else 0, len(blob)))
        return 1

    print("PASS: on_copy populated the real clipboard (%d chars) and the button "
          "flipped to 'Copied!'" % len(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
