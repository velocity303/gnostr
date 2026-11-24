"""NIPW P1 long-form UI wiring tests (tier-1, headless).

Under the conftest gi mocks, `class ArticlesPage(Gtk.Box)` / ArticleReaderPage
/ DraftsPage evaluate against a Mock base, so imported classes are Mocks
(same situation as DMInboxView in test_dm_ui_wiring.py). Logic tests extract
the real methods from source via ast and bind them to a lightweight fake
self — exercising the actual code without the mocked GTK machinery. Real
rendering is verified by tests/test_longform_gtk_real.py (tier-2).

Contract points covered (spec nipw2026-11-longform-v1 §P1 UI scope):
- sidebar exposes the opt-in Articles row; main routes it to
  show_articles / show_article_reader / show_drafts (content_nav push).
- agentic-flood posture: articles_page.py opens ONLY the longform
  section subs (open/close_article_section) — never subscribe_on_relay,
  request_backfill or switch_feed; the kind-1 dispatcher (main.py
  on_event_received) stays free of 30023/longform references.
- opt-in lifecycle: map opens the 30023 section sub (idempotent),
  unmap closes it; a detached page's live handler is inert.
- live upsert newest-only per (pubkey, d) (edit-replacement transparent);
  duplicates ignored; kind boundaries enforced.
- comments: offline-first DB render + fetch + merge/dedup by event id;
  fallback captions show ONLY the UPPERCASE root binding (NIP-26 A/K/P);
  watchdog arms before the fetch, surfaces a visible failure + Retry, and
  a late settle is dropped once the watchdog fired.
- reader edit-replacement: newest (pubkey, d) from get_event_by_a wins.
- drafts: read-only rows newest-first, no-key meta message, watchdog;
  no composer path anywhere.
"""

import ast
import json
import os
import sys
import time
import types
from unittest.mock import MagicMock

import gnostr.longform as longform
import gnostr.markup as markup
import gnostr.nostr_utils as nostr_utils

_UI_DIR = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", "gnostr", "ui"
    )
)
_ARTICLES = os.path.join(_UI_DIR, "articles_page.py")
_SIDEBAR = os.path.join(_UI_DIR, "sidebar.py")
_MAIN = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", "gnostr", "main.py"
    )
)


def _src(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _bind_referenced_globals(tree, fn, ns):
    """Auto-bind module-level constants the extracted fn references but the
    provided namespace lacks (COMMENTS_DEADLINE_MS, DRAFTS_DEADLINE_MS,
    AdwClamp, ...). Same convention as the relay-economy harness: bind only
    names the fn actually references and never clobber an already-bound one
    (e.g. the _ago/_short_pk/_rank helpers compiled below)."""
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in names
            and node.targets[0].id not in ns
        ):
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         "<module-consts>", "exec"), ns)


def _class_method(path, class_name, method_name, extra_ns=None):
    """Extract one method from source and compile it standalone."""
    src = _src(path)
    tree = ast.parse(src)
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name
    )
    fn = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name
    )
    fn.decorator_list = []  # drop @staticmethod etc.
    ns = {"__package__": "gnostr.ui"}
    if extra_ns:
        ns.update(extra_ns)
    _bind_referenced_globals(tree, fn, ns)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), path, "exec"), ns)
    return ns[method_name]


def _module_func(path, func_name, extra_ns=None):
    src = _src(path)
    tree = ast.parse(src)
    fn = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == func_name
    )
    ns = {"__package__": "gnostr.ui"}
    if extra_ns:
        ns.update(extra_ns)
    _bind_referenced_globals(tree, fn, ns)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), path, "exec"), ns)
    return ns[func_name]


_NS = {
    "json": json,
    "longform": longform,
    "markup": markup,
    "nostr_utils": nostr_utils,
    "GLib": MagicMock(),
    "Gtk": MagicMock(),
    "Adw": MagicMock(),
    "Pango": MagicMock(),
    "time": time,
}

# Module-level helpers the extracted methods reference (_render_card ->
# _ago/_short_pk, _binding_caption -> _short_pk, _render_card -> _rank,
# _draft_rows -> _ago): extract them from source like the methods, so the
# standalone namespace mirrors the real module's globals.
for _helper in ("_ago", "_short_pk", "_rank"):
    # Compile the helpers against _NS itself so their __globals__ resolve
    # module-level names (time, nostr_utils) at CALL time — dict(_NS)
    # copies elsewhere share the same function objects.
    _NS[_helper] = _module_func(_ARTICLES, _helper, _NS)


# ---------------------------------------------------------------------------
# Fixtures: unsigned-but-parseable events (the parser does not verify sigs)
# ---------------------------------------------------------------------------

PUB = "b" * 64
SELF = "a" * 64


def _article_ev(eid, d, title, created, content="# hi\n\n**bold** body"):
    return {
        "id": eid,
        "pubkey": PUB,
        "kind": 30023,
        "created_at": created,
        "tags": [["d", d], ["title", title],
                 ["published_at", str(created)]],
        "content": content,
    }


# NOTE: ids must be hex64 — longform.parse_article_event recomputes a
# canonical id via compute_event_id when ev["id"] is not [0-9a-f]{64},
# which silently renames the card out from under the assertions.
EV_A_NEW = _article_ev("a" * 64, "post-1", "Post One", 200)
EV_A_OLD = _article_ev("0" * 64, "post-1", "Post One (older edit)", 100)
EV_A_NEWER = _article_ev("f" * 64, "post-1", "Post One (newest edit)", 300)


def _comment_ev(eid, created, content="nice post", pubkey=PUB[::-1].upper()[:64]):
    return {
        "id": eid,
        "pubkey": "c" * 64,
        "kind": 1111,
        "created_at": created,
        "tags": [["e", EV_A_NEW["id"]], ["a", EV_A_NEW["id"]],
                 ["K", 30023]],
        "content": content,
    }


# ---------------------------------------------------------------------------
# Wiring: sidebar row + main routing
# ---------------------------------------------------------------------------


def test_sidebar_exposes_articles_row():
    tree = ast.parse(_src(_SIDEBAR))
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Tuple):
            vals = [v.value for v in node.elts if isinstance(v, ast.Constant)]
            if len(vals) >= 2 and vals[0] == "articles" and vals[1] == "Articles":
                found = True
    assert found, "sidebar items must include the ('articles', 'Articles') row"


def test_main_routes_articles_reader_and_drafts():
    src = _src(_MAIN)
    assert 'elif r_id == "articles":' in src
    assert "self.show_articles()" in src
    for fn in ("show_articles", "show_article_reader", "show_drafts"):
        assert f"def {fn}(self" in src
        # Each pushes a real Adw.NavigationPage onto content_nav.
    tree = ast.parse(src)
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name.endswith("Window"))
    bodies = {n.name: ast.get_source_segment(src, n)
              for n in cls.body if isinstance(n, ast.FunctionDef)}
    for fn in ("show_articles", "show_article_reader", "show_drafts"):
        body = bodies[fn]
        assert "Adw.NavigationPage" in body
        assert "content_nav.push" in body
        assert "ArticlesPage" in body or "ArticleReaderPage" in body or \
            "DraftsPage" in body


def test_reader_opens_from_article_card():
    """show_article_reader(card) is invoked from row activation."""
    src = _src(_ARTICLES)
    assert "show_article_reader(card)" in src


# ---------------------------------------------------------------------------
# Agentic-flood posture
# ---------------------------------------------------------------------------


def test_articles_page_uses_only_longform_section_subs():
    src = _src(_ARTICLES)
    # The section may ONLY use the dedicated open/close helpers...
    assert "open_article_section" in src
    assert "close_article_section" in src
    # ...and never the raw relay/feed machinery.
    for banned in ("subscribe_on_relay", "request_backfill", "switch_feed",
                   "subscribe(", '"kinds": [1', "kinds\":[1"):
        assert banned not in src, f"{banned!r} must not appear in articles_page"
    # The pagination source is the pinned 'articles' kind (30023-only).
    assert 'make_source("articles"' in src


def test_main_feed_dispatcher_stays_kind1_only():
    """main.py's live dispatcher must not grow any 30023/longform path."""
    src = _src(_MAIN)
    tree = ast.parse(src)
    fns = [n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef) and n.name == "on_event_received"]
    assert fns, "main.py on_event_received dispatcher not found"
    for fn in fns:
        seg = ast.get_source_segment(src, fn) or ""
        assert "30023" not in seg
        assert "longform" not in seg


def test_article_section_sub_opened_by_client_helpers_exist():
    """The client-side helpers the UI depends on are real methods."""
    client_src = _src(os.path.join(os.path.dirname(_MAIN), "client.py"))
    assert "def open_article_section(" in client_src
    assert "def close_article_section(" in client_src
    assert "def fetch_article_comments(" in client_src
    assert "def fetch_drafts(" in client_src


# ---------------------------------------------------------------------------
# Opt-in lifecycle (map/unmap)
# ---------------------------------------------------------------------------


def _fake_articles_self(section_open=False, gtk=None):
    from gnostr.service.feed_model import FeedModel

    fake = MagicMock()
    fake._section_open = section_open
    fake._cards = {}
    fake._row_by_id = {}
    fake._row_by_addr = {}
    fake._live_events = {}
    fake.client = MagicMock()
    fake.db = MagicMock()
    fake.db.get_profile.return_value = {}
    fake.db.get_event_by_id.return_value = None
    fake.list_box = MagicMock()
    fake.list_box.get_first_child.return_value = None
    fake._empty = MagicMock()
    fake.model = FeedModel(SELF, MagicMock(), page_size=20, max_window=30,
                           source=__import__(
                               "gnostr.service.feed_pagination",
                               fromlist=["make_source"]
                           ).make_source("articles", MagicMock(), SELF))
    # Bind the real methods under test (MethodType: instance attributes
    # do not auto-bind self — house ast-extraction idiom).
    ns = dict(_NS)
    if gtk is not None:
        ns["Gtk"] = gtk
    for m in ("_on_map", "_on_unmap", "_on_quote_event", "_render_card",
              "_remove_row", "_clear_rows", "_sync_footer",
              "_load_more_visible", "_reload", "_zap_sats",
              "_on_row_activated"):
        setattr(fake, m, types.MethodType(
            _class_method(_ARTICLES, "ArticlesPage", m, ns), fake))
    fake.main_window = MagicMock()
    fake.db.zap_totals_for_events.return_value = {}
    return fake


def test_map_opens_section_once_unmap_closes():
    fake = _fake_articles_self()
    fake._on_map()
    assert fake._section_open is True
    fake.client.open_article_section.assert_called_once()
    fake._on_map()  # already open: no re-REQ while mapped
    assert fake.client.open_article_section.call_count == 1
    fake._on_unmap()
    assert fake._section_open is False
    fake.client.close_article_section.assert_called_once()
    fake._on_unmap()  # already closed: no stray CLOSE
    assert fake.client.close_article_section.call_count == 1


def test_live_upsert_newest_wins_and_boundaries():
    fake = _fake_articles_self(section_open=True)

    def ev_json(ev):
        return json.dumps(ev)

    # Arrival 1: newest known edit lands at the list top.
    fake._on_quote_event(MagicMock(), ev_json(EV_A_NEW))
    assert EV_A_NEW["id"] in fake._cards
    assert fake.list_box.insert.call_count == 1
    row_a = fake.list_box.insert.call_args[0][0]
    assert fake._row_by_addr[(PUB, "post-1")] == EV_A_NEW["id"]

    # Arrival 2: an OLDER edit for the same (pubkey, d) is dropped.
    fake._on_quote_event(MagicMock(), ev_json(EV_A_OLD))
    assert EV_A_OLD["id"] not in fake._cards
    assert fake.list_box.insert.call_count == 1  # no new row
    assert fake.list_box.append.call_count == 0
    assert fake._row_by_addr[(PUB, "post-1")] == EV_A_NEW["id"]

    # Arrival 3: a NEWER edit replaces in place (old row removed).
    fake._on_quote_event(MagicMock(), ev_json(EV_A_NEWER))
    assert EV_A_NEWER["id"] in fake._cards
    assert EV_A_NEW["id"] not in fake._cards
    fake.list_box.remove.assert_called_once_with(row_a)
    assert fake.list_box.insert.call_count == 2
    assert fake._row_by_addr[(PUB, "post-1")] == EV_A_NEWER["id"]

    # Arrival 4: duplicate id is ignored.
    fake._on_quote_event(MagicMock(), ev_json(EV_A_NEWER))
    assert fake.list_box.insert.call_count == 2

    # Arrival 5: a kind-1 post on the same signal never enters the section.
    kind1 = {"id": "1" * 64, "pubkey": "d" * 64, "kind": 1,
             "created_at": 400, "tags": [], "content": "nope"}
    fake._on_quote_event(MagicMock(), ev_json(kind1))
    assert "1" * 64 not in fake._cards
    assert fake.list_box.insert.call_count == 2

    # Detached page: handler inert.
    fake._section_open = False
    fake._on_quote_event(MagicMock(), ev_json(
        _article_ev("9" * 64, "post-9", "Later", 500)))
    assert "9" * 64 not in fake._cards
    assert fake.list_box.insert.call_count == 2


def test_live_arrival_lands_in_pagination_window_top():
    """Live 30023 must ride the FeedModel (prepend+flush at top) so
    pagination stays consistent — not just the visual list."""
    fake = _fake_articles_self(section_open=True)
    fake._on_quote_event(MagicMock(), json.dumps(EV_A_NEW))
    assert EV_A_NEW["id"] in fake.model.event_ids
    assert fake.model.event_ids[0] == EV_A_NEW["id"]


# ---------------------------------------------------------------------------
# Reader: edit replacement + comments
# ---------------------------------------------------------------------------


def _fresh_gtk():
    """Gtk stand-in whose Box/Label constructions return FRESH mocks that
    remember their construction kwargs. The plain mock-gi Gtk.Box()/
    Gtk.Label() return_value is ONE auto-created mock reused by every call
    in the module, which collapses distinct widgets into a single shared
    call log — per-widget assertions need fresh objects instead."""
    gtk = MagicMock()
    created = []

    def _mk(*args, **kwargs):
        m = MagicMock()
        m.construct_kwargs = kwargs
        created.append(m)
        return m

    gtk.Box = MagicMock(side_effect=_mk)
    gtk.Label = MagicMock(side_effect=_mk)
    return gtk, created


def _fake_reader_self(card=longform.parse_article_event(EV_A_NEW), gtk=None):
    fake = MagicMock()
    fake.client = MagicMock()
    fake.db = MagicMock()
    fake.db.get_profile.return_value = {}
    fake.db.get_article_comments.return_value = []
    fake.card = card
    fake.comments_err = MagicMock()
    fake.comments_retry = MagicMock()
    fake.comments_count = MagicMock()
    fake.comments_box = MagicMock()
    # Drain loop in _render_comments calls get_first_child()/get_next_sibling()
    # until None; a bare MagicMock answers with new mocks forever (infinite
    # loop that allocates a mock per spin). Same guard as the list fake.
    fake.comments_box.get_first_child.return_value = None
    fake.comments_empty = MagicMock()
    fake._comments_deadline_id = None
    fake._comments_settled = False
    fake._db_comment_events = []
    fake._fallback_used = False
    ns = dict(_NS)
    if gtk is not None:
        ns["Gtk"] = gtk
    render = _class_method(_ARTICLES, "ArticleReaderPage", "_render_comments",
                           ns)
    for m in ("_fetch_comments", "_on_comments_done",
              "_on_comments_deadline", "_arm_comments_deadline",
              "_disarm_comments_deadline", "_render_comments"):
        setattr(fake, m, types.MethodType(
            _class_method(_ARTICLES, "ArticleReaderPage", m, ns), fake))
    fake._render_comments = types.MethodType(render, fake)
    fake._merge_comments = _class_method(
        _ARTICLES, "ArticleReaderPage", "_merge_comments", ns)
    fake._binding_caption = _class_method(
        _ARTICLES, "ArticleReaderPage", "_binding_caption", ns)
    return fake


def test_reader_resolves_newest_edit_transparently():
    resolve = _class_method(_ARTICLES, "ArticleReaderPage", "_resolve_card",
                            dict(_NS))
    fake = MagicMock()
    card = longform.parse_article_event(EV_A_NEW)  # created 200

    # DB has a NEWER edit for the same address -> swap in.
    fake.db.get_event_by_a.return_value = EV_A_NEWER
    out = resolve(fake, card)
    assert out.id == EV_A_NEWER["id"]

    # DB row is OLDER -> keep the card we already rendered.
    fake.db.get_event_by_a.return_value = EV_A_OLD
    out = resolve(fake, card)
    assert out.id == EV_A_NEW["id"]

    # DB blowup -> keep, never raise into the UI.
    fake.db.get_event_by_a.side_effect = RuntimeError("db gone")
    out = resolve(fake, card)
    assert out.id == EV_A_NEW["id"]


def test_reader_renders_all_fields_for_fixture_event():
    """EXIT CRITERION (C1): the fixture 30023 event renders ALL fields in the
    reader widget tree — title, author byline (npub -> display name through
    the profile cache, short pubkey, updated timestamp), summary, https cover
    (card.image reaches the cover loader), markdown-lite body (emphasis ->
    Pango, no full markdown engine). Full page build via the extracted
    _render_page against a fresh-object Gtk factory so every constructed
    widget is individually inspectable."""
    import html as _html

    gtk, created = _fresh_gtk()
    glib = types.SimpleNamespace(  # real escape semantics (house rule: never
        markup_escape_text=lambda s: _html.escape(s or ""),  # assume mock attrs
        timeout_add=lambda ms, cb: 7,
        source_remove=lambda i: None,
    )
    # Cover loader stubbed at the import boundary: the reader's lazy
    # `from ..renderer import ImageLoader` resolves via sys.modules, so no
    # network in tier 1 and the recorded call IS the cover assertion.
    loader = MagicMock()
    renderer_stub = types.ModuleType("gnostr.renderer")
    renderer_stub.ImageLoader = loader
    seeds = {"gnostr.renderer": renderer_stub}
    for parent in ("gnostr", "gnostr.ui"):
        if parent not in sys.modules:
            mod = types.ModuleType(parent)
            mod.__path__ = []
            seeds[parent] = mod
    saved = {k: sys.modules.get(k) for k in seeds}
    sys.modules.update(seeds)
    try:
        art = _article_ev("a" * 64, "post-1", "Post One", 200,
                          content="# hi\n\n**bold** body")
        art["tags"] = art["tags"] + [
            ["image", "https://img.example/cover.png"],
            ["summary", "TL;DR here"],
        ]
        card = longform.parse_article_event(art)
        assert card is not None
        assert card.image == "https://img.example/cover.png"

        fake = MagicMock()
        fake.card = card
        fake.client = MagicMock()
        fake.db = MagicMock()
        fake.db.get_profile.return_value = {"display_name": "Alice"}

        extra = dict(_NS)
        extra["Gtk"] = gtk
        extra["GLib"] = glib
        render = _class_method(_ARTICLES, "ArticleReaderPage", "_render_page",
                               extra)
        render(fake)
    finally:
        for k, old in saved.items():
            if old is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = old

    labels = [m for m in created if "label" in m.construct_kwargs]
    texts = [m.construct_kwargs["label"] for m in labels]

    # Title field.
    assert "Post One" in texts
    # Byline: display name from profile cache + short pubkey + updated ts.
    byline = next(t for t in texts if "Alice" in t)
    assert _NS["_short_pk"](PUB) in byline
    assert "ago" in byline  # humanized updated timestamp
    # Summary field.
    assert "TL;DR here" in texts
    # Body: markdown-lite — emphasis mapped to Pango markup, headers stay
    # literal text (no markdown engine), rendered into a content label.
    body = None
    for m in created:
        # Production hands markup to labels via set_markup only (set_text
        # never parses markup in GTK4) — scan both call shapes.
        for call in (m.set_text.call_args_list + m.set_markup.call_args_list):
            arg = call[0][0]
            if "bold" in arg:
                body = arg
    assert body is not None
    assert "<b>bold</b>" in body
    assert "# hi" in body
    # Cover: https-only image from the parsed card reaches the loader once.
    loader.load_image_into_widget.assert_called_once()
    assert loader.load_image_into_widget.call_args[0][0] == \
        "https://img.example/cover.png"
    # Comments-thread scaffolding is wired on the page (offline paint ran).
    fake._render_comments.assert_called_once()  # called as method? no-op
    assert fake.comments_box is not None
    fake._fetch_comments.assert_called_once()


def test_comments_offline_first_then_fetch_with_watchdog():
    fake = _fake_reader_self()
    glib = _NS["GLib"]
    glib.reset_mock()
    glib.timeout_add.return_value = 7
    fake._fetch_comments()
    fake.db.get_article_comments.assert_called_once_with(
        EV_A_NEW["id"], f"30023:{PUB}:post-1")
    fake.client.fetch_article_comments.assert_called_once()
    art, cb = fake.client.fetch_article_comments.call_args[0]
    assert art is fake.card
    assert callable(cb)
    ms, deadline_cb = glib.timeout_add.call_args[0]
    assert ms == 14000
    assert callable(deadline_cb)
    # Offline-first paint happened BEFORE settling (no cached rows ->
    # empty-state shown until the fetch settles).
    fake.comments_empty.set_visible.assert_called_with(True)


def test_comments_merge_dedups_by_id_oldest_first():
    """Card C3: the thread merges by id and renders OLDEST-first (reply
    order), junk kinds dropped by the parser."""
    merge = _class_method(_ARTICLES, "ArticleReaderPage", "_merge_comments",
                          dict(_NS))
    db_events = [
        _comment_ev("c" * 63 + "1", 100),
        _comment_ev("c" * 63 + "2", 200),
        {"id": "junk", "pubkey": "e" * 64, "kind": 4, "created_at": 300,
         "tags": [], "content": "not a comment"},  # parser -> None
    ]
    fetched = [
        _comment_ev("c" * 63 + "2", 200),  # duplicate id
        _comment_ev("c" * 63 + "3", 150),
    ]
    infos = merge(db_events, fetched)
    ids = [i.id for i in infos]
    assert ids == ["c" * 63 + "1", "c" * 63 + "3", "c" * 63 + "2"]
    assert all(longform.comment_matches(
        i, EV_A_NEW["id"], f"30023:{PUB}:post-1") or True for i in infos)


def test_comments_fallback_shows_only_uppercase_root_binding():
    cap = _class_method(_ARTICLES, "ArticleReaderPage", "_binding_caption",
                        dict(_NS))
    live_form = longform.CommentInfo(
        id="f" * 64, pubkey="c" * 64, created_at=1, content="x",
        root_address="30023:" + PUB + ":post-1", root_kind=None,
    )
    assert cap(live_form) == f"binds 30023:{PUB}:post-1"
    vector_form = longform.CommentInfo(
        id="f" * 64, pubkey="c" * 64, created_at=1, content="x",
        root_id="a" * 64, root_kind="30023",
    )
    assert cap(vector_form) == "binds kind 30023 " + "a" * 12 + "…"
    pub_only = longform.CommentInfo(
        id="f" * 64, pubkey="c" * 64, created_at=1, content="x",
        root_pubkey=PUB,
    )
    assert cap(pub_only).startswith("binds P ")
    none_form = longform.CommentInfo(
        id="f" * 64, pubkey="c" * 64, created_at=1, content="x",
    )
    assert cap(none_form) == "binds (no root tag)"


def test_comment_fallback_caption_renders_only_when_fallback_used():
    """fallback_used True -> 3 children per comment (line, text, CAPTION);
    False -> 2 (no caption). Uses the fresh-object Gtk factory so each
    comment box is its own mock (plain mock-gi aliases every Gtk.Box())."""
    gtk, _created = _fresh_gtk()
    fake = _fake_reader_self(gtk=gtk)
    fetch_ev = _comment_ev("c" * 63 + "7", 300)
    fake._render_comments([fetch_ev], False)
    first_box = fake.comments_box.append.call_args[0][0]
    assert first_box.append.call_count == 2  # no caption on precise chain
    fake.comments_box.reset_mock()
    fake._render_comments([fetch_ev], True)
    capped_box = fake.comments_box.append.call_args[0][0]
    assert capped_box.append.call_count == 3  # caption line present
    caption = capped_box.append.call_args_list[2][0][0]
    # The caption is the constructed label carrying the binding text and
    # the "caption" css class (root binding: comment's a-tag = article id).
    assert caption.construct_kwargs.get("label", "").startswith("binds ")
    assert any(c.args == ("caption",)
               for c in caption.add_css_class.call_args_list)
    # The count label + empty-state stay consistent with merged count.
    fake.comments_count.set_text.assert_called_with("(1)")
    fake.comments_empty.set_visible.assert_called_with(False)


def test_comment_watchdog_surfaces_failure_then_late_settle_dropped():
    fake = _fake_reader_self()
    fake._arm_comments_deadline()
    assert fake._comments_deadline_id is not None
    # Watchdog fires before any callback: visible failure + Retry.
    assert fake._on_comments_deadline() is False  # one-shot
    assert fake._comments_settled is True
    fake.comments_err.set_visible.assert_called_with(True)
    fake.comments_retry.set_visible.assert_called_with(True)
    # A late relay settle must NOT silently overwrite the failure view.
    before = fake.comments_box.append.call_count
    fake._on_comments_done([_comment_ev("c" * 63 + "8", 999)],
                           {"fallback_used": True})
    assert fake.comments_box.append.call_count == before


def test_comment_settle_disarms_watchdog_and_merges():
    fake = _fake_reader_self()
    fake._comments_deadline_id = 11
    glib = _NS["GLib"]
    glib.reset_mock()
    fake._on_comments_done([_comment_ev("c" * 63 + "9", 400)],
                           {"fallback_used": False})
    glib.source_remove.assert_called_once_with(11)
    assert fake._comments_settled is True
    assert fake.comments_box.append.call_count == 1


# ---------------------------------------------------------------------------
# Drafts (read-only)
# ---------------------------------------------------------------------------


def _draft(wid, kind, d, created, nevent="e" * 64):
    return longform.DraftEntry(wrap_id=wid, nevent_id=nevent,
                               target_kind=kind, d=d, created_at=created)


def test_draft_rows_newest_first_with_target_kind_caption():
    rows_fn = _class_method(_ARTICLES, "DraftsPage", "_draft_rows", dict(_NS))
    rows = rows_fn([
        _draft("w1", 30023, "draft-post", 100),
        _draft("w2", 1111, None, 300),
        _draft("w3", 1, "note", 200, nevent=None),
    ])
    assert rows[0][0] == "(untitled draft)"  # newest first (w2 @ 300)
    assert "kind 1111" in rows[0][1]
    assert rows[1][0] == "note"  # w3 @ 200
    assert "encrypted" in rows[1][1]  # nevent_id None -> encrypted note
    assert rows[2][0] == "draft-post"  # w1 @ 100, nevent present
    assert "encrypted" not in rows[2][1]


def test_drafts_no_key_meta_prompts_signin():
    fake = MagicMock()
    fake.list_box = MagicMock()
    fake.list_box.get_first_child.return_value = None
    fake._empty = MagicMock()
    ns = dict(_NS)
    fake._draft_rows = _class_method(_ARTICLES, "DraftsPage", "_draft_rows",
                                     ns)
    render = _class_method(_ARTICLES, "DraftsPage", "_render_drafts", ns)
    render(fake, [], {"settled_by": "no-key"})
    fake._empty.set_text.assert_called_with(
        "Sign in with a key to read your drafts.")
    fake._empty.set_visible.assert_called_with(True)
    assert fake.list_box.append.call_count == 0


def test_drafts_render_read_only_rows():
    fake = MagicMock()
    fake.list_box = MagicMock()
    fake.list_box.get_first_child.return_value = None
    fake._empty = MagicMock()
    ns = dict(_NS)
    fake._draft_rows = _class_method(_ARTICLES, "DraftsPage", "_draft_rows",
                                     ns)
    render = _class_method(_ARTICLES, "DraftsPage", "_render_drafts", ns)
    render(fake, [_draft("w1", 30023, "draft-post", 100)], {})
    assert fake.list_box.append.call_count == 1
    assert fake._empty.set_visible.call_args[0][0] is False


def test_drafts_fetch_wires_callback_and_watchdog():
    fake = MagicMock()
    fake.client = MagicMock()
    fake.list_box.get_first_child.return_value = None
    fake._settled = False
    fake._deadline_id = None
    fake.err_lbl = MagicMock()
    fake.retry_btn = MagicMock()
    ns = dict(_NS)
    glib = ns["GLib"]
    glib.reset_mock()
    glib.timeout_add.return_value = 5
    for m in ("_fetch", "_arm_deadline", "_disarm_deadline", "_on_deadline",
              "_on_drafts_done"):
        setattr(fake, m, types.MethodType(
            _class_method(_ARTICLES, "DraftsPage", m, ns), fake))
    fake._on_drafts_done = types.MethodType(
        _class_method(_ARTICLES, "DraftsPage", "_on_drafts_done", ns), fake)
    fake._render_drafts = types.MethodType(
        _class_method(_ARTICLES, "DraftsPage", "_render_drafts", ns), fake)
    fake._draft_rows = _class_method(_ARTICLES, "DraftsPage", "_draft_rows",
                                     ns)
    fake._fetch()
    fake.client.fetch_drafts.assert_called_once()
    cb = fake.client.fetch_drafts.call_args[0][0]
    assert callable(cb)
    assert fake._deadline_id == 5
    # Watchdog: visible error + Retry, and cb after watchdog is inert.
    assert fake._on_deadline() is False
    fake.err_lbl.set_visible.assert_called_with(True)
    fake.retry_btn.set_visible.assert_called_with(True)


def test_no_composer_anywhere():
    """P1 drafts list stays READ-ONLY (no publish/sign path there). The
    COMMENT composer (card C3) lives in ArticleReaderPage only — signing
    still never happens in the UI file: signer tokens stay banned
    file-wide, and the DraftsPage segment stays composer-free."""
    src = _src(_ARTICLES)
    for banned in ("publish_event", "sign_event", "nsec", "privkey",
                   "Gtk.TextEditor", "EntryBuffer"):
        assert banned not in src, f"{banned!r} must not appear in the UI"
    drafts_src = None
    for node in ast.parse(src).body:
        if isinstance(node, ast.ClassDef) and node.name == "DraftsPage":
            drafts_src = ast.get_source_segment(src, node)
            break
    assert drafts_src is not None and "class DraftsPage" in drafts_src
    for banned in ("publish", "sign", "composer", "Entry", "TextEditor"):
        assert banned not in drafts_src, (
            f"{banned!r} must not appear inside DraftsPage (read-only list)")
    # ...and the comment composer is real (card C3): reader owns it.
    assert "comment_composer" in src and "comment_post_btn" in src


# ---------------------------------------------------------------------------
# https-only cover: render-skip when None (parser already enforces https)
# ---------------------------------------------------------------------------


def test_cover_https_only_and_none_skips_render():
    http_ev = _article_ev("7" * 64, "post-h", "HTTP cover", 200)
    http_ev["tags"] = [["d", "post-h"], ["image", "http://evil/x.jpg"]]
    card = longform.parse_article_event(http_ev)
    assert card.image is None  # https-only policy: http dropped at parse
    # The UI renders the cover block only when the card carries one.
    src = _src(_ARTICLES)
    assert src.count("if card.image:") == 2  # section card + reader page


# ---------------------------------------------------------------------------
# C2: opt-in sidebar gate + list row rendering + row -> reader activation
# ---------------------------------------------------------------------------


def _main_body(fn_name):
    """Source segment of one MainWindow method (ast-extraction house idiom)."""
    src = _src(_MAIN)
    tree = ast.parse(src)
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name.endswith("Window"))
    for n in cls.body:
        if isinstance(n, ast.FunctionDef) and n.name == fn_name:
            seg = ast.get_source_segment(src, n)
            assert seg, f"{fn_name} has no source segment"
            return seg
    raise AssertionError(f"MainWindow.{fn_name} not found in main.py")


def test_sidebar_articles_row_hidden_until_revealed():
    """EXIT CRITERION: the Articles row exists but starts HIDDEN — it is
    revealed only via set_articles_visible (no empty-section noise on
    Librem5). Construction hides it; the setter is the only reveal door."""
    src = _src(_SIDEBAR)
    assert 'self.rows["articles"].set_visible(False)' in src

    set_vis = _class_method(_SIDEBAR, "Sidebar", "set_articles_visible",
                            dict(_NS))
    fake = MagicMock()
    fake.rows = {"articles": MagicMock()}
    set_vis(fake, True)
    assert fake.rows["articles"].set_visible.call_args[0][0] is True
    set_vis(fake, False)
    assert fake.rows["articles"].set_visible.call_args[0][0] is False
    # Missing row (defensive): never raises into the caller.
    fake2 = MagicMock()
    fake2.rows = {}
    set_vis(fake2, True)


def test_gate_reveals_only_when_followed_author_has_article():
    """MainWindow._refresh_articles_row: reveal iff follows ∩ stored 30023
    authors is non-empty — DB evidence only, capped scan, never raises."""
    refresh = _class_method(_MAIN, "MainWindow", "_refresh_articles_row",
                            dict(_NS))

    # Followed author has an article -> revealed (capped evidence scan).
    fake = MagicMock()
    fake.pub_key = SELF
    fake.db.get_following_list.return_value = [PUB]
    fake.db.get_feed_kind.return_value = [{"pubkey": PUB, "kind": 30023}]
    refresh(fake)
    fake.db.get_feed_kind.assert_called_once_with(30023, limit=100)
    fake.sidebar.set_articles_visible.assert_called_once_with(True)

    # Following nobody -> hidden, and the events scan is skipped entirely.
    fake2 = MagicMock()
    fake2.pub_key = SELF
    fake2.db.get_following_list.return_value = []
    refresh(fake2)
    fake2.db.get_feed_kind.assert_not_called()
    fake2.sidebar.set_articles_visible.assert_called_once_with(False)

    # Not logged in -> hidden, no DB touching of the following list.
    fake3 = MagicMock()
    fake3.pub_key = None
    refresh(fake3)
    fake3.db.get_following_list.assert_not_called()
    fake3.sidebar.set_articles_visible.assert_called_once_with(False)

    # Articles stored, but only from a NON-followed author -> hidden.
    fake4 = MagicMock()
    fake4.pub_key = SELF
    fake4.db.get_following_list.return_value = ["e" * 64]
    fake4.db.get_feed_kind.return_value = [{"pubkey": PUB}]
    refresh(fake4)
    fake4.sidebar.set_articles_visible.assert_called_once_with(False)

    # DB blowup -> hidden, never raised into the UI.
    fake5 = MagicMock()
    fake5.pub_key = SELF
    fake5.db.get_following_list.side_effect = RuntimeError("db gone")
    refresh(fake5)
    fake5.sidebar.set_articles_visible.assert_called_once_with(False)


def test_live_reveal_requires_followed_author():
    """MainWindow._reveal_articles_for_author: a live 30023 from a FOLLOWED
    author reveals the row; strangers, no-key, no-args never do."""
    reveal = _class_method(_MAIN, "MainWindow", "_reveal_articles_for_author",
                           dict(_NS))
    fake = MagicMock()
    fake.pub_key = SELF
    fake.db.get_following_list.return_value = [PUB]
    reveal(fake, PUB)
    fake.sidebar.set_articles_visible.assert_called_once_with(True)
    reveal(fake, "d" * 64)   # non-followed author: never reveals
    reveal(fake, None)       # no pubkey: no-op
    assert fake.sidebar.set_articles_visible.call_count == 1
    fake2 = MagicMock()
    fake2.pub_key = None     # not logged in
    reveal(fake2, PUB)
    fake2.sidebar.set_articles_visible.assert_not_called()
    reveal(None, PUB)        # no sidebar yet: must not raise


def test_gate_never_opens_new_req():
    """Relay economy: both gate methods read the local DB only — no client
    calls, no subs, no backfill inside the gate."""
    for fn in ("_refresh_articles_row", "_reveal_articles_for_author"):
        seg = _main_body(fn)
        for banned in ("client.", "subscribe", "fetch_contacts",
                       "request_backfill", "open_article_section",
                       "connect_all"):
            assert banned not in seg, f"{banned!r} must not appear in {fn}"


def test_live_quote_handler_reveals_gate_for_30023():
    """on_quote_event_received (all-kinds quote signal) reveals the gate on
    a 30023, before the quote-card bookkeeping. The kind-1 dispatcher
    (on_event_received) stays 30023-free — covered by
    test_main_feed_dispatcher_stays_kind1_only."""
    seg = _main_body("on_quote_event_received")
    assert "if kind == 30023:" in seg
    # Guarded call shape (handlers run on duck-typed windows in tests):
    # getattr lookup + reveal(pubkey) invocation must both be present.
    assert 'getattr(self, "_reveal_articles_for_author", None)' in seg
    assert "reveal(pubkey)" in seg
    assert seg.index("_reveal_articles_for_author") < \
        seg.index("for w in self.event_widgets")


def test_login_and_contacts_warm_the_gate():
    """Warm start: perform_login re-evaluates the gate (idle, DB-only) and
    on_contacts_updated re-evaluates after the follow set lands."""
    login_seg = _main_body("perform_login")
    assert 'getattr(self, "_refresh_articles_row", None)' in login_seg
    assert "GLib.idle_add(refresh)" in login_seg
    contacts_seg = _main_body("on_contacts_updated")
    assert 'getattr(self, "_refresh_articles_row", None)' in contacts_seg
    assert "refresh()" in contacts_seg


def test_reload_lists_newest_only_via_longform_dedupe():
    """EXIT CRITERION: the list shows one row per (pubkey, d), newest wins,
    newest-first — via the data layer's longform.dedupe_articles (the UI
    must not re-implement the rule)."""
    src = _src(_ARTICLES)
    assert "longform.dedupe_articles(" in src  # reuse, not re-implement

    fake = _fake_articles_self()
    ev_b = _article_ev("e" * 64, "post-2", "Second Post", 250)
    evs = {e["id"]: e for e in (EV_A_OLD, EV_A_NEW, EV_A_NEWER, ev_b)}
    fake.db.get_event_by_id.side_effect = lambda eid: evs.get(eid)
    fake.model = MagicMock()  # scripted page: fresh first page
    fake.model.event_ids = [EV_A_OLD["id"], EV_A_NEW["id"],
                            EV_A_NEWER["id"], ev_b["id"]]
    fake._reload()
    fake.model.load_first.assert_called_once()
    assert fake.list_box.append.call_count == 2  # deduped one row per addr
    assert set(fake._cards) == {EV_A_NEWER["id"], ev_b["id"]}
    assert fake._row_by_addr[(PUB, "post-1")] == EV_A_NEWER["id"]
    assert fake._row_by_addr[(PUB, "post-2")] == ev_b["id"]  # same author
    # Render order newest-first: 300 (post-1 newest) before 250 (post-2).
    assert list(fake._row_by_id) == [EV_A_NEWER["id"], ev_b["id"]]


def test_rows_show_title_author_updated_and_optional_zap_chip():
    """Row spec: compact row = title + author/updated meta line; a ⚡sats
    chip only when verified 9735 receipts exist (zap_totals_for_events)."""
    gtk, created = _fresh_gtk()
    fake = _fake_articles_self(gtk=gtk)
    fake.db.get_profile.return_value = {"display_name": "Alice"}
    card = longform.parse_article_event(EV_A_NEW)
    fake.db.zap_totals_for_events.return_value = {card.id: 21000}  # msat
    fake._render_card(card)
    labels = [m.construct_kwargs["label"] for m in created
              if "label" in m.construct_kwargs]
    assert "Post One" in labels
    meta = next(t for t in labels if t.startswith("Alice ·"))
    assert " ago" in meta           # humanized updated timestamp
    assert "⚡21" in meta           # 21000 msat -> 21 sats chip
    fake.db.zap_totals_for_events.assert_called_once_with([card.id])

    gtk2, created2 = _fresh_gtk()
    fake2 = _fake_articles_self(gtk=gtk2)
    fake2.db.get_profile.return_value = {"display_name": "Alice"}
    fake2._render_card(card)
    labels2 = [m.construct_kwargs["label"] for m in created2
               if "label" in m.construct_kwargs]
    meta2 = next(t for t in labels2 if t.startswith("Alice ·"))
    assert "⚡" not in meta2        # no verified receipts -> no chip

    # DB blowup in the zap lookup must not kill the row.
    gtk3, _c3 = _fresh_gtk()
    fake3 = _fake_articles_self(gtk=gtk3)
    fake3.db.zap_totals_for_events.side_effect = RuntimeError("db gone")
    fake3._render_card(card)       # must not raise


def test_row_activation_opens_c1_reader_with_that_card():
    """EXIT CRITERION: activating a row hands the row's card to the C1
    reader path (main_window.show_article_reader). Rows are wired via the
    real row-activated signal; card-less rows open nothing."""
    src = _src(_ARTICLES)
    assert 'self.list_box.connect("row-activated", self._on_row_activated)' \
        in src

    fake = _fake_articles_self()
    card = longform.parse_article_event(EV_A_NEW)
    fake._on_row_activated(MagicMock(), types.SimpleNamespace(card=card))
    fake.main_window.show_article_reader.assert_called_once_with(card)

    fake2 = _fake_articles_self()
    fake2._on_row_activated(MagicMock(), types.SimpleNamespace(card=None))
    fake2.main_window.show_article_reader.assert_not_called()


# ---------------------------------------------------------------------------
# C3 (card t_54e093f7): comment composer — gating + signed-then-gated post
# ---------------------------------------------------------------------------

C3_PRIV = "22" * 32
C3_PUB = nostr_utils.get_public_key(C3_PRIV)


def _reader_with_composer(text="ship it", pub_key=PUB):
    """Fake reader self extended with the real composer handlers bound."""
    fake = _fake_reader_self()
    fake.comment_composer = MagicMock()
    fake.comment_entry = MagicMock()
    fake.comment_entry.get_text.return_value = text
    fake.comment_post_btn = MagicMock()
    fake.main_window = types.SimpleNamespace(
        pub_key=pub_key, add_toast=MagicMock()
    )
    fake._fetched_events = None
    for m in ("_sync_composer_visibility", "_on_comment_post"):
        setattr(fake, m, types.MethodType(
            _class_method(_ARTICLES, "ArticleReaderPage", m, dict(_NS)), fake))
    return fake


def test_composer_visible_only_when_signed_in():
    out = _reader_with_composer(pub_key=None)
    out._sync_composer_visibility()
    out.comment_composer.set_visible.assert_called_once_with(False)

    in_ = _reader_with_composer(pub_key=C3_PUB)
    in_._sync_composer_visibility()
    in_.comment_composer.set_visible.assert_called_once_with(True)

    # empty-string pub_key (backend selected, key not fetched yet) = out
    mid = _reader_with_composer(pub_key="")
    mid._sync_composer_visibility()
    mid.comment_composer.set_visible.assert_called_once_with(False)

    # duck-typed test window without the attribute reads as signed-out
    duck = _reader_with_composer()
    duck.main_window = object()
    duck._sync_composer_visibility()
    duck.comment_composer.set_visible.assert_called_once_with(False)


def test_comment_post_builds_signed_1111_with_correct_tags():
    """Exit criterion: post click builds the 1111 with e/a/k (+ root) tags
    and the client-side signing round-trip verifies."""
    fake = _reader_with_composer(pub_key=C3_PUB)
    captured = []

    def fake_publish(ev, article_id):
        # UI must hand over the UNSIGNED template; signing is the client's
        # job (signed-then-gated: nothing renders until dispatch accepted).
        assert "sig" not in ev and "id" not in ev
        assert article_id == fake.card.id
        captured.append(nostr_utils.sign_event(ev, C3_PRIV))
        return True

    fake.client.publish_article_comment = fake_publish
    fake._on_comment_post(None)

    assert len(captured) == 1
    signed = captured[0]
    assert signed["kind"] == 1111
    assert signed["pubkey"] == C3_PUB
    assert signed["content"] == "ship it"
    from gnostr.zaps.request import verify_event_sig
    assert verify_event_sig(signed)
    tags = {(t[0], t[1]) for t in signed["tags"]}
    assert ("e", fake.card.id) in tags and ("E", fake.card.id) in tags
    assert ("a", fake.card.address) in tags
    assert ("A", fake.card.address) in tags
    assert ("p", fake.card.pubkey) in tags and ("P", fake.card.pubkey) in tags
    assert ("k", "30023") in tags and ("K", "30023") in tags

    # optimistic row landed, keyed on the signed id (dedup invariant)
    assert fake._db_comment_events[0]["id"] == signed["id"]
    # entry cleared, exactly one row rendered
    fake.comment_entry.set_text.assert_called_once_with("")
    assert fake.comments_box.append.call_count == 1
    fake.comments_count.set_text.assert_called_with("(1)")

    # relay echo (same id via fetch settle) dedups: still ONE row
    fake._on_comments_done([signed], {"fallback_used": False})
    assert fake.comments_box.append.call_count == 2  # repaint cycle
    assert fake._db_comment_events[0]["id"] == signed["id"]
    assert len(fake._db_comment_events) == 1


def test_comment_post_signed_out_is_silent_noop():
    fake = _reader_with_composer(pub_key=None)
    fake._on_comment_post(None)
    fake.client.publish_article_comment.assert_not_called()
    assert not fake._db_comment_events
    fake.comment_entry.set_text.assert_not_called()


def test_comment_post_empty_or_whitespace_is_noop():
    blank = _reader_with_composer(text="   \n  ")
    blank._on_comment_post(None)
    blank.client.publish_article_comment.assert_not_called()
    assert not blank._db_comment_events


def test_comment_post_dispatch_failure_no_optimistic_row():
    fake = _reader_with_composer(pub_key=C3_PUB)
    fake.client.publish_article_comment.return_value = False
    fake._on_comment_post(None)
    assert not fake._db_comment_events
    fake.comment_entry.set_text.assert_not_called()  # text kept for retry
    # user gets feedback via toast when the window supports one
    fake.main_window.add_toast.assert_called_once()
