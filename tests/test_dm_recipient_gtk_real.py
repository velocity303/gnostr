"""Real-GTK recipient-picker smoke (spec Phase A2, tier 2).

Constructs ui.dm_recipient_view.DmRecipientView against REAL Gtk/Adw and
drives the real signal path: reload() builds real Adw.ActionRows from a
stub follows set, typing in the real Gtk.SearchEntry filters them, and a
pasted npub pins a synthetic "Message …" row whose activation calls
main_window.show_dm_thread. This is the construct-and-filter smoke the
headless ast tests cannot do (they never touch real widget machinery).

Runs standalone (NOT under the pytest suite: conftest mocks gi).
Skips cleanly with exit code 77 (meson "skip") when real GTK4/Adw or a
display is unavailable, so it is safe to register in meson unconditionally.

Usage:  xvfb-run -a python3 tests/test_dm_recipient_gtk_real.py
"""

import os
import sys
import types
import importlib.util
import tempfile

# Isolated user-data dir BEFORE any GLib use.
_TMPDIR = tempfile.mkdtemp(prefix="gnostr-dmpick-test-")
os.environ["XDG_DATA_HOME"] = os.path.join(_TMPDIR, "data")

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk, Adw
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


def _load_view():
    gp = types.ModuleType("gnostr")
    gp.__path__ = [SRC]
    sys.modules["gnostr"] = gp
    up = types.ModuleType("gnostr.ui")
    up.__path__ = [os.path.join(SRC, "ui")]
    sys.modules["gnostr.ui"] = up
    # nostr_utils imports ecdsa at module level; if the system GTK
    # interpreter lacks it, stub the two helpers the view uses.
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
    return _load(
        "gnostr.ui.dm_recipient_view",
        os.path.join(SRC, "ui", "dm_recipient_view.py"),
        "gnostr.ui",
    )


PEER_A = "aa" * 32
PEER_B = "bb" * 32
OWNER = "cc" * 32


class StubDB:
    def __init__(self, follows=(PEER_A, PEER_B)):
        self._follows = list(follows)

    def get_following_list(self, pk):
        return list(self._follows)

    def get_profile(self, pk):
        return {
            PEER_A: {"display_name": "Alice Liddell"},
            PEER_B: {"name": "bob"},
        }.get(pk, {})


class StubClient:
    def __init__(self):
        self.fetched = []

    def fetch_profile(self, pk):
        self.fetched.append(pk)


class StubMainWindow:
    pub_key = OWNER

    def __init__(self, db=None):
        self.db = db or StubDB()
        self.client = StubClient()
        self.opened = []

    def show_dm_thread(self, pk):
        self.opened.append(pk)

    def content_nav_pop(self):
        pass


def _row_titles(view):
    out = []
    row = view.list_box.get_first_child()
    while row is not None:
        # Adw.ActionRow IS a Gtk.ListBoxRow subclass — the title lives on
        # the row itself, not on a wrapped child.
        if isinstance(row, Adw.ActionRow):
            out.append(row.get_title())
        row = row.get_next_sibling()
    return out


def _scroll_contains(view, widget):
    """True when `widget` is reachable from the ScrolledWindow down the
    child chain (GTK auto-wraps non-Scrollable children like StatusPage
    in a Viewport; walk get_first_child/get_next_sibling —
    Gtk.Widget.get_children is removed in the GTK 4.22 runtime floor)."""
    seen = [view.scroll]
    while seen:
        node = seen.pop()
        if node is widget:
            return True
        child = node.get_first_child()
        while child is not None:
            seen.append(child)
            child = child.get_next_sibling()
    return False


def _scroll_reaches_list_box(view):
    return _scroll_contains(view, view.list_box)


def _type(view, text):
    """set_text + emit search-changed — programmatic set_text does NOT
    emit the signal, real typing does; emit it to drive the real handler."""
    view.search_entry.set_text(text)
    view.search_entry.emit("search-changed")


def main():
    Adw.init()
    mod = _load_view()
    win = StubMainWindow()
    view = mod.DmRecipientView(win)

    # 1. Full follows set renders two named rows.
    titles = _row_titles(view)
    if titles != ["Alice Liddell", "bob"]:
        print("FAIL: initial rows %r, expected ['Alice Liddell', 'bob']" % titles)
        return 1
    if sorted(win.client.fetched) != sorted([PEER_A, PEER_B]):
        print(
            "FAIL: fetch_profile not kicked for both follows: %r" % win.client.fetched
        )
        return 1

    # 2. Real SearchEntry typing filters case-insensitively on name.
    _type(view, "LIDDELL")
    titles = _row_titles(view)
    if titles != ["Alice Liddell"]:
        print("FAIL: filter 'LIDDELL' rows %r, expected ['Alice Liddell']" % titles)
        return 1

    # 3. No-match shows the No matches row (dataset non-empty).
    _type(view, "zzzz")
    titles = _row_titles(view)
    if titles != ["No matches"]:
        print("FAIL: no-match rows %r, expected ['No matches']" % titles)
        return 1

    # 4. Pasted npub pins a synthetic Message row and activates the thread.
    from gnostr.nostr_utils import hex_to_npub

    _type(view, hex_to_npub(PEER_A))
    titles = _row_titles(view)
    if not titles or not titles[0].startswith("Message npub"):
        print("FAIL: paste-pin rows %r, expected pinned 'Message npub…' first" % titles)
        return 1
    view._on_entry_activate(view.search_entry)
    if win.opened != [PEER_A]:
        print("FAIL: activate opened %r, expected [%s]" % (win.opened, PEER_A))
        return 1

    # 5. Own npub is never a recipient (nsec-equivalent rejection).
    _type(view, hex_to_npub(OWNER))
    titles = _row_titles(view)
    if any(t.startswith("Message") for t in titles):
        print("FAIL: own npub pinned a Message row: %r" % titles)
        return 1

    # 6. Empty-follows regression: after the StatusPage is shown, pasting
    # a valid npub must RESTORE the list under the scroll (the old bug
    # re-attached the StatusPage, so the pinned row was unreachable).
    empty_win = StubMainWindow(db=StubDB(follows=[]))
    eview = mod.DmRecipientView(empty_win)
    status = eview.empty_page
    if not isinstance(status, Adw.StatusPage) or not _scroll_contains(eview, status):
        print("FAIL: empty follows did not show the StatusPage under the scroll")
        return 1
    _type(eview, hex_to_npub(PEER_A))
    if not _scroll_reaches_list_box(eview):
        print(
            "FAIL: paste after empty-follows never restored the list under the scroll"
        )
        return 1
    titles = _row_titles(eview)
    if not titles or not titles[0].startswith("Message npub"):
        print(
            "FAIL: empty-follows paste rows %r, expected pinned 'Message npub…' first"
            % titles
        )
        return 1

    print(
        "PASS: DmRecipientView constructed under real GTK; filter, paste-pin, "
        "activate, own-key rejection and empty-follows restore all behaved (%d checks)"
        % 6
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
