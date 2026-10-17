"""Real-GTK DM back-affordance harness (issue DM-2, tier 2).

The Librem 5 e2e test (2026-10-16) saw TWO back buttons stacked in the DM
thread header: the page's manual go-previous button coexisted with the
automatic back button that Adw.HeaderBar shows for a page pushed onto an
Adw.NavigationView (MainWindow.content_nav). This harness constructs the
real DM views under real Gtk/Adw, pushes each onto a real NavigationView,
and asserts EXACTLY ONE visible back control per DM navigation state:

- inbox -> thread (thread page pushed)
- thread -> inbox (clicking the thread's back button pops the real nav)
- new-message flow (recipient picker pushed)
- inbox page itself

Runs standalone (NOT under the pytest suite: conftest mocks gi).
Skips cleanly with exit code 77 (meson "skip") when real GTK4/Adw or a
display is unavailable, so it is safe to register in meson unconditionally.

Usage:  xvfb-run -a python3 tests/test_dm_thread_back_gtk_real.py
"""

import importlib.util
import os
import sys
import tempfile
import types

# Isolated user-data dir BEFORE any GLib use.
_TMPDIR = tempfile.mkdtemp(prefix="gnostr-dmback-test-")
os.environ["XDG_DATA_HOME"] = os.path.join(_TMPDIR, "data")

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk
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


def _load_views():
    gp = types.ModuleType("gnostr")
    gp.__path__ = [SRC]
    sys.modules["gnostr"] = gp
    up = types.ModuleType("gnostr.ui")
    up.__path__ = [os.path.join(SRC, "ui")]
    sys.modules["gnostr.ui"] = up
    # nostr_utils imports ecdsa at module level; if the system GTK
    # interpreter lacks it, stub the one helper the views use.
    try:
        import ecdsa  # noqa: F401

        _load("gnostr.nostr_utils", os.path.join(SRC, "nostr_utils.py"), "gnostr")
    except ImportError:
        nu = types.ModuleType("gnostr.nostr_utils")

        def hex_to_npub(h):
            return "npub1" + (h or "")[:8]

        nu.hex_to_npub = hex_to_npub
        nu.resolve_profile_identifier = lambda t: (t if (t and len(t) == 64) else None)
        sys.modules["gnostr.nostr_utils"] = nu
    _load("gnostr.ui.icons", os.path.join(SRC, "ui", "icons.py"), "gnostr.ui")
    inbox = _load(
        "gnostr.ui.dm_inbox_view",
        os.path.join(SRC, "ui", "dm_inbox_view.py"),
        "gnostr.ui",
    )
    thread = _load(
        "gnostr.ui.dm_thread_view",
        os.path.join(SRC, "ui", "dm_thread_view.py"),
        "gnostr.ui",
    )
    recipient = _load(
        "gnostr.ui.dm_recipient_view",
        os.path.join(SRC, "ui", "dm_recipient_view.py"),
        "gnostr.ui",
    )
    return inbox, thread, recipient


PEER = "aa" * 32
OWNER = "cc" * 32


class StubGW:
    def get_thread(self, peer, limit, before):
        return []

    def get_conversations(self, limit=50):
        return []

    def mark_read(self, pk):
        pass


class StubClient:
    dm_gateway = StubGW()
    dm_service = None

    def connect(self, *args, **kwargs):
        pass

    def fetch_profile(self, pk):
        pass


class StubDB:
    def get_profile(self, pk):
        return {"display_name": "Alice"}

    def get_following_list(self, pk):
        return [PEER]


class StubMainWindow:
    """Minimal MainWindow surface the DM views touch. content_nav is the
    REAL Adw.NavigationView the harness pushes pages onto, so clicking a
    back control drives real navigation."""

    pub_key = OWNER

    def __init__(self):
        self.db = StubDB()
        self.client = StubClient()
        self.content_nav = None  # set by the harness before pushing
        self.thread_opens = []
        self.recipient_opens = 0

    def update_dm_badge(self):
        pass

    def show_dm_thread(self, pk):
        self.thread_opens.append(pk)

    def show_dm_recipient(self):
        self.recipient_opens += 1


def _walk(root):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        child = node.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()


def _visible_back_controls(root):
    """Every VISIBLE button in the subtree that reads as a back control:
    go-previous icon (manual buttons and Adw's automatic NavigationView
    back button both use it) or a 'Back' tooltip. The automatic button is
    hidden (visible=False) when the page cannot pop, so visibility is the
    real user-facing assertion."""
    out = []
    for node in _walk(root):
        if not isinstance(node, Gtk.Button) or not node.get_visible():
            continue
        icon = ""
        child = node.get_child()
        if isinstance(child, Gtk.Image):
            icon = child.get_icon_name() or ""
        tooltip = node.get_tooltip_text() or ""
        if "go-previous" in icon or tooltip.strip().lower() == "back":
            out.append(node)
    return out


def _assert_one_back(scope, page_child, failures):
    backs = _visible_back_controls(page_child)
    if len(backs) != 1:
        failures.append(
            "%s: expected exactly 1 visible back control, found %d"
            % (scope, len(backs))
        )
    return backs


def main():
    Adw.init()
    inbox_mod, thread_mod, recipient_mod = _load_views()
    win = StubMainWindow()

    # Real navigation stack mirroring MainWindow.content_nav.
    nav = Adw.NavigationView()
    win.content_nav = nav
    nav.push(Adw.NavigationPage(title="Feed", child=Gtk.Label(label="feed root")))

    top = Gtk.Window()
    top.set_child(nav)
    top.present()

    failures = []

    # --- state 1: inbox page (pushed) ---
    inbox = inbox_mod.DMInboxView(win)
    inbox_page = Adw.NavigationPage(title="Messages", child=inbox)
    nav.push(inbox_page)
    _assert_one_back("inbox page", inbox, failures)

    # --- state 2: inbox -> thread (the reported double-back state) ---
    thread = thread_mod.DMThreadView(win, PEER)
    nav.push(Adw.NavigationPage(title="DM", child=thread))
    backs = _assert_one_back("thread page", thread, failures)

    # --- state 3: thread -> inbox via the thread's single back control ---
    if backs:
        backs[0].emit("clicked")
        if nav.get_visible_page() is not inbox_page:
            failures.append(
                "thread back click did not pop to inbox (visible page=%r)"
                % nav.get_visible_page()
            )
    else:
        failures.append("thread page has no back control to click")

    # --- state 4: new-message flow (recipient picker pushed from inbox) ---
    recipient = recipient_mod.DmRecipientView(win)
    nav.push(Adw.NavigationPage(title="New Message", child=recipient))
    _assert_one_back("recipient picker page", recipient, failures)

    # --- state 5: thread pushed directly from inbox again, then popped
    # back: the inbox page must still show exactly one back control ---
    thread2 = thread_mod.DMThreadView(win, PEER)
    nav.push(Adw.NavigationPage(title="DM", child=thread2))
    _assert_one_back("thread page (re-entry)", thread2, failures)
    nav.pop()
    _assert_one_back("inbox after pop", inbox, failures)

    top.destroy()

    if failures:
        print("FAIL:")
        for f in failures:
            print("  -", f)
        return 1
    print(
        "OK: exactly one visible back control in every DM navigation state "
        "(inbox, thread, thread-back-click pops, recipient picker, re-entry)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
