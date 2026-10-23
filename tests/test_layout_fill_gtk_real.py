"""Tier-2 real-GTK layout regression: main_stack must fill the window.

RELSET-4 wrapped main_stack in _root_box (key banner above the stack)
without vexpand; Gtk.Box gives slack space only to vexpand children, so
the stack rendered at natural height — footer icons stranded mid-screen,
feed filled only the top of the window (FLX1 report, 2026-10-23).

Replicates the structural shape (ToastOverlay > Box > [Banner, ViewStack
> NavigationSplitView]) at 540x1206 and asserts the ViewStack allocates
the full window height. Standalone only (xvfb-run python3 tests/
test_layout_fill_gtk_real.py); exits 77 without a display.
"""
import os
import sys

os.environ.setdefault("GDK_BACKEND", "x11")
try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import GLib, Gtk
except (ImportError, ValueError):
    sys.exit(77)
if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    sys.exit(77)

from gi.repository import Adw  # noqa: E402,F401  (require_version above)


def build():
    """Mirror MainWindow's root: ToastOverlay > Box > [Banner, ViewStack]."""
    win = Gtk.Window()
    win.set_default_size(540, 1206)
    overlay = Adw.ToastOverlay()
    win.set_child(overlay)
    root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    banner = Adw.Banner(title="x")
    banner.set_revealed(False)
    root.append(banner)
    stack = Adw.ViewStack()
    stack.set_vexpand(True)  # the fix under test
    split = Adw.NavigationSplitView()
    sb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    footer = Gtk.Box()
    footer.set_size_request(-1, 44)
    sb.append(Gtk.Box(vexpand=True))
    sb.append(footer)
    split.set_sidebar(Adw.NavigationPage(title="Menu", child=sb))
    sw = Gtk.ScrolledWindow()
    sw.set_child(Gtk.Label(label="feed"))
    split.set_content(Adw.NavigationPage(title="C", child=sw))
    split.set_collapsed(True)
    stack.add_named(split, "app")
    root.append(stack)
    overlay.set_child(root)
    return win, stack


def pump(n=60):
    ctx = GLib.MainContext.default()
    for _ in range(n):
        while ctx.pending():
            ctx.iteration(False)
        GLib.usleep(20_000)


def main():
    win, stack = build()
    win.present()
    pump()
    win_h = win.get_allocated_height()
    stack_h = stack.get_allocated_height()
    win.destroy()
    pump(10)
    print(f"window_h={win_h} stack_h={stack_h}")
    # banner is hidden (revealed=False) so the stack should own the height
    if stack_h < win_h - 60:
        print(f"FAIL: stack {stack_h} does not fill window {win_h}")
        return 1
    print("PASS: main_stack fills the window")
    return 0


sys.exit(main())
