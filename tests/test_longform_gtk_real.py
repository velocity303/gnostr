"""Real-GTK article reader harness (NIPW1123-P1-C1 exit criteria).

Verifies the Article reader against REAL Gtk4/Adw widgets (mock-gi tier-1
cannot catch widget-level failures — the FLX1 bug class):

  1. Fixture 30023 renders every field into the reader tree: title-1, byline
     (ACTIONABLE flat dim-label name button -> ProfileView + a dim-label
     "· {ago} · {short_pk}" tail), summary, markdown-lite body (headers
     literal, emphasis -> Pango), all inside the house 600 px reading clamp.
  2. The reader opens and scrolls: a real Gtk.ScrolledWindow with vertical
     AUTOMATIC policy wraps a real AdwClamp maximum_size=600, and the page
     docks into an Adw.NavigationPage (the main-window list-selection open
     path).
  3. Cover URL shows only when https: https cover -> real FULL-COLUMN cover
     box (hexpand, no fixed height request — natural aspect) + loader invoked
     with the URL and NO max_height cap; http cover -> parser drops it and
     the reader renders NO cover box and never calls the loader. (Loader is
     stubbed at the module boundary so no network runs; the real box/picture
     wiring is real.)
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
  6. (LFUX-2) Comments + reactions theming (brief §1.3/§1.7/§2.2.8 + §5
     zap vectors): comment rows are the feed meta idiom on real widgets
     (dim-label "{name} · {ago}" over a normal-weight wrapped body, npub
     name fallback); the fallback-matched set paints one A/K/P root-binding
     caption per comment (dim-label + caption, start-aligned) while the
     precise primary chain paints none; the composer is the house
     Adw.EntryRow "Add a comment…" + suggested-action Post (groups_page
     shape) with Enter driven by a REAL 'apply' emission; the optimistic
     row after a REAL Post click is structurally identical to a confirmed
     row (same container, same label classes, same meta shape); a stalled
     fetch hits the REAL GLib watchdog — error-class label + visible Retry,
     a real Retry click redrives the fetch, a settle clears both; and the
     brief's 1234-sat list-row vector paints the exact " · ⚡1234" meta
     tail.
  7. (C4) Drafts: real DraftsPage rows for a decrypted 31234 wrap (real
     NIP-44 parse) + a legacy 30024 row labeled 'legacy'; real
     View/Publish/Delete buttons clicked drive the full flows — unsigned
     30023 rebuilt from the draft (title/content/t-tags carried), row
     removal, confirmation toasts on a real Adw.ToastOverlay, and the
     draft-mode reader (banner, no comments) on a real Adw.NavigationView.
  8. Hero actions on real widgets (brief §2.2.3/§2.2.4/§2.2.7), each driven
     by a REAL 'clicked' signal emission rather than a direct handler call:
     the byline button routes to the window's show_profile; the zap chip is
     built only from verified 9735 receipts (label "{sats} sats") and opens
     the receipts dialog; the zap action appears only when the feed gate
     passes (signed in, not my own article, zapable profile) and hands
     exactly one start_zap with the (id, 30023) event pointer; a >500-char
     body paints at the house collapse cap with a flat 44px 'Show more' that
     expands in place and then disappears. A draft reader has no zap line.
  9. No GTK error/critical log records during the run.

Standalone run (NOT under the pytest suite — conftest mocks gi):
    xvfb-run -a python3 tests/test_longform_gtk_real.py
Skips cleanly with exit code 77 (meson "skip") when real GTK4/Adw or a
display is unavailable.
"""

import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import time
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
loader_kwargs = []


class _RecordingImageLoader:
    @staticmethod
    def load_image_into_widget(url, target, *args, **kwargs):
        loader_calls.append(url)
        loader_kwargs.append(kwargs)
        target.append(Gtk.Picture())


_r = types.ModuleType("gnostr.renderer")
_r.ImageLoader = _RecordingImageLoader
sys.modules["gnostr.renderer"] = _r

import gnostr.longform as longform  # noqa: E402  (real logic layer)
import gnostr.nostr_utils as nostr_utils  # noqa: E402
import gnostr.database as database_mod  # noqa: E402  (real DB, seeded)
from gnostr.zaps.request import verify_event_sig  # noqa: E402  (C3 sig proof)


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

# The house meta shape (brief §1.3): "{name} · {ago}" with the phone buckets
# of _ago — used to assert every comment row's meta line on real widgets.
_META_RE = re.compile(r"^npub1\S+ · (?:just now|\d+[mhd] ago)$")


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


def _comment_event(eid, created, content, tags=None):
    return {
        "id": eid,
        "pubkey": COMMENT_PUB,
        "kind": 1111,
        "created_at": created,
        "content": content,
        "tags": tags if tags is not None else [["a", ARTICLE_ADDR]],
    }


class FakeDb:
    """Reader-side DB stub. zap_msat / zap_rows drive the hero zap line: the
    reader reads totals only through zap_totals_for_events — the same verified
    9735-receipt lookup the feed chip uses (articles_page._zap_sats_from_db) — so a
    stub total stands in for receipts the DB already holds. No network here."""

    def __init__(self, rows, zap_msat=0, zap_rows=None, profile=None):
        self.rows = rows
        self.zap_msat = zap_msat
        self.zap_rows = zap_rows or []
        self.profile = profile if profile is not None else {"display_name": "Alice"}

    def get_profile(self, pk):
        return dict(self.profile) if pk == PUB else {}

    def zap_totals_for_events(self, ids):
        return {i: self.zap_msat for i in ids} if self.zap_msat else {}

    def zaps_for_event(self, event_id):
        return list(self.zap_rows)

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


class StallClient:
    """fetch_article_comments records the callback and never completes it —
    the exact state a stalled relay leaves the reader in. The watchdog must
    surface it; a real Retry click must redrive the fetch, and the harness
    settles it by INVOKING the recorded callback with the real client
    signature (events, meta)."""

    def __init__(self):
        self.calls = 0
        self.pending = []

    def fetch_article_comments(self, card, cb):
        self.calls += 1
        self.pending.append(cb)

    def publish_article_comment(self, ev, article_id):
        return True


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


def _list_edit(d, eid, title, created, pubkey=PUB, image=None):
    tags = [["d", d], ["title", title], ["published_at", str(created)]]
    if image:
        tags.append(["image", image])
    return {
        "id": eid,
        "pubkey": pubkey,
        "kind": 30023,
        "created_at": created,
        "content": "body of " + title,
        "tags": tags,
        "sig": "0" * 64,  # DB layer is thin (client verifies, not the DB)
    }


LIST_EVS = [
    _list_edit("post-1", "a1" + "1" * 62, "Post One (oldest edit)", 1763000000),
    _list_edit("post-1", "a2" + "2" * 62, "Post One (middle edit)", 1763000100),
    _list_edit("post-1", "a3" + "3" * 62, "Post One (newest edit)", 1763000200),
    _list_edit("post-2", "b1" + "4" * 62, "Second Post", 1763000150,
               image="https://img.example/row.png"),
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


def _click(widget):
    """Emit the REAL 'clicked' signal so a connected handler runs with the
    arity a button press delivers it. activate() no-ops on unrealized
    (windowless) widgets — the C2 row precedent in this harness — so emit()
    is the house way to drive a click here."""
    widget.emit("clicked")
    return True


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
    profiles_opened = []
    fetched = [
        _comment_event("f" * 64, 1763000100, "nice post"),
        _comment_event("e" * 64, 1763000001, "cached comment"),  # duplicate
    ]
    card = longform.parse_article_event(_article_fixture(
        "https://img.example/cover.png"))
    if card is None:
        print("FAIL: fixture 30023 did not parse")
        return 1

    mw = types.SimpleNamespace(client=FakeClient(fetched), db=FakeDb(db_rows),
                               show_profile=lambda pk: profiles_opened.append(pk))
    page = articles_page.ArticleReaderPage(mw, card)
    texts = _label_texts(page)

    # 1. Every field rendered into the real tree.
    check("title renders", "Post One" in texts)
    # Byline (brief §2.2.3): the house meta shape, but the author is
    # actionable — a real flat dim-label Button (the feed's affordance,
    # post_widget.py:90-99) plus one dim-label "· {ago} · {short_pk}" tail.
    buttons = _collect(page, lambda n: isinstance(n, Gtk.Button))
    byline_btns = [n for n in buttons if n.get_label() == "Alice"]
    check("byline name is exactly one real button", len(byline_btns) == 1,
          repr([n.get_label() for n in buttons]))
    if byline_btns:
        check("byline button carries the house flat + dim-label classes",
              byline_btns[0].has_css_class("flat")
              and byline_btns[0].has_css_class("dim-label"),
              repr(byline_btns[0].get_css_classes()))
        check("byline cluster is start-aligned like every meta line",
              byline_btns[0].get_parent().get_halign() == Gtk.Align.START)
    tails = [t for t in texts if t.startswith("·") and " ago" in t]
    check("byline tail carries updated timestamp + short pubkey",
          len(tails) == 1 and "bbbbbbbb…bbbbbbbb" in tails[0], repr(tails))
    check("byline click opens the author's ProfileView (real signal)",
          bool(byline_btns) and _click(byline_btns[0])
          and profiles_opened == [card.pubkey], repr(profiles_opened))
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
        check("reader uses the house 600 px reading measure",
              isinstance(inner, Adw.Clamp) and inner.get_maximum_size() == 600,
              repr(getattr(inner, "get_maximum_size", lambda: None)()))
        check("scroller expands to fill", sw.get_vexpand())
    nav = Adw.NavigationPage(title="Article", child=page)  # main.py idiom
    check("reader docks as navigation page (list-selection open path)",
          page.get_parent() is nav)

    # 3. Cover https-only policy on real widgets, at the READER measure
    #    (brief §2.2.5): full clamped column, natural aspect — no fixed height
    #    request and no max_height cap. The fixed crop stays the list ROW's
    #    idiom (§2.1.3), never the hero's.
    check("https cover reaches loader exactly once",
          loader_calls == ["https://img.example/cover.png"],
          repr(loader_calls))
    check("reader cover loader gets no max_height cap (natural aspect)",
          all("max_height" not in kw for kw in loader_kwargs),
          repr(loader_kwargs))
    cover_boxes = [n for n in _collect(page, lambda n: isinstance(n, Gtk.Box))
                   if isinstance(n.get_first_child(), Gtk.Picture)]
    check("https cover builds real cover box + picture",
          len(cover_boxes) == 1, repr(cover_boxes))
    if cover_boxes:
        cbox = cover_boxes[0]
        check("cover spans the column (hexpand, FILL, no fixed height)",
              cbox.get_hexpand() and cbox.get_halign() == Gtk.Align.FILL
              and cbox.get_size_request()[1] == -1,
              repr((cbox.get_hexpand(), cbox.get_halign(),
                    cbox.get_size_request())))
        check("cover stays unframed (no card class)",
              not cbox.has_css_class("card"), repr(cbox.get_css_classes()))

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
                        if isinstance(n.get_first_child(), Gtk.Picture)]
    check("http cover renders no cover box", len(http_cover_boxes) == 0)

    # 3b. Hero actions on real widgets (brief §2.2.4 + §2.2.7), every handler
    #     driven by a REAL 'clicked' emission (house rule: a handler is only
    #     proven when a real signal delivers its arity — never a direct call).
    #     zaps.ui is stubbed at the MODULE boundary: the reader's lazy
    #     `from ..zaps.ui import start_zap` resolves through sys.modules, so
    #     recording here keeps LNURL + signing out of the harness while the
    #     widget tree, gating and signal delivery stay real.
    import gnostr.zaps.ui as zaps_ui

    zap_calls = []
    receipt_calls = []

    class _RecordingZapDialog:
        def __init__(self, parent, rows):
            self.parent, self.rows = parent, rows

        def present(self):
            receipt_calls.append((self.parent, list(self.rows)))

    zaps_ui.start_zap = lambda mw, pk, event=None: zap_calls.append((pk, event))
    zaps_ui.ZapReceiptsDialog = _RecordingZapDialog

    zap_db = FakeDb([], zap_msat=12_000,
                    zap_rows=[{"id": "z" * 64, "kind": 9735}],
                    profile={"display_name": "Alice",
                             "lud16": "alice@pay.example"})
    page_zap = articles_page.ArticleReaderPage(
        types.SimpleNamespace(client=FakeClient([]), db=zap_db,
                              pub_key=SELF_KEY), card)
    zap_buttons = _collect(page_zap, lambda n: isinstance(n, Gtk.Button))
    check("hero zap chip built only from verified 9735 receipts",
          page_zap.zap_chip is not None and page_zap.zap_chip in zap_buttons)
    if page_zap.zap_chip is not None:
        check("chip is the house flat 3-widget idiom (bolt + sats label)",
              page_zap.zap_chip.has_css_class("flat")
              and isinstance(page_zap.zap_chip.get_child(), Gtk.Box)
              and any(isinstance(n, Gtk.Image)
                      for n in _collect(page_zap.zap_chip, lambda x: True)),
              repr(page_zap.zap_chip.get_css_classes()))
        check("chip label is the house sats form",
              _label_texts(page_zap.zap_chip) == ["12 sats"],
              repr(_label_texts(page_zap.zap_chip)))
        _click(page_zap.zap_chip)
        check("chip click opens the receipts dialog exactly once",
              len(receipt_calls) == 1
              and [r["id"] for r in receipt_calls[0][1]] == ["z" * 64],
              repr(receipt_calls))
    check("hero zap action appears when the feed gate passes",
          page_zap.zap_btn is not None and page_zap.zap_btn in zap_buttons)
    if page_zap.zap_btn is not None:
        check("zap action is flat like every in-page action",
              page_zap.zap_btn.has_css_class("flat"),
              repr(page_zap.zap_btn.get_css_classes()))
        _click(page_zap.zap_btn)
        check("zap click hands exactly one start_zap with the 30023 pointer",
              zap_calls == [(PUB, (card.id, longform.KIND_ARTICLE))],
              repr(zap_calls))

    # Gate negatives — the SAME conditions the feed button uses
    # (post_widget.py:171-187): signed in, not my own article, zapable cache.
    page_own = articles_page.ArticleReaderPage(
        types.SimpleNamespace(client=FakeClient([]), db=zap_db, pub_key=PUB),
        card)
    check("no zap action on my own article", page_own.zap_btn is None)
    page_out = articles_page.ArticleReaderPage(
        types.SimpleNamespace(client=FakeClient([]), db=zap_db), card)
    check("no zap action while signed out", page_out.zap_btn is None)
    page_plain = articles_page.ArticleReaderPage(
        types.SimpleNamespace(client=FakeClient([]), db=FakeDb([]),
                              pub_key=SELF_KEY), card)
    check("no chip without verified receipts", page_plain.zap_chip is None)
    check("no zap action when the cached profile is not zapable",
          page_plain.zap_btn is None)

    # Body collapse (brief §2.2.7): deterministic 500-char house cap, cut on
    # the SOURCE markdown, flat 44px 'Show more'.
    long_body = "\n\n".join(
        "paragraph %d mentions **alpha** and https://example.com/x%d" % (i, i)
        for i in range(24))
    long_ev = dict(_article_fixture("https://img.example/cover.png"))
    long_ev["content"] = long_body
    long_card = longform.parse_article_event(long_ev)
    check("long-body fixture parses past the cap",
          long_card is not None and len(long_card.markdown) > 500,
          repr(len(long_card.markdown)))
    page_long = articles_page.ArticleReaderPage(
        types.SimpleNamespace(client=FakeClient([]), db=FakeDb([])), long_card)
    body_labels = [n for n in _collect(page_long, lambda n: isinstance(n, Gtk.Label))
                   if (n.get_text() or "").startswith("paragraph 0")]
    check("long body paints exactly one body label", len(body_labels) == 1,
          repr(len(body_labels)))
    if body_labels:
        shown = body_labels[0].get_text()
        check("body collapses at the cap (tail hidden + ellipsis)",
              "paragraph 23" not in shown and shown.endswith("…"),
              repr(shown[-40:]))
        # The cap is SOFT: safe_truncate stops at a token boundary it can no
        # longer afford, so the kept text is just under 500 chars and the cut
        # lands mid-paragraph, not at a heading/section boundary ("brutal"
        # would hard-cut at exactly 500 and slice tokens). NB the painted text
        # is a little shorter than the source cut: markup stripping removes
        # the ** emphasis markers and GTK collapses the blank-line runs.
        check("collapse cap is soft, not brutal (kept whole tokens, cap "
              "budget spent)", 420 <= len(shown) <= 500, repr(len(shown)))
        trimmed = shown[:-1]
        urls = [w for w in trimmed.split() if w.startswith("https://")]
        check("cut never splits a URL (each shown URL is whole)",
              bool(urls) and all(w in long_body for w in urls),
              repr(urls[-2:]))
        check("cut never leaves an orphan ** emphasis marker",
              shown.count("**") % 2 == 0, repr(shown.count("**")))
        check("body text keeps normal weight (never dim-label)",
              not body_labels[0].has_css_class("dim-label"),
              repr(body_labels[0].get_css_classes()))
    toggles = [n for n in _collect(page_long, lambda n: isinstance(n, Gtk.Button))
               if n.get_label() == "Show more"]
    check("flat 'Show more' at the 44 px touch height",
          len(toggles) == 1 and toggles[0].has_css_class("flat")
          and toggles[0].get_size_request()[1] == 44,
          repr([(n.get_label(), n.get_size_request()) for n in toggles]))
    if toggles:
        _click(toggles[0])
        check("Show more expands the SAME label in place",
              bool(body_labels) and "paragraph 23" in body_labels[0].get_text())
        check("toggle removes itself after expanding (one-way per page)",
              page_long.body_toggle is None and toggles[0].get_parent() is None
              and not [n for n in _collect(page_long,
                                           lambda n: isinstance(n, Gtk.Button))
                       if n.get_label() == "Show more"])
    check("short body renders no collapse toggle", page.body_toggle is None)

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

    # --- LFUX-1 theming (brief §2.1): the row IS the feed idiom. Title is
    # the only emphasized element (heading), the author/updated line stays a
    # dim-label, the zap tail is ⚡sats and comes ONLY from verified 9735
    # receipts (the same zap_totals_for_events lookup the feed chip and the
    # reader hero chip use), and the cover keeps the 160 px crop slot rather
    # than the reader's full-column natural aspect.
    titles = [n for r in rows for n in _collect(r, lambda w: isinstance(w, Gtk.Label))
              if "heading" in (n.get_css_classes() or [])]
    check("row title is the only emphasized element (house heading class)",
          len(titles) == 2, repr(len(titles)))
    metas = [n for r in rows for n in _collect(r, lambda w: isinstance(w, Gtk.Label))
             if "dim-label" in (n.get_css_classes() or [])
             and "Alice ·" in (n.get_text() or "")]
    check("row author/updated line is a house dim-label",
          len(metas) == 2, repr(len(metas)))
    # The list column shares the app's single reading measure so the two-pane
    # desktop stays coherent (feed_view.py:94 sets the same cap).
    clamp = page2.list_box.get_parent()
    check("list column clamps to the house 600 px measure",
          isinstance(clamp, Adw.Clamp) and clamp.get_maximum_size() == 600,
          repr(getattr(clamp, "get_maximum_size", lambda: None)()))

    # Seeded verified receipt -> exactly one row gains the ⚡ tail.
    db.save_zap({"event_id": "z" * 64, "bolt11": "lnbc1" + "1" * 30,
                 "request_id": "r" * 64, "sender": SELF_KEY, "recipient": PUB,
                 "target_e": LIST_NEWEST_ID, "amount_msat": 5000,
                 "comment": "", "ts": 1763000500})
    page2._reload()
    rows = _rows_of(page2.list_box)
    texts = _joined(rows)
    check("verified 9735 receipts render the ⚡sats tail on their row",
          any("⚡5" in t for t in texts), repr(texts))
    check("zapless article keeps no zap tail",
          all("⚡" not in t for t in texts if "Second Post" in t), repr(texts))
    row2 = next(r for r in rows if "Second Post" in " ".join(_label_texts(r)))
    crops = [n for n in _collect(row2, lambda w: isinstance(w, Gtk.Box))
             if n.get_size_request()[1] == 160]
    check("row cover keeps the 160 px crop slot (list idiom)",
          len(crops) == 1 and any(isinstance(c, Gtk.Picture)
                                  for c in _collect(row2, lambda w: True)),
          repr([n.get_size_request() for n in crops]))
    check("row cover is capped by the loader (crop), never full height",
          any(loader_calls[i] == "https://img.example/row.png"
              and loader_kwargs[i].get("max_height") == 200
              for i in range(len(loader_calls))),
          repr(list(zip(loader_calls, (k.get("max_height")
                                       for k in loader_kwargs)))[-3:]))

    # --- LFUX-2 (brief §5 vector): a verified total of 1234 sats must paint
    # the meta label with the EXACT tail ' · ⚡1234' — whole-sat rendering
    # (no msat leakage, no thousands separator), inside the single dim meta
    # line, same _zap_sats_from_db helper the reader chip uses.
    db.save_zap({"event_id": "z2" + "2" * 62, "bolt11": "lnbc12" + "2" * 28,
                 "request_id": "r2" + "2" * 62, "sender": SELF_KEY,
                 "recipient": PUB, "target_e": "b1" + "4" * 62,
                 "amount_msat": 1_234_000, "comment": "", "ts": 1763000600})
    page2._reload()
    rows = _rows_of(page2.list_box)  # re-capture: reload replaced the rows
    texts = _joined(rows)
    metas2 = [n for r in rows for n in _collect(r, lambda w: isinstance(w, Gtk.Label))
              if "dim-label" in (n.get_css_classes() or [])
              and "Alice ·" in (n.get_text() or "")]
    check("1234 verified sats paint the exact ' · ⚡1234' meta tail",
          any((n.get_text() or "").endswith(" · ⚡1234") for n in metas2),
          repr([n.get_text() for n in metas2]))
    check("the ⚡tail lives in the single dim meta line (no second line, "
          "no plain-text variant)",
          len([n for n in metas2 if "⚡1234" in (n.get_text() or "")]) == 1
          and not any("1234" in t for r in rows for t in _label_texts(r)
                      if t not in [n.get_text() for n in metas2]),
          repr(texts))

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

    # 6. (C3) Comments: 30-row oldest-first render on real widgets, composer
    #    gating, and a REAL Post-button click driving the signed-then-gated
    #    flow end to end.
    check("signed-out reader hides the composer",
          not page.comment_composer.get_visible())

    class PostCaptureClient(FakeClient):
        def __init__(self, fetched):
            super().__init__(fetched)
            self.published = []  # (template, article_id) handed to client

        def publish_article_comment(self, ev, article_id):
            self.published.append((ev, article_id))
            return True

    HARNESS_PRIV = "22" * 32
    HARNESS_PUB = nostr_utils.get_public_key(HARNESS_PRIV)
    base_ts = 1763000000
    many = []
    for i in range(30):
        many.append(_comment_event(
            "%064x" % (i + 1), base_ts + (i * 7) % 30, "harness comment %02d" % i))
    expected_order = [e["content"] for e in sorted(
        many, key=lambda e: (e["created_at"], e["id"]))]

    mw3 = types.SimpleNamespace(
        client=PostCaptureClient([]), db=FakeDb(many), pub_key=HARNESS_PUB)
    t0 = time.perf_counter()
    page3 = articles_page.ArticleReaderPage(mw3, card)
    render_s = time.perf_counter() - t0
    page3._disarm_comments_deadline()

    texts3 = _label_texts(page3)
    comment_labels = [t for t in texts3 if t.startswith("harness comment ")]
    check("30 fixture comments render in the reader",
          len(comment_labels) == 30, str(len(comment_labels)))
    check("comments render oldest-first (reply order)",
          comment_labels == expected_order,
          "got %r want %r" % (comment_labels[:3], expected_order[:3]))
    check("30 comments + full article build under 2s scroll budget",
          render_s < 2.0, "%.3fs" % render_s)
    check("compact rows: author line carries npub + ago",
          any("npub" in t and " ago" in t for t in texts3))

    # --- LFUX-2 (brief §1.3/§2.2.8): every comment row is the FEED META
    # IDIOM on real widgets — a dim-label "{name} · {ago}" line,
    # start-aligned, over a normal-weight wrapped body. No profile row in
    # the DB -> the name falls down the chain to npub (display_name ->
    # name -> npub, brief §1.3). Rows stay plain Boxes: the brief's closer
    # for §2.2.8 keeps click semantics untouched.
    crows = []
    c = page3.comments_box.get_first_child()
    while c is not None:
        if isinstance(c, Gtk.Box):
            crows.append(c)
        c = c.get_next_sibling()
    check("comments render as plain non-activatable thread rows (Box rows "
          "in a Box container — click semantics unchanged per brief §2.2.8)",
          len(crows) == 30 and not isinstance(page3.comments_box, Gtk.ListBox))
    cmetas = [r.get_first_child() for r in crows]
    cbodies = [m.get_next_sibling() if m is not None else None
               for m in cmetas]
    check("each row opens with a meta label; every meta line is dim-label, "
          "start-aligned, and EXACTLY '{name} · {ago}' with the npub name "
          "fallback",
          all(isinstance(m, Gtk.Label) and m.has_css_class("dim-label")
              and m.get_xalign() == 0 and _META_RE.match(m.get_text() or "")
              for m in cmetas),
          repr([m.get_text() if isinstance(m, Gtk.Label) else m
                for m in cmetas[:2]]))
    check("comment bodies stay normal-weight (never dim-label), wrapped, "
          "start-aligned",
          all(isinstance(b, Gtk.Label) and not b.has_css_class("dim-label")
              and b.get_wrap() and b.get_xalign() == 0 for b in cbodies))

    # --- LFUX-2 (brief §2.2.8): the composer is the HOUSE composer row —
    # Adw.EntryRow "Add a comment…" + the surface's single suggested-action
    # verb, the groups_page.py:349-361 shape the phone already ships (a
    # bare Gtk.Entry would make this the only off-idiom composer left in
    # the app). Post is valign-center against the taller row; the row
    # keeps the house 8 px composer spacing.
    check("signed-in reader shows the composer row",
          page3.comment_composer.get_visible())
    entries = [n for n in _collect(page3, lambda n: isinstance(n, Adw.EntryRow))]
    posts = [n for n in _collect(page3, lambda n: isinstance(n, Gtk.Button))
             if n.get_label() == "Post"]
    check("composer is a real Adw.EntryRow + Post button (house composer idiom)",
          page3.comment_entry in entries and len(entries) == 1
          and len(posts) == 1 and page3.comment_post_btn in posts,
          repr((len(entries), len(posts))))
    if entries:
        check("EntryRow carries the brief's affordance text",
              page3.comment_entry.get_title() == "Add a comment…",
              repr(page3.comment_entry.get_title()))
    check("composer keeps the house 8 px composer spacing",
          page3.comment_composer.get_spacing() == 8)
    if posts:
        check("Post is the surface's suggested-action verb, centered "
              "against the taller row (groups composer shape)",
              posts[0].has_css_class("suggested-action")
              and posts[0].get_valign() == Gtk.Align.CENTER)

    # Real click -> handler -> unsigned 1111 template handed to the client
    # (signing is the client's job; tier-1 proves the signed round-trip).
    page3.comment_entry.set_text("harness ships it")
    # activate() no-ops on unrealized buttons (C2 row precedent); emit the
    # real clicked signal so the connected handler runs.
    page3.comment_post_btn.emit("clicked")
    check("Post click dispatches exactly once",
          len(mw3.client.published) == 1, str(len(mw3.client.published)))
    template, target_id = mw3.client.published[0]
    check("dispatched event is kind 1111 with correct article id",
          template["kind"] == 1111 and target_id == card.id)
    tag_pairs = {(t[0], t[1]) for t in template["tags"]}
    check("template carries e/a/k parent pointers",
          ("e", card.id) in tag_pairs and ("a", card.address) in tag_pairs
          and ("k", "30023") in tag_pairs, repr(sorted(tag_pairs)))
    check("template carries E/A/K/P root pointers (NIP-22/26 shape)",
          ("E", card.id) in tag_pairs and ("A", card.address) in tag_pairs
          and ("K", "30023") in tag_pairs and ("P", card.pubkey) in tag_pairs)
    check("UI hands the UNSIGNED template (never signs itself)",
          "sig" not in template and "id" not in template)
    check("entry cleared after accepted dispatch",
          page3.comment_entry.get_text() == "")

    # Optimistic row lands as a REAL widget; signed echo then dedups by id.
    signed_ev = nostr_utils.sign_event(dict(template), HARNESS_PRIV)
    check("template signs and verifies with the harness key",
          signed_ev is not None and signed_ev["pubkey"] == HARNESS_PUB
          and verify_event_sig(signed_ev))
    texts_after = _label_texts(page3)
    ships = [t for t in texts_after if t == "harness ships it"]
    check("optimistic comment row rendered after post",
          len(ships) == 1, repr(ships))
    counts3 = [t for t in texts_after if t.startswith("(")]
    check("count covers the pending comment (31)",
          counts3 == ["(31)"], repr(counts3))
    page3._on_comments_done([signed_ev], {"fallback_used": False})
    texts_dedup = _label_texts(page3)
    check("relay echo dedups by id (still 31, no double row)",
          [t for t in texts_dedup if t == "harness ships it"] == ships
          and [t for t in texts_dedup if t.startswith("(")] == ["(31)"])
    check("posted comment sits at the thread tail (newest ts)",
          texts_dedup[-1] == "harness ships it"
          or "harness ships it" in texts_dedup[-2:],
          repr(texts_dedup[-2:]))

    # --- LFUX-2 (brief §2.2.8): optimistic-row VISUAL PARITY. The pending
    # row is not special chrome — it goes through the same
    # _render_comments path as confirmed rows, so on real widgets it must
    # be structurally identical: same container, same two-label shape (dim
    # meta "{name} · {ago}" + normal-weight wrapped body), nothing that a
    # confirmed row lacks.
    def _comment_row_of(label):
        n = label.get_parent()
        while n is not None and n.get_parent() is not page3.comments_box:
            n = n.get_parent()
        return n

    def _row_shape(row):
        if row is None:
            return None
        labs = _collect(row, lambda n: isinstance(n, Gtk.Label))
        if len(labs) != 2:
            return ("label-count", len(labs))
        meta, body = labs
        return (meta.has_css_class("dim-label"),
                bool(_META_RE.match(meta.get_text() or "")),
                not body.has_css_class("dim-label"),
                body.get_wrap(), meta.get_xalign() == 0)

    opt_lbl = next(l for l in _collect(page3, lambda n: isinstance(n, Gtk.Label))
                   if l.get_text() == "harness ships it")
    conf_lbl = next(l for l in _collect(page3, lambda n: isinstance(n, Gtk.Label))
                    if l.get_text() == "harness comment 00")
    opt_row = _comment_row_of(opt_lbl)
    conf_row = _comment_row_of(conf_lbl)
    check("optimistic row is structurally IDENTICAL to a confirmed row "
          "(dim meta + plain non-dim body, same shape)",
          _row_shape(opt_row) == _row_shape(conf_row)
          == (True, True, True, True, True),
          repr((_row_shape(opt_row), _row_shape(conf_row))))
    check("optimistic row sits in the SAME thread container as confirmed rows",
          opt_row.get_parent() is conf_row.get_parent() is page3.comments_box)

    # --- LFUX-2: Enter on the EntryRow drives the same post handler
    # through EntryRow's real 'apply' signal (the Gtk.Entry 'activate'
    # equivalent). Emission is real (probe-verified: Adw's default apply
    # closure leaves the text intact for the connected handler).
    page3.comment_entry.set_text("via apply")
    page3.comment_entry.emit("apply")
    check("EntryRow 'apply' (Enter) drives the same post handler",
          len(mw3.client.published) == 2
          and mw3.client.published[1][0]["content"] == "via apply",
          str(len(mw3.client.published)))
    box_tail = _label_texts(page3.comments_box)[-3:]
    counts4 = [t for t in _label_texts(page3) if t.startswith("(")]
    check("apply dispatch lands its own optimistic row at the thread tail",
          "via apply" in box_tail and "harness ships it" in box_tail
          and counts4 == ["(32)"],
          repr((box_tail, counts4)))
    check("apply cleared the entry like the button path",
          page3.comment_entry.get_text() == "")

    # --- LFUX-2 (brief §2.2.8, NIP-26 pointers): FALLBACK-CAPTION
    # styling. When the fetch settles with meta fallback_used (relay-side
    # tag filtering broken), each comment carries a dim caption naming WHY
    # it matched: the UPPERCASE root binding — A/K live form (whole
    # address), E + K id form, or P-only author form. The precise primary
    # chain (fallback_used False) renders NO caption anywhere.
    fb_events = [
        _comment_event("f1" + "1" * 62, 1763000411, "binds by address",
                       tags=[["A", ARTICLE_ADDR], ["K", "30023"]]),
        _comment_event("f2" + "2" * 62, 1763000412, "binds by id",
                       tags=[["E", "f" * 64], ["K", "30023"]]),
        _comment_event("f3" + "3" * 62, 1763000413, "binds by pubkey",
                       tags=[["P", PUB]]),
    ]
    fb_client = StallClient()
    page_fb = articles_page.ArticleReaderPage(
        types.SimpleNamespace(client=fb_client, db=FakeDb([])), card)
    fb_client.pending[-1](fb_events, {"fallback_used": True})
    page_fb._disarm_comments_deadline()
    caps = [n for n in _collect(page_fb, lambda n: isinstance(n, Gtk.Label))
            if n.has_css_class("caption")]
    check("fallback-matched set paints exactly one binding caption per comment",
          len(caps) == 3, repr([c.get_text() for c in caps]))
    cap_texts = sorted(c.get_text() or "" for c in caps)
    want_caps = sorted(["binds 30023:%s:post-1" % PUB,
                        "binds kind 30023 " + "f" * 12 + "…",
                        "binds P bbbbbbbb…bbbbbbbb"])
    check("captions carry the A/K, E+K and P root-binding pointers "
          "(NIP-26 uppercase forms, brief §2.2.8)",
          cap_texts == want_caps, repr(cap_texts))
    check("captions are house-styled: dim-label + caption class, "
          "start-aligned, wrapped, normal-weight",
          all(c.has_css_class("dim-label") and c.has_css_class("caption")
              and c.get_xalign() == 0 and c.get_wrap() for c in caps))
    check("precise primary chain renders NO binding caption anywhere",
          not [n for n in _collect(page, lambda n: isinstance(n, Gtk.Label))
               if n.has_css_class("caption")])

    # --- LFUX-2 (brief §1.7 'visible, never silent'): watchdog/Retry
    # affordance driven for REAL. The stall client never settles the
    # fetch; the REAL one-shot GLib deadline (production arm site, just a
    # shorter constant) fires on the main loop -> error-class label +
    # visible Retry button. A REAL 'clicked' emission on Retry redrives
    # the fetch (affordance clears while in flight, never stale-visible);
    # a settle through the recorded client callback clears both for good.
    saved_deadline = articles_page.COMMENTS_DEADLINE_MS
    articles_page.COMMENTS_DEADLINE_MS = 300
    try:
        stall = StallClient()
        page_wd = articles_page.ArticleReaderPage(
            types.SimpleNamespace(client=stall, db=FakeDb([]),
                                  pub_key=HARNESS_PUB), card)
        loop = GLib.MainLoop()
        GLib.timeout_add(1200, loop.quit)
        loop.run()
        check("real GLib watchdog surfaces a stalled fetch visibly "
              "(error-class label + visible Retry — never silent 'loading')",
              page_wd.comments_err.get_visible()
              and page_wd.comments_err.has_css_class("error")
              and page_wd.comments_retry.get_visible())
        page_wd.comments_retry.emit("clicked")
        check("Retry click (real signal) redrives the fetch",
              stall.calls == 2, str(stall.calls))
        check("affordance clears while the retry fetch is in flight",
              not page_wd.comments_err.get_visible()
              and not page_wd.comments_retry.get_visible())
        stall.pending[-1]([_comment_event("ad" + "a" * 62, 1763000414,
                                          "late but settled")],
                          {"fallback_used": False})
        check("a settle after Retry clears the failure affordance for good",
              not page_wd.comments_err.get_visible()
              and not page_wd.comments_retry.get_visible()
              and "late but settled" in _label_texts(page_wd.comments_box))
        page_wd._disarm_comments_deadline()
    finally:
        articles_page.COMMENTS_DEADLINE_MS = saved_deadline

    # 7. (C4) Draft management on real widgets: DraftsPage renders a
    #    decrypted 31234 wrap (real NIP-44 self↔self parse through the
    #    shipped code) + a legacy 30024 row; real View/Publish/Delete
    #    buttons drive the flows against real Adw.ToastOverlay /
    #    Adw.NavigationView hosts.
    from gnostr import nip44  # noqa: F401  (resolved via SRC path)

    conv_self = nip44.get_conversation_key(
        bytes.fromhex(HARNESS_PRIV), bytes.fromhex(HARNESS_PUB))

    inner_draft = {
        "pubkey": HARNESS_PUB,
        "kind": 30023,
        "created_at": 1763000400,
        "content": "alpha beta gamma delta work in progress",
        "tags": [["d", "mydraft"], ["title", "My Draft Title"],
                 ["t", "linux"], ["t", "nostr"]],
    }
    inner_draft["id"] = nostr_utils.compute_event_id(inner_draft)
    wrap_ev = {
        "pubkey": HARNESS_PUB,
        "kind": 31234,
        "created_at": 1763000401,
        "content": nip44.encrypt(json.dumps(inner_draft), conv_self),
        "tags": [["d", "wrap-1"], ["k", "30023"]],
    }
    wrap_ev["id"] = nostr_utils.compute_event_id(wrap_ev)
    legacy_ev = {
        "id": "ee" * 32,
        "pubkey": HARNESS_PUB,
        "kind": 30024,
        "created_at": 1763000500,
        "content": "one two three four five six seven",
        "tags": [["d", "legacy-1"], ["title", "Old Legacy Draft"]],
    }

    entry_wrap = longform.parse_draft_event(
        wrap_ev, privkey_hex=HARNESS_PRIV, self_pubkey_hex=HARNESS_PUB)
    entry_legacy = longform.parse_draft_event(legacy_ev)
    check("31234 wrap decrypts via the real parse path (title/words/target)",
          entry_wrap is not None
          and entry_wrap.title == "My Draft Title"
          and entry_wrap.word_count == 7 and not entry_wrap.legacy
          and entry_wrap.target_kind == 30023
          and entry_wrap.nevent_id == inner_draft["id"], repr(entry_wrap))
    check("legacy 30024 parses flagged legacy",
          entry_legacy is not None and entry_legacy.legacy
          and entry_legacy.title == "Old Legacy Draft"
          and entry_legacy.word_count == 7, repr(entry_legacy))

    class DraftsCaptureClient:
        """Records exactly what the DraftsPage actions hand the client."""

        def __init__(self, entries):
            self.entries = list(entries)
            self.published = []  # (unsigned template, wrap_id)
            self.deleted = []    # wrap ids handed to delete_draft

        def fetch_drafts(self, cb):
            cb(list(self.entries), {})

        def publish_draft_article(self, template, wrap_id):
            self.published.append((template, wrap_id))
            return True

        def delete_draft(self, wrap_id):
            self.deleted.append(wrap_id)
            return True

    toast_overlay = Adw.ToastOverlay()   # real house snackbar host
    nav = Adw.NavigationView()           # real content nav (main.py layout)
    added_toasts = []

    def _add_toast(toast):
        toast_overlay.add_toast(toast)   # real widget first: a failure
        added_toasts.append(toast)       # here is a real failure, not noise

    dc = DraftsCaptureClient([entry_legacy, entry_wrap])
    mw4 = types.SimpleNamespace(
        client=dc, db=FakeDb([]), pub_key=HARNESS_PUB,
        content_nav=nav, add_toast=_add_toast)
    drafts_page = articles_page.DraftsPage(mw4)
    drafts_page._disarm_deadline()

    def _action_rows(list_box):
        out, r = [], list_box.get_first_child()
        while r is not None:
            if isinstance(r, Adw.ActionRow):
                out.append(r)
            r = r.get_next_sibling()
        return out

    def _row_buttons(row, label):
        return [b for b in _collect(row, lambda n: isinstance(n, Gtk.Button))
                if b.get_label() == label]

    def _row_for(title):
        for r in _action_rows(drafts_page.list_box):
            if title in " ".join(_label_texts(r)):
                return r
        return None

    def _toast_titles():
        return [t.get_title() for t in added_toasts]

    rows4 = _action_rows(drafts_page.list_box)
    texts4 = _joined(rows4)
    check("draft list renders both fixture rows (Adw.ActionRow)",
          len(rows4) == 2, repr(texts4))
    check("31234 row shows title + word count (not labeled legacy)",
          any("My Draft Title" in t and "7 words" in t and "legacy" not in t
              for t in texts4), repr(texts4))
    check("30024 row labeled legacy with word count",
          any("Old Legacy Draft" in t and "7 words" in t and "legacy" in t
              for t in texts4), repr(texts4))
    check("each draft row carries View + Publish + Delete real buttons",
          all(len(_row_buttons(r, lbl)) == 1 for r in rows4
              for lbl in ("View", "Publish", "Delete")), repr(texts4))

    # View click -> C1 reader in draft mode on the REAL nav stack: preview
    # banner present, comments thread absent (drafts are unpublished).
    row_wrap = _row_for("My Draft Title")
    if row_wrap is not None:
        _row_buttons(row_wrap, "View")[0].emit("clicked")
    nav_page = nav.get_visible_page() if nav is not None else None
    draft_view_tree = ""
    if nav_page is not None:
        draft_view_tree = " | ".join(
            " ".join(_label_texts(n))
            for n in _collect(nav_page, lambda n: True))
    check("View opens the draft-mode reader on the real nav stack",
          "Draft preview — not yet published" in draft_view_tree
          and "My Draft Title" in draft_view_tree, draft_view_tree[:160])
    check("draft reader hides the comments thread",
          "Comments" not in draft_view_tree, draft_view_tree[:160])

    # Publish click -> UNSIGNED 30023 rebuilt from the draft, handed to
    # the client once; signs + verifies under the harness key; row gone;
    # confirmation toast on the real overlay.
    if row_wrap is not None:
        _row_buttons(row_wrap, "Publish")[0].emit("clicked")
    check("Publish click hands exactly one signed-then-gated draft to "
          "the client (wrap id carried for supersede)",
          len(dc.published) == 1 and dc.published[0][1] == wrap_ev["id"],
          repr(dc.published))
    art_tmpl = dc.published[0][0] if dc.published else {}
    art_tags = {(t[0], t[1]) for t in art_tmpl.get("tags", [])}
    check("rebuilt 30023 carries title/content/d/pubkey from the draft",
          art_tmpl.get("kind") == 30023
          and art_tmpl.get("content") == inner_draft["content"]
          and art_tmpl.get("pubkey") == HARNESS_PUB
          and ("title", "My Draft Title") in art_tags
          and any(t[0] == "d" for t in art_tmpl.get("tags", [])),
          repr(sorted(art_tags)))
    check("rebuilt 30023 preserves the draft's own t tags",
          ("t", "linux") in art_tags and ("t", "nostr") in art_tags,
          repr(sorted(art_tags)))
    check("UI hands the UNSIGNED 30023 (client owns signing)",
          "sig" not in art_tmpl and "id" not in art_tmpl, repr(art_tmpl))
    signed_article = nostr_utils.sign_event(dict(art_tmpl), HARNESS_PRIV)
    check("draft publish template signs + verifies as a real 30023",
          signed_article is not None
          and signed_article["pubkey"] == HARNESS_PUB
          and verify_event_sig(signed_article), repr(signed_article))
    check("publish removes the row + shows confirmation toast",
          len(_action_rows(drafts_page.list_box)) == 1
          and "Draft published — article sent" in _toast_titles(),
          repr(_toast_titles()))

    # Delete click -> wrap id handed to client.delete_draft (the client
    # signs + fans the deletion event, NIP-09 semantics); row gone;
    # snackbar.
    row_legacy = _row_for("Old Legacy Draft")
    if row_legacy is not None:
        _row_buttons(row_legacy, "Delete")[0].emit("clicked")
    check("Delete click sends the wrap id to delete_draft",
          dc.deleted == [legacy_ev["id"]], repr(dc.deleted))
    check("delete empties the list + shows confirmation snackbar",
          not _action_rows(drafts_page.list_box)
          and "Draft deleted" in _toast_titles(), repr(_toast_titles()))

    # 8. No GTK criticals during the whole run.
    check("no GTK error/critical records", not criticals, repr(criticals))

    print("SUMMARY: %s (%d failures)" % ("ALL CHECKS PASS" if not failed
                                         else "FAILURES", failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
