# src/gnostr/ui/articles_page.py
"""NIPW P1 long-form UI (spec nipw2026-11-longform-v1 §P1 UI scope).

ArticlesPage: the opt-in 30023 section. Opening it (sidebar row) is the
opt-in; the live sub opens when the page maps and CLOSEs when it detaches
(unmap) — 30023 never enters the kind-1 feed models (agentic-flood
posture, enforced in service/feed_pagination: the 'articles' source
accepts kind 30023 only). Pagination rides the shared FeedModel with
make_source("articles", ...); live arrivals are upserted in time order.
Compact row = title + dim author/updated meta line + a ⚡sats chip when
verified 9735 zap receipts exist (C2 row spec "zap count if available").
Sidebar gating: the Articles row starts hidden and MainWindow reveals it
once the user follows at least one 30023 author (DB evidence / live
arrival). Cover images are https-only (longform.parse_article_event
already drops anything else — None means render skip). Edit-replacement
is transparent: newest wins per (pubkey, d) — the list defers to
longform.dedupe_articles on reload and the _render_card rank guard on
live arrivals (same rule as db.get_event_by_a).

ArticleReaderPage: title/summary/markdown-as-plaintext (emphasis via
markup.emphasis_to_pango, selectable), cover, author line. Comments thread
under the reader: DB rows render offline-first, client.fetch_article_comments
merges in by event id (dedupe), OLDEST-first (reply order, card C3). The
composer row (entry + Post) renders only for a signed-in window: posting
builds the unsigned 1111 template (longform.build_comment_event: e/a/k
parent + E/A/K/P root pointers) and hands it to
client.publish_article_comment — the signing happens there
(signed-then-gated, group-composer pattern), which fans to the article's
known relays ∪ default write relays; the optimistic row keyed on the
template id dedups the relay echo by id. The noisy fallback set
(meta fallback_used — relay-side tag filtering broken, spec Q1) shows each
comment's UPPERCASE root binding (NIP-26: A/K/P are root pointers) so the
user can see why a loosely-matched comment is there; the precise primary
chain renders no caption. Every async step carries a watchdog + visible
Retry (GroupDetailPage reliability house rule).

DraftsPage: read-only kind-31234 list (target_kind, d) via
client.fetch_drafts. NO edit/publish path (out of P1 scope), kind-1234
checkpoints stay out.

30024 legacy-draft decision (reported): NOT surfaced in P1. The data-layer
probe audit (longform.py docstring) found live 30024 rows belong to OTHER
authors (7 on nos.lol, 0 on our own accounts); surfacing them would need a
REQ the P1 contract does not make (31234-only) and get_event_by_a(30024,
pubkey, d) needs a known d that 31234 wraps do not carry. The P1 drafts
list therefore reads fetch_drafts (31234) exclusively.
"""

import json
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango

from .. import longform, markup, nostr_utils

AdwClamp = Adw.Clamp

# UI watchdog deadlines: the client settles every longform fetch at
# LONGFORM_FETCH_TIMEOUT_MS (12s) by construction; the page must show a
# user-visible failure shortly after, never stall silently.
COMMENTS_DEADLINE_MS = 14_000
DRAFTS_DEADLINE_MS = 14_000

ARTICLE_PAGE_SIZE = 20
ARTICLE_WINDOW_MAX = longform.ARTICLE_FEED_LIMIT  # spec: 30 cards per section


def _ago(ts):
    """Relative timestamp (phone-friendly, same helper shape as groups_page)."""
    d = max(0, int(time.time()) - int(ts))
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


def _short_pk(pk):
    return f"{pk[:8]}…{pk[-8:]}" if pk and len(pk) > 16 else (pk or "")


def _rank(card):
    """Recency rank identical to longform.dedupe_articles (published_at wins)."""
    return (card.published_at or card.created_at, card.id)


class ArticlesPage(Gtk.Box):
    """The opt-in Articles section: kind-30023 cards, live while mapped."""

    def __init__(self, main_window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.main_window = main_window
        self.client = main_window.client
        self.db = main_window.db

        self._section_open = False
        self._cards = {}  # event id -> ArticleCard (rendered)
        self._row_by_id = {}  # event id -> Gtk.ListBoxRow
        self._row_by_addr = {}  # (pubkey, d) -> event id currently rendered
        self._live_events = {}  # id -> raw ev (DB buffer flush may lag)

        # Shared pagination state machine, 'articles' source (kind 30023
        # only — the main feed's kind-1 subs are never touched).
        from ..service.feed_model import FeedModel
        from ..service.feed_pagination import make_source

        owner = getattr(main_window, "pub_key", None)
        self.model = FeedModel(
            owner,
            self.db,
            page_size=ARTICLE_PAGE_SIZE,
            max_window=ARTICLE_WINDOW_MAX,
            source=make_source("articles", self.db, owner),
        )

        bar = Gtk.Box(spacing=12, halign=Gtk.Align.START, margin_top=6,
                      margin_bottom=6, margin_start=6)
        # No manual back button: content_nav (Adw.NavigationView) provides
        # the automatic one (DOX DM-2 two-backs lesson).
        btn_refresh = Gtk.Button(icon_name="view-refresh-symbolic",
                                 css_classes=["flat"])
        btn_refresh.set_tooltip_text("Reload articles")
        btn_refresh.connect("clicked", self._on_refresh)
        bar.append(btn_refresh)
        btn_drafts = Gtk.Button(label="Drafts")
        btn_drafts.connect("clicked", lambda b: main_window.show_drafts())
        bar.append(btn_drafts)
        self.append(bar)

        clamp = AdwClamp()
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_box.connect("row-activated", self._on_row_activated)
        clamp.set_child(self.list_box)
        scroll.set_child(clamp)
        self.append(scroll)

        self.load_more_btn = Gtk.Button(label="Load older")
        self.load_more_btn.add_css_class("flat")
        self.load_more_btn.set_margin_bottom(12)
        self.load_more_btn.connect("clicked", self._on_load_more)
        self.load_more_btn.set_visible(False)
        self.append(self.load_more_btn)

        self._empty = Gtk.Label(label="No long-form articles yet.\n"
                                      "Open articles will appear here.")
        self._empty.set_justify(Gtk.Justification.CENTER)
        self._empty.add_css_class("dim-label")
        self._empty.set_wrap(True)
        self._empty.set_margin_top(48)
        self._empty.set_visible(False)
        self.append(self._empty)

        # Live route: 30023 arrivals already reach quote-event-received
        # (fires for EVERY kind after the client persists them). The kind
        # filter here is the section boundary — kind-1 events pass through
        # untouched, and this page never widens any feed subscription.
        self.client.connect("quote-event-received", self._on_quote_event)

        # Opt-in lifecycle: sub opens when the section is on screen,
        # CLOSES when it detaches. A popped page stays connected (house
        # dm_inbox pattern) and is inert while _section_open is False.
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)

        self._reload()

    # ------------------------------------------------------------------
    # Section lifecycle (opt-in)
    # ------------------------------------------------------------------

    def _on_map(self, *_args):
        if not self._section_open:
            self.client.open_article_section()
            self._section_open = True

    def _on_unmap(self, *_args):
        if self._section_open:
            self.client.close_article_section()
            self._section_open = False

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _reload(self):
        """Fresh first page from the model + re-render the whole list."""
        self.model.load_first()
        self._clear_rows()
        cards = []
        for eid in self.model.event_ids:
            ev = self.db.get_event_by_id(eid) or self._live_events.get(eid)
            card = longform.parse_article_event(ev)
            if card is not None:
                cards.append(card)
        # Newest-only edit-replacement defers to the data layer's ONE rule
        # (longform.dedupe_articles): newest wins per (pubkey, d), newest-
        # first output. The _render_card rank guard covers live arrivals.
        for card in longform.dedupe_articles(cards):
            self._render_card(card)
        self._sync_footer()

    def _on_load_more(self, _btn):
        added = self.model.load_older()
        for eid in added or []:
            ev = self.db.get_event_by_id(eid) or self._live_events.get(eid)
            card = longform.parse_article_event(ev)
            if card is not None:
                self._render_card(card)
        self._sync_footer()

    def _sync_footer(self):
        self._load_more_visible(self.model and not self.model.exhausted)
        self._empty.set_visible(not self._cards)

    def _load_more_visible(self, show):
        self.load_more_btn.set_visible(bool(show))

    def _clear_rows(self):
        row = self.list_box.get_first_child()
        while row is not None:
            nxt = row.get_next_sibling()
            self.list_box.remove(row)
            row = nxt
        self._cards = {}
        self._row_by_id = {}
        self._row_by_addr = {}

    def _zap_sats(self, event_id):
        """Received zap total in sats for one row (C2 row spec: "zap count
        if available"). Verified 9735 receipts only — db.zap_totals_for_events
        sums split receipts; no receipts means no chip."""
        try:
            totals = self.db.zap_totals_for_events([event_id]) or {}
            msat = int(totals.get(event_id, 0) or 0)
        except Exception:
            # Decorative chip: a DB blowup must never kill the row.
            return 0
        return msat // 1000

    def _render_card(self, card, at_top=False):
        """Append one article row (or replace an older edit in place).

        at_top=True (live arrivals): insert at the row-list head — they
        are the newest known. Reload/page renders append in page order.
        """
        addr = (card.pubkey, card.d)
        prev_id = self._row_by_addr.get(addr)
        if prev_id is not None:
            prev = self._cards.get(prev_id)
            if prev is not None and _rank(prev) >= _rank(card):
                return  # newest-only per (pubkey, d): keep the newer edit
            self._remove_row(prev_id)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)

        title = Gtk.Label(label=card.title or card.d)
        title.add_css_class("heading")
        title.set_wrap(True)
        title.set_xalign(0)
        box.append(title)

        prof = self.db.get_profile(card.pubkey) or {}
        name = prof.get("display_name") or prof.get("name") or \
            nostr_utils.hex_to_npub(card.pubkey) or _short_pk(card.pubkey)
        meta_text = f"{name} · {_ago(card.published_at or card.created_at)}"
        zaps = self._zap_sats(card.id)
        if zaps:
            meta_text += f" · ⚡{zaps}"
        meta = Gtk.Label(label=meta_text)
        meta.add_css_class("dim-label")
        meta.set_xalign(0)
        box.append(meta)

        # https-only cover policy is enforced in the parser; None -> skip.
        if card.image:
            img_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            img_box.set_size_request(-1, 160)
            img_box.set_halign(Gtk.Align.FILL)
            box.append(img_box)
            from ..renderer import ImageLoader

            ImageLoader.load_image_into_widget(
                card.image, img_box, None, max_height=200,
                author_pk=card.pubkey,
            )

        if card.summary:
            summ = Gtk.Label(label=card.summary)
            summ.set_wrap(True)
            summ.set_xalign(0)
            summ.set_max_width_chars(80)
            summ.add_css_class("dim-label")
            box.append(summ)

        row = Gtk.ListBoxRow()
        row.set_child(box)
        row.card = card
        if at_top:
            self.list_box.insert(row, 0)
        else:
            self.list_box.append(row)
        self._cards[card.id] = card
        self._row_by_id[card.id] = row
        self._row_by_addr[addr] = card.id
        self._empty.set_visible(False)

    def _remove_row(self, eid):
        row = self._row_by_id.pop(eid, None)
        card = self._cards.pop(eid, None)
        if row is not None:
            self.list_box.remove(row)
        if card is not None:
            self._row_by_addr.pop((card.pubkey, card.d), None)

    def _on_row_activated(self, _listbox, row):
        card = getattr(row, "card", None)
        if card is not None:
            self.main_window.show_article_reader(card)

    # ------------------------------------------------------------------
    # Live arrivals
    # ------------------------------------------------------------------

    def _on_quote_event(self, _client, event_json):
        """Section boundary on the all-kinds quote signal: kind 30023 only.

        A popped page (_section_open False) is inert — its handler stays
        connected (house pattern) but must not mutate the live section.
        """
        if not self._section_open:
            return
        try:
            ev = json.loads(event_json)
        except (ValueError, TypeError):
            return
        if not isinstance(ev, dict) or ev.get("kind") != longform.KIND_ARTICLE:
            return
        card = longform.parse_article_event(ev)
        if card is None or card.id in self._cards:
            return
        self._live_events[card.id] = ev
        if self.model is not None:
            # Buffer into the pagination window, then flush at the top:
            # the section has no "N new posts" pill (max 30 cards) — the
            # card is visible immediately and stays in the window order.
            self.model.prepend_new([ev])
            self.model.flush_new_at_top()
        self._render_card(card, at_top=True)

    def _on_refresh(self, _btn):
        self._reload()
        if self._section_open:
            # Idempotent re-REQ of the section sub (client re-REQs the same
            # sub_id with the new limit).
            self.client.open_article_section()


class ArticleReaderPage(Gtk.Box):
    """One article: markdown-as-plaintext reader + NIP-22 comments thread."""

    def __init__(self, main_window, card):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.main_window = main_window
        self.client = main_window.client
        self.db = main_window.db

        # Edit-replacement transparent: the address point-query already
        # returns newest-only (db.get_event_by_a, NIP-33 replaceable).
        self.card = self._resolve_card(card)
        self._render_page()

    def _render_page(self):
        """Build the full reader tree for self.card. Split from __init__ so
        the standalone harness can exercise the whole render path without
        instantiating a mocked Gtk.Box base (same reason the DM views keep
        their build step separate). Covers title, byline (npub -> display
        name via the profile cache), updated timestamp, summary, https
        cover, markdown-lite body, and the comments-thread scaffolding."""
        card = self.card

        clamp = AdwClamp()
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_top(12)
        body.set_margin_bottom(24)
        body.set_margin_start(12)
        body.set_margin_end(12)
        clamp.set_child(body)
        scroll.set_child(clamp)
        self.append(scroll)

        title = Gtk.Label(label=card.title or card.d)
        title.add_css_class("title-1")
        title.set_wrap(True)
        title.set_xalign(0)
        body.append(title)

        prof = self.db.get_profile(card.pubkey) or {}
        name = prof.get("display_name") or prof.get("name") or \
            nostr_utils.hex_to_npub(card.pubkey) or _short_pk(card.pubkey)
        updated = card.published_at or card.created_at
        meta = Gtk.Label(label=f"{name} · {_ago(updated)} · {_short_pk(card.pubkey)}")
        meta.add_css_class("dim-label")
        meta.set_wrap(True)
        meta.set_xalign(0)
        body.append(meta)

        if card.summary:
            summ = Gtk.Label(label=card.summary)
            summ.set_wrap(True)
            summ.set_xalign(0)
            summ.add_css_class("dim-label")
            body.append(summ)

        if card.image:
            img_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            img_box.set_size_request(-1, 200)
            img_box.set_halign(Gtk.Align.FILL)
            body.append(img_box)
            from ..renderer import ImageLoader

            ImageLoader.load_image_into_widget(
                card.image, img_box, None, max_height=260,
                author_pk=card.pubkey,
            )

        content = Gtk.Label()
        content.set_wrap(True)
        content.set_selectable(True)
        content.set_xalign(0)
        content.set_yalign(0)
        content.set_max_width_chars(90)
        # markdown-as-plaintext: escape, then map **bold** / _italic_ to
        # Pango markup (house markup module) and hand it over via
        # set_markup — a bare set_text does NOT parse Pango markup in GTK4
        # and would render the <b>/<i> tags literally (real-GTK harness
        # test_longform_gtk_real.py caught this).
        content.set_markup(markup.emphasis_to_pango(
            GLib.markup_escape_text(card.markdown)))
        body.append(content)

        # --- comments thread -------------------------------------------
        head = Gtk.Box(spacing=8, margin_top=12)
        comments_title = Gtk.Label(label="Comments")
        comments_title.add_css_class("heading")
        comments_title.set_xalign(0)
        head.append(comments_title)
        self.comments_count = Gtk.Label(label="")
        self.comments_count.add_css_class("dim-label")
        head.append(self.comments_count)
        self.comments_refresh = Gtk.Button(label="Refresh")
        self.comments_refresh.add_css_class("flat")
        self.comments_refresh.connect("clicked", self._on_comments_refresh)
        head.append(self.comments_refresh)
        self.comments_retry = Gtk.Button(label="Retry")
        self.comments_retry.connect("clicked", self._on_comments_refresh)
        self.comments_retry.set_visible(False)
        head.append(self.comments_retry)
        body.append(head)

        # --- comment composer (card C3): visible ONLY when signed in.
        # Single-line entry + Post. Signing lives in the client
        # (publish_article_comment, signed-then-gated) — this page never
        # touches keys, keeping the reader free of any signer path.
        self.comment_composer = Gtk.Box(spacing=8)
        self.comment_entry = Gtk.Entry()
        self.comment_entry.set_placeholder_text("Add a comment…")
        self.comment_entry.set_hexpand(True)
        self.comment_entry.connect("activate", self._on_comment_post)
        self.comment_post_btn = Gtk.Button(label="Post")
        self.comment_post_btn.add_css_class("suggested-action")
        self.comment_post_btn.connect("clicked", self._on_comment_post)
        self.comment_composer.append(self.comment_entry)
        self.comment_composer.append(self.comment_post_btn)
        self.comment_composer.set_visible(False)  # _sync_composer_visibility decides
        body.append(self.comment_composer)

        self.comments_err = Gtk.Label(label="Comments did not load in time.")
        self.comments_err.add_css_class("error")
        self.comments_err.set_wrap(True)
        self.comments_err.set_xalign(0)
        self.comments_err.set_visible(False)
        body.append(self.comments_err)

        self.comments_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                    spacing=10)
        body.append(self.comments_box)

        self.comments_empty = Gtk.Label(label="No comments on this article yet.")
        self.comments_empty.add_css_class("dim-label")
        self.comments_empty.set_wrap(True)
        self.comments_empty.set_visible(False)
        body.append(self.comments_empty)

        self._comments_deadline_id = None
        self._comments_settled = False
        self._db_comment_events = []
        self._fallback_used = False
        self._fetched_events = None
        self._render_comments()  # offline first paint (DB rows)
        self._sync_composer_visibility()
        self._fetch_comments()

    # ------------------------------------------------------------------
    # Edit replacement
    # ------------------------------------------------------------------

    def _resolve_card(self, card):
        """Swap in the newest edit for the same (pubkey, d) address if the
        DB has one newer than the card the list rendered (transparent)."""
        try:
            row = self.db.get_event_by_a(
                longform.KIND_ARTICLE, card.pubkey, card.d)
        except Exception:
            return card
        fresh = longform.parse_article_event(row) if row else None
        if fresh is not None and _rank(fresh) > _rank(card):
            return fresh
        return card

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------

    def _fetch_comments(self):
        self._comments_settled = False
        self.comments_err.set_visible(False)
        self.comments_retry.set_visible(False)
        try:
            self._db_comment_events = list(self.db.get_article_comments(
                self.card.id, self.card.address))
        except Exception:
            self._db_comment_events = []
        self._render_comments()
        self.client.fetch_article_comments(self.card, self._on_comments_done)
        self._arm_comments_deadline()

    def _on_comments_refresh(self, _btn):
        self._fetch_comments()

    def _sync_composer_visibility(self):
        """Card C3: the composer renders only for a signed-in window. The
        reader is a transient NavigationPage rebuilt on every open, and
        login/logout re-push the stack, so evaluating at render time is
        enough — no live login signal needed here. Duck-typed test windows
        without pub_key read as signed-out (getattr house rule)."""
        signed_in = bool(getattr(self.main_window, "pub_key", None))
        self.comment_composer.set_visible(signed_in)

    def _on_comment_post(self, _source):
        """Post click / entry-activate (signed-then-gated like the group
        composer, test_group_join_race conventions): build the unsigned 1111
        template (longform.build_comment_event — e/a/k parent pointers +
        E/A/K/P root pointers), hand it to client.publish_article_comment,
        which signs once through the active backend and publishes to the
        article's known relays ∪ default write relays. The page never signs.

        compute_event_id(template) == id(signed) (id is signature-independent
        over the canonical serialization), so the optimistic row we insert
        under that id dedups against the relay echo on the next
        fetch/refresh — no double render, no race, no phantom on failure
        (we only insert after the client accepted the dispatch)."""
        text = (self.comment_entry.get_text() or "").strip()
        pk = getattr(self.main_window, "pub_key", None)
        if not text or not pk:
            return
        ev = longform.build_comment_event(pk, self.card, text)
        if ev is None:
            return
        if not self.client.publish_article_comment(ev, self.card.id):
            toast = getattr(self.main_window, "add_toast", None)
            if toast is not None:
                toast(Adw.Toast(title="Could not post comment"))
            return
        self.comment_entry.set_text("")
        pending = dict(ev)
        pending["id"] = nostr_utils.compute_event_id(ev)
        self._db_comment_events.append(pending)
        # Merged re-paint riding the SAME path as a settle (captions kept if
        # fallback was used). A later fetch settle re-merges the same set;
        # the id dedup absorbs the relay echo.
        self._render_comments(getattr(self, "_fetched_events", None),
                              self._fallback_used)

    def _arm_comments_deadline(self):
        self._disarm_comments_deadline()
        self._comments_deadline_id = GLib.timeout_add(
            COMMENTS_DEADLINE_MS, self._on_comments_deadline)

    def _disarm_comments_deadline(self):
        if self._comments_deadline_id is not None:
            GLib.source_remove(self._comments_deadline_id)
            self._comments_deadline_id = None

    def _on_comments_deadline(self):
        self._comments_deadline_id = None
        if self._comments_settled:
            return False
        self._comments_settled = True
        self.comments_err.set_visible(True)
        self.comments_retry.set_visible(True)
        return False  # one-shot

    def _on_comments_done(self, events, meta):
        if self._comments_settled:
            return  # watchdog already surfaced the failure
        self._comments_settled = True
        self._disarm_comments_deadline()
        self._fallback_used = bool((meta or {}).get("fallback_used"))
        self._fetched_events = events  # kept for post-merge re-paints (C3)
        self._render_comments(events, self._fallback_used)

    @staticmethod
    def _merge_comments(db_events, fetched_events):
        """CommentInfo rows merged by event id, OLDEST-first (card C3: a
        comment thread reads top-down in reply order).

        (created_at, id) ASC with a stable id tie-break keeps the render
        order deterministic against the offline-first paint.
        """
        by_id = {}
        for ev in list(db_events) + list(fetched_events or []):
            info = longform.parse_comment_event(ev)
            if info is not None:
                by_id[info.id] = info
        return sorted(by_id.values(),
                      key=lambda c: (c.created_at, c.id))

    @staticmethod
    def _binding_caption(info):
        """UPPERCASE root binding of a fallback-matched comment (NIP-26:
        A/K/P are ROOT pointers; lowercase parent pointers stay private —
        the caption explains why the comment is HERE, not what it replies
        to). None for the precise primary-chain set."""
        if info.root_address:
            return f"binds {info.root_address}"
        if info.root_id:
            kind = f"kind {info.root_kind} " if info.root_kind else ""
            return f"binds {kind}{info.root_id[:12]}…"
        if info.root_pubkey:
            return f"binds P {_short_pk(info.root_pubkey)}"
        return "binds (no root tag)"

    def _render_comments(self, fetched_events=None, fallback_used=False):
        child = self.comments_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.comments_box.remove(child)
            child = nxt
        infos = self._merge_comments(self._db_comment_events, fetched_events)
        for info in infos:
            prof = self.db.get_profile(info.pubkey) or {}
            name = prof.get("display_name") or prof.get("name") or \
                nostr_utils.hex_to_npub(info.pubkey) or _short_pk(info.pubkey)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            line = Gtk.Label(label=f"{name} · {_ago(info.created_at)}")
            line.add_css_class("dim-label")
            line.set_xalign(0)
            box.append(line)
            text = Gtk.Label()
            text.set_wrap(True)
            text.set_selectable(True)
            text.set_xalign(0)
            text.set_max_width_chars(80)
            # Same rule as the body label: markup only via set_markup.
            text.set_markup(markup.emphasis_to_pango(
                GLib.markup_escape_text(info.content or "")))
            box.append(text)
            # Fallback set only: show the ROOT binding that matched (the
            # noisy relay-ignored-tags half of the fetch).
            if fallback_used:
                cap = Gtk.Label(label=self._binding_caption(info))
                cap.add_css_class("dim-label")
                cap.add_css_class("caption")
                cap.set_wrap(True)
                cap.set_xalign(0)
                box.append(cap)
            self.comments_box.append(box)
        self.comments_count.set_text(f"({len(infos)})" if infos else "")
        self.comments_empty.set_visible(not infos)


class DraftsPage(Gtk.Box):
    """Read-only kind-31234 drafts list (NIP-37 read side). No edit path."""

    def __init__(self, main_window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.main_window = main_window
        self.client = main_window.client
        self.db = main_window.db

        bar = Gtk.Box(spacing=12, halign=Gtk.Align.START, margin_top=6,
                      margin_bottom=6, margin_start=6)
        btn_refresh = Gtk.Button(icon_name="view-refresh-symbolic",
                                 css_classes=["flat"])
        btn_refresh.set_tooltip_text("Reload drafts")
        btn_refresh.connect("clicked", self._on_refresh)
        bar.append(btn_refresh)
        self.append(bar)

        clamp = AdwClamp()
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        clamp.set_child(self.list_box)
        scroll.set_child(clamp)
        self.append(scroll)

        self.err_lbl = Gtk.Label(label="Drafts did not load in time.")
        self.err_lbl.add_css_class("error")
        self.err_lbl.set_wrap(True)
        self.err_lbl.set_visible(False)
        self.append(self.err_lbl)
        self.retry_btn = Gtk.Button(label="Retry")
        self.retry_btn.set_visible(False)
        self.retry_btn.connect("clicked", self._on_refresh)
        self.append(self.retry_btn)

        self._empty = Gtk.Label(label="No drafts.")
        self._empty.add_css_class("dim-label")
        self._empty.set_visible(False)
        self.append(self._empty)

        self._deadline_id = None
        self._settled = False
        self._fetch()

    def _on_refresh(self, _btn):
        self._fetch()

    def _fetch(self):
        self._settled = False
        self.err_lbl.set_visible(False)
        self.retry_btn.set_visible(False)
        self.client.fetch_drafts(self._on_drafts_done)
        self._arm_deadline()

    def _arm_deadline(self):
        self._disarm_deadline()
        self._deadline_id = GLib.timeout_add(
            DRAFTS_DEADLINE_MS, self._on_deadline)

    def _disarm_deadline(self):
        if self._deadline_id is not None:
            GLib.source_remove(self._deadline_id)
            self._deadline_id = None

    def _on_deadline(self):
        self._deadline_id = None
        if self._settled:
            return False
        self._settled = True
        self.err_lbl.set_visible(True)
        self.retry_btn.set_visible(True)
        return False

    def _on_drafts_done(self, entries, meta):
        if self._settled:
            return
        self._settled = True
        self._disarm_deadline()
        self._render_drafts(entries, meta)

    @staticmethod
    def _draft_rows(entries):
        """(title, subtitle) pairs, newest-first, for the read-only list."""
        rows = sorted(entries or [], key=lambda e: (e.created_at, e.wrap_id),
                      reverse=True)
        out = []
        for e in rows:
            title = e.d or "(untitled draft)"
            kind = e.target_kind if e.target_kind is not None else "?"
            sub = f"kind {kind} · {_ago(e.created_at)}"
            if e.nevent_id is None:
                sub += " · encrypted"
            out.append((title, sub))
        return out

    def _render_drafts(self, entries, meta):
        row = self.list_box.get_first_child()
        while row is not None:
            nxt = row.get_next_sibling()
            self.list_box.remove(row)
            row = nxt
        if meta and meta.get("settled_by") == "no-key":
            self._empty.set_text("Sign in with a key to read your drafts.")
            self._empty.set_visible(True)
            return
        pairs = self._draft_rows(entries)
        for title, sub in pairs:
            r = Adw.ActionRow(title=title, subtitle=sub)
            r.set_activatable(False)  # read-only: no edit, no open flow
            self.list_box.append(r)
        self._empty.set_text("No drafts.")
        self._empty.set_visible(not pairs)
