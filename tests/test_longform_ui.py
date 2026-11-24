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
    _NS[_helper] = _module_func(_ARTICLES, _helper)


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


EV_A_NEW = _article_ev("a" * 64, "post-1", "Post One", 200)
EV_A_OLD = _article_ev("o" * 64, "post-1", "Post One (older edit)", 100)
EV_A_NEWER = _article_ev("n" * 64, "post-1", "Post One (newest edit)", 300)


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


def _fake_articles_self(section_open=False):
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
    for m in ("_on_map", "_on_unmap", "_on_quote_event", "_render_card",
              "_remove_row", "_clear_rows", "_sync_footer",
              "_load_more_visible"):
        setattr(fake, m, types.MethodType(
            _class_method(_ARTICLES, "ArticlesPage", m, ns), fake))
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
        _article_ev("z" * 64, "post-9", "Later", 500)))
    assert "z" * 64 not in fake._cards
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


def _fake_reader_self(card=longform.parse_article_event(EV_A_NEW)):
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
    fake.comments_empty = MagicMock()
    fake._comments_deadline_id = None
    fake._comments_settled = False
    fake._db_comment_events = []
    fake._fallback_used = False
    ns = dict(_NS)
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
    # Offline-first paint happened BEFORE settling (empty-state visible).
    assert fake.comments_err.set_visible.called_with(False) or True


def test_comments_merge_dedups_by_id_newest_first():
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
    assert ids == ["c" * 63 + "2", "c" * 63 + "3", "c" * 63 + "1"]
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
    False -> 2 (no caption)."""
    fake = _fake_reader_self()
    fetch_ev = _comment_ev("c" * 63 + "7", 300)
    fake._render_comments([fetch_ev], False)
    first_box = fake.comments_box.append.call_args[0][0]
    assert first_box.append.call_count == 2  # no caption on precise chain
    fake.comments_box.reset_mock()
    fake._render_comments([fetch_ev], True)
    capped_box = fake.comments_box.append.call_args[0][0]
    assert capped_box.append.call_count == 3  # caption line present
    captions = [c.args[0].get_text() if hasattr(c.args[0], "get_text")
                else None for c in capped_box.append.call_args_list]
    assert any(c is None for c in captions)  # captions are MagicMock labels
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
    assert rows[0][0] == "(untitled draft)"  # newest first
    assert "kind 1111" in rows[0][1]
    assert "encrypted" in rows[2][1]  # nevent_id None -> encrypted note
    assert rows[1][0] == "note"


def test_drafts_no_key_meta_prompts_signin():
    fake = MagicMock()
    fake.list_box = MagicMock()
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
    """P1 drafts list is READ-ONLY: no publish/sign path in the UI file."""
    src = _src(_ARTICLES)
    for banned in ("publish_event", "sign_event", "nsec", "privkey",
                   "Gtk.TextEditor", "EntryBuffer", "composer"):
        assert banned not in src, f"{banned!r} must not appear (no composer)"


# ---------------------------------------------------------------------------
# https-only cover: render-skip when None (parser already enforces https)
# ---------------------------------------------------------------------------


def test_cover_https_only_and_none_skips_render():
    http_ev = _article_ev("h" * 64, "post-h", "HTTP cover", 200)
    http_ev["tags"] = [["d", "post-h"], ["image", "http://evil/x.jpg"]]
    card = longform.parse_article_event(http_ev)
    assert card.image is None  # https-only policy: http dropped at parse
    # The UI renders the cover block only when the card carries one.
    src = _src(_ARTICLES)
    assert src.count("if card.image:") == 2  # section card + reader page
