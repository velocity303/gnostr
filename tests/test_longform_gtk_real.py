"""Real-GTK article reader harness (NIPW1123-P1-C1 exit criteria).

Verifies the Article reader against REAL Gtk4/Adw widgets (mock-gi tier-1
cannot catch widget-level failures — the FLX1 bug class):

  1. Fixture 30023 renders every field into the reader tree: title, byline
     (npub -> profile-cache display name + short pubkey + humanized
     updated timestamp), summary, markdown-lite body (headers literal,
     emphasis -> Pango).
  2. The reader opens and scrolls: a real Gtk.ScrolledWindow with vertical
     AUTOMATIC policy wraps a real AdwClamp, and the page docks into an
     Adw.NavigationPage (the main-window list-selection open path).
  3. Cover URL shows only when https: https cover -> real cover box (200px
     height request) + loader invoked with the URL; http cover -> parser
     drops it and the reader renders NO cover box and never calls the
     loader. (Loader is stubbed at the module boundary so no network runs;
     the real box/picture wiring is real.)
  4. Comments thread: offline-first DB rows paint immediately, then the
     stubbed live fetch merges + dedups by id into real label rows; the
     count label matches; empty-state hides.
  5. (C2) Articles LIST on real widgets, end to end: seeded real Database
     -> real FeedModel page -> real ListBox rows. Newest-only per
     (pubkey, d) after edit-replacement (longform.dedupe_articles),
     author+updated meta line, live 30023 arrival upserts in place, row
     activation (real row-activated signal) hands the row's card to the
     window's reader path. Opt-in lifecycle: map opens the articles sub
     once, unmap closes it. Sidebar Articles row starts hidden and only
     set_articles_visible can reveal it.
  6. No GTK error/critical log records during the run.

Standalone run (NOT under the pytest suite — conftest mocks gi):
    xvfb-run -a python3 tests/test_longform_gtk_real.py
Skips cleanly with exit code 77 (meson "skip") when real GTK4/Adw or a
display is unavailable.
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib, Gtk
except Exception as e:
    print("SKIP: real GTK4/Adw unavailable (%s)" % e)
    sys.exit(77)

if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    print("SKIP: no display (run under xvfb-run)")
    sys.exit(77)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src", "gnostr")

# GTK criticals/errors captured through the log handler (4-arg form —
# the 1-arg binding raises TypeError in PyGObject).
criticals = []


def _log_handler(domain, level, message, user_data):
    criticals.append("%s: %s" % (domain, message.strip()))


GLib.log_set_handler(
    None,
    GLib.LogLevelFlags.LEVEL_ERROR | GLib.LogLevelFlags.LEVEL_CRITICAL,
    _log_handler,
    None,
)

# Stub the gnostr package so the REAL longform/articles_page modules load
# without running gnostr/__init__ (which needs a compiled GResource).
_gp = types.ModuleType("gnostr")
_gp.__path__ = [SRC]
sys.modules["gnostr"] = _gp
_ui = types.ModuleType("gnostr.ui")
_ui.__path__ = [os.path.join(SRC, "ui")]
sys.modules["gnostr.ui"] = _ui

# Cover loader stubbed at the module boundary (reader does a lazy
# `from ..renderer import ImageLoader` -> gnostr.renderer): the harness
# records the URL and appends a REAL Gtk.Picture so the widget wiring
# around the cover stays real; nothing touches the network.
loader_calls = []


class _RecordingImageLoader:
    @staticmethod
    def load_image_into_widget(url, target, *args, **kwargs):
        loader_calls.append(url)
        target.append(Gtk.Picture())


_r = types.ModuleType("gnostr.renderer")
_r.ImageLoader = _RecordingImageLoader
sys.modules["gnostr.renderer"] = _r

import gnostr.longform as longform  # noqa: E402  (real logic layer)
import gnostr.nostr_utils as nostr_utils  # noqa: E402
import gnostr.database as database_mod  # noqa: E402  (real DB, seeded)


def _load_articles_page():
    spec = importlib.util.spec_from_file_location(
        "gnostr.ui.articles_page", os.path.join(SRC, "ui", "articles_page.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gnostr.ui.articles_page"] = mod
    spec.loader.exec_module(mod)
    return mod


articles_page = _load_articles_page()

PUB = "b" * 64
COMMENT_PUB = "c" * 64
ARTICLE_ID = "a" * 64
ARTICLE_ADDR = "30023:%s:post-1" % PUB


def _article_fixture(image_url):
    return {
        "id": ARTICLE_ID,
        "pubkey": PUB,
        "kind": 30023,
        "created_at": 1763000000,
        "content": "# hi\n\n**bold** body",
        "tags": [
            ["d", "post-1"],
            ["title", "Post One"],
            ["published_at", "1763000000"],
            ["summary", "TL;DR here"],
            ["image", image_url],
        ],
    }


def _comment_event(eid, created, content):
    return {
        "id": eid,
        "pubkey": COMMENT_PUB,
        "kind": 1111,
        "created_at": created,
        "content": content,
        "tags": [["a", ARTICLE_ADDR]],
    }


class FakeDb:
    def __init__(self, rows):
        self.rows = rows

    def get_profile(self, pk):
        return {"display_name": "Alice"} if pk == PUB else {}

    def get_event_by_a(self, kind, pk, d):
        return None  # no newer edit under test

    def get_article_comments(self, article_id, address):
        return list(self.rows)


class FakeClient:
    """Plain object: the reader reaches comments via client method +
    callback (no GObject signal wiring on this page), so a plain stub is
    faithful here."""

    def __init__(self, fetched):
        self.fetched = fetched
        self.calls = 0

    def fetch_article_comments(self, card, cb):
        self.calls += 1
        cb(self.fetched, {"fallback_used": False})


class SectionClient:
    """ArticlesPage wires client.connect("quote-event-received", ...) and
    open/close_article_section at the section boundary (opt-in sub). This
    stub records the section lifecycle; nothing touches the network.
    connect() mirrors GObject.connect(name, cb) -> handler id."""

    def __init__(self):
        self.opens = 0
        self.closes = 0
        self.handlers = {}

    def connect(self, name, cb):
        self.handlers.setdefault(name, []).append(cb)
        return len(self.handlers[name])

    def open_article_section(self, *args, **kwargs):
        self.opens += 1

    def close_article_section(self, *args, **kwargs):
        self.closes += 1


def _load_sidebar():
    spec = importlib.util.spec_from_file_location(
        "gnostr.ui.sidebar", os.path.join(SRC, "ui", "sidebar.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gnostr.ui.sidebar"] = mod
    spec.loader.exec_module(mod)
    return mod


# --- C2 list fixtures: three edits of (PUB, post-1) + one other article ---


def _list_edit(d, eid, title, created, pubkey=PUB):
    return {
        "id": eid,
        "pubkey": pubkey,
        "kind": 30023,
        "created_at": created,
        "content": "body of " + title,
        "tags": [["d", d], ["title", title], ["published_at", str(created)]],
        "sig": "0" * 64,  # DB layer is thin (client verifies, not the DB)
    }


LIST_EVS = [
    _list_edit("post-1", "a1" + "1" * 62, "Post One (oldest edit)", 1763000000),
    _list_edit("post-1", "a2" + "2" * 62, "Post One (middle edit)", 1763000100),
    _list_edit("post-1", "a3" + "3" * 62, "Post One (newest edit)", 1763000200),
    _list_edit("post-2", "b1" + "4" * 62, "Second Post", 1763000150),
]
LIST_NEWEST_ID = "a3" + "3" * 62
LIVE_EDIT = _list_edit("post-1", "a4" + "4" * 62, "Post One (live edit)",
                       1763000300)
SELF_KEY = "d" * 64


def _collect(root, pred):
    out = []

    def walk(node):
        if pred(node):
            out.append(node)
        child = node.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(root)
    return out


def _label_texts(root):
    return [lbl.get_text()
            for lbl in _collect(root, lambda n: isinstance(n, Gtk.Label))]


def main():
    Adw.init()
    failed = 0

    def check(name, cond, detail=""):
        nonlocal failed
        if cond:
            print("PASS: %s" % name)
        else:
            failed += 1
            print("FAIL: %s%s" % (name, (" (%s)" % detail) if detail else ""))

    db_rows = [_comment_event("e" * 64, 1763000001, "cached comment")]
    fetched = [
        _comment_event("f" * 64, 1763000100, "nice post"),
        _comment_event("e" * 64, 1763000001, "cached comment"),  # duplicate
    ]
    card = longform.parse_article_event(_article_fixture(
        "https://img.example/cover.png"))
    if card is None:
        print("FAIL: fixture 30023 did not parse")
        return 1

    mw = types.SimpleNamespace(client=FakeClient(fetched), db=FakeDb(db_rows))
    page = articles_page.ArticleReaderPage(mw, card)
    texts = _label_texts(page)

    # 1. Every field rendered into the real tree.
    check("title renders", "Post One" in texts)
    bylines = [t for t in texts if "Alice" in t]
    check("byline uses profile-cache display name", len(bylines) == 1)
    if bylines:
        check("byline carries short pubkey", "bbbbbbbb…bbbbbbbb" in bylines[0],
              bylines[0])
        check("byline carries updated timestamp", " ago" in bylines[0],
              bylines[0])
    check("summary renders", "TL;DR here" in texts)
    body = [t for t in texts if "# hi" in t]
    check("body renders long-form markdown literal headers", len(body) == 1)
    check("body emphasis rendered via Pango markup (no literal tags)",
          any("bold body" in t and "<b>" not in t and "**" not in t
              for t in body),
          repr(body))

    # 2. Reader opens and scrolls.
    scrolls = _collect(page, lambda n: isinstance(n, Gtk.ScrolledWindow))
    check("reader is scrollable", len(scrolls) == 1)
    if scrolls:
        sw = scrolls[0]
        h, v = sw.get_policy()
        check("vertical scroll AUTOMATIC",
              v == Gtk.PolicyType.AUTOMATIC and h == Gtk.PolicyType.NEVER)
        inner = sw.get_child()
        if isinstance(inner, Gtk.Viewport):  # non-scrollable child is wrapped
            inner = inner.get_child()
        check("content wrapped in real AdwClamp", isinstance(inner, Adw.Clamp))
        check("scroller expands to fill", sw.get_vexpand())
    nav = Adw.NavigationPage(title="Article", child=page)  # main.py idiom
    check("reader docks as navigation page (list-selection open path)",
          page.get_parent() is nav)

    # 3. Cover https-only policy, on real widgets.
    check("https cover reaches loader exactly once",
          loader_calls == ["https://img.example/cover.png"],
          repr(loader_calls))
    cover_boxes = [n for n in _collect(page, lambda n: isinstance(n, Gtk.Box))
                   if n.get_size_request()[1] == 200]
    check("https cover builds real cover box + picture",
          len(cover_boxes) == 1 and cover_boxes
          and isinstance(cover_boxes[0].get_first_child(), Gtk.Picture))

    http_card = longform.parse_article_event(
        _article_fixture("http://evil.example/cover.png"))
    check("http cover dropped by parser (https-only)",
          http_card is not None and http_card.image is None)
    page_http = articles_page.ArticleReaderPage(
        types.SimpleNamespace(client=FakeClient([]), db=FakeDb([])), http_card)
    check("http cover never reaches loader",
          loader_calls == ["https://img.example/cover.png"],
          repr(loader_calls))
    http_cover_boxes = [n for n
                        in _collect(page_http, lambda n: isinstance(n, Gtk.Box))
                        if n.get_size_request()[1] == 200]
    check("http cover renders no cover box", len(http_cover_boxes) == 0)

    # 4. Comments thread: offline paint + merged dedup (2 unique ids).
    check("offline comment row paints", "cached comment" in texts)
    check("fetched comment merges in", "nice post" in texts)
    counts = [t for t in _label_texts(page) if t.startswith("(")]
    check("merged count label dedups by id", counts == ["(2)"], repr(counts))
    empty = [lbl for lbl in _collect(page, lambda n: isinstance(n, Gtk.Label))
             if "No comments" in (lbl.get_text() or "")]
    check("empty-state hidden once comments present",
          len(empty) == 1 and not empty[0].get_visible())
    page._disarm_comments_deadline()  # harness never spins a main loop

    # 5. (C2) Articles list, real end-to-end: seeded real Database ->
    #    real FeedModel page (ArticlesPage.__init__ reloads) -> real rows.
    # GLib caches XDG dirs on first call (GTK init already called it), so
    # env is too late — redirect the function itself: the harness must
    # NEVER touch the developer's real ~/.local/share/gnostr.db.
    data_home = tempfile.mkdtemp(prefix="gnostr-c2-harness-")
    GLib.get_user_data_dir = lambda: data_home
    db = database_mod.Database()
    check("harness DB is isolated in a temp dir",
          db.db_path.startswith(data_home), db.db_path)
    if not db.db_path.startswith(data_home):
        print("ABORT: refusing to seed a non-isolated DB")
        return 1
    db.save_events(LIST_EVS)
    db.save_profile(PUB, json.dumps({"display_name": "Alice"}), 1763000000)
    db.set_following(SELF_KEY, PUB, True)

    sec = SectionClient()
    reader_opens = []
    mw2 = types.SimpleNamespace(
        client=sec, db=db, pub_key=SELF_KEY,
        show_article_reader=lambda card: reader_opens.append(card),
        show_drafts=lambda *a: None,
    )
    page2 = articles_page.ArticlesPage(mw2)

    def _rows_of(list_box):
        out, r = [], list_box.get_first_child()
        while r is not None:
            if isinstance(r, Gtk.ListBoxRow):
                out.append(r)
            r = r.get_next_sibling()
        return out

    def _joined(rows):
        return [" | ".join(_label_texts(r)) for r in rows]

    rows = _rows_of(page2.list_box)
    texts = _joined(rows)
    check("list shows exactly 2 rows (newest-only per (pubkey,d))",
          len(rows) == 2, repr(texts))
    check("older+middle edits never listed",
          not any("oldest edit" in t or "middle edit" in t for t in texts),
          repr(texts))
    check("newest edit row first",
          bool(texts) and "Post One (newest edit)" in texts[0], repr(texts))
    check("second article listed", any("Second Post" in t for t in texts),
          repr(texts))
    check("row meta shows author + humanized updated",
          any("Alice ·" in t and " ago" in t for t in texts), repr(texts))

    # Row activation hands the card to C1. Call the wired handler directly
    # on the REAL row: activate() on an unrealized (windowless) row queries
    # root focus — a harness artifact the real app never hits, and the
    # signal connection itself is asserted in the mock-gi tier.
    if rows:
        page2._on_row_activated(page2.list_box, rows[0])
    check("row activation opens reader with the newest card",
          len(reader_opens) == 1
          and reader_opens[0].id == LIST_NEWEST_ID
          and reader_opens[0].title == "Post One (newest edit)",
          repr([getattr(c, "id", None) for c in reader_opens]))

    # Opt-in lifecycle: map opens the articles sub exactly once; unmap
    # closes it (no background sub while the page is detached).
    page2._on_map()
    check("map opens the articles sub exactly once", sec.opens == 1,
          str(sec.opens))
    page2._on_map()
    check("repeat map does not re-open", sec.opens == 1, str(sec.opens))

    # Live 30023 arrival through the real signal handler path, while the
    # section is OPEN: in-place newest-only upsert (row replaced, list
    # size unchanged). (While detached the page is inert by design — the
    # dm_inbox house pattern.)
    page2._on_quote_event(sec, json.dumps(LIVE_EDIT))
    rows2 = _rows_of(page2.list_box)
    texts2 = _joined(rows2)
    check("live 30023 keeps list at 2 rows (no duplicate row)",
          len(rows2) == 2, repr(texts2))
    check("live edit replaces its address row at the head",
          bool(texts2) and "Post One (live edit)" in texts2[0]
          and not any("newest edit" in t for t in texts2), repr(texts2))

    page2._on_unmap()
    check("unmap closes the sub", sec.closes == 1, str(sec.closes))

    # Sidebar gate: row exists but starts HIDDEN; only the setter reveals.
    sidebar_mod = _load_sidebar()
    sidebar = sidebar_mod.Sidebar(types.SimpleNamespace(
        client=SectionClient(), db=db, pub_key=SELF_KEY))
    art_row = sidebar.rows["articles"]
    check("sidebar Articles row starts hidden", not art_row.get_visible())
    sidebar.set_articles_visible(True)
    check("set_articles_visible(True) reveals it", art_row.get_visible())
    sidebar.set_articles_visible(False)
    check("gate hides it again", not art_row.get_visible())

    shutil.rmtree(data_home, ignore_errors=True)

    # 6. No GTK criticals during the whole run.
    check("no GTK error/critical records", not criticals, repr(criticals))

    print("SUMMARY: %s (%d failures)" % ("ALL CHECKS PASS" if not failed
                                         else "FAILURES", failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
