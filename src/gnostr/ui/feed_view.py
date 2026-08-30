# src/ui/feed_view.py
"""Feed page — thin GTK glue over service.feed_model.FeedModel.

Workstream #0. All the hard behavior (bounded window, keyset cursor,
eviction, live-prepend buffer, end-of-DB sentinel) lives in FeedModel,
which is GTK-free and unit-tested headless. This view only:

  * owns the ScrolledWindow + vadjustment scroll plumbing
  * maps model.event_ids -> PostWidget instances (reusing widgets by
    event id so content is never re-rendered)
  * reacts to scroll position:
      near-bottom -> evict posts that scrolled out the top, load older
      near-top    -> re-pull evicted newer posts (or auto-flush the
                     new-posts buffer when already at the top)

The feed page is model-driven only for the "following" feed (the
workstream target). "global" and "me" feeds keep the legacy inline
render in MainWindow.switch_feed; their handlers here no-op when
``self.feed_model`` is None, so the scroll wiring is inert for them.
"""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

# Scroll thresholds in pixels. NEAR_BOTTOM_PX triggers load-older when
# the user is within this distance of the end of the feed. NEAR_TOP_PX
# triggers the re-pull / buffer flush when the user is within this
# distance of the top of the feed.
NEAR_BOTTOM_PX = 300
NEAR_TOP_PX = 120
# Max consecutive auto-continuation loads while pinned at the bottom of the
# feed (one scroll gesture). Bounds the work a single gesture can trigger;
# the counter resets on the next genuine value-changing scroll.
_BOTTOM_AUTO_CAP = 8


class FeedView(Adw.NavigationPage):
    def __init__(self, main_window):
        super().__init__(title="Feed", tag="feed")
        self.main_window = main_window
        # Set by MainWindow for the "following" feed (None otherwise).
        self.feed_model = None
        self._loading_older = False
        self._syncing = False
        # Scroll-trigger state: the last scroll value seen. A zone fires
        # only when the value actually changes — i.e. a real user scroll.
        # A page load grows the content (upper) without moving value, so an
        # at-rest scroll can't cascade; the next real scroll re-fires.
        # Programmatic moves (anchor restore, scroll-to-top) run under
        # _syncing and never count.
        self._last_value = None
        # Consecutive auto-continuation loads since the last genuine
        # scroll (capped at _BOTTOM_AUTO_CAP to bound one gesture's work).
        # Reset whenever a genuine value-changing scroll arrives.
        self._bottom_auto = 0
        self._top_auto = 0

        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(self.box)

        # Header bar with refresh button.
        hb = Adw.HeaderBar()
        btn_refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        btn_refresh.set_tooltip_text("Refresh Feed")
        btn_refresh.connect("clicked", self.on_refresh_clicked)
        hb.pack_end(btn_refresh)
        self.box.append(hb)

        # "N new posts" pill — fixed above the scroll area (not
        # scrolling), hidden until the FeedModel buffers live events.
        self._pill = Gtk.Button(label="New posts", css_classes=["flat", "pill"])
        self._pill.set_halign(Gtk.Align.CENTER)
        self._pill.set_margin_top(6)
        self._pill.set_visible(False)
        self._pill.connect("clicked", self.on_pill_clicked)
        self.box.append(self._pill)

        # Content area.
        self._scrolled = Gtk.ScrolledWindow(vexpand=True)
        self._scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        c = Adw.Clamp(maximum_size=600)
        self.posts_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        # End-of-feed sentinel — permanent last child of posts_box, hidden
        # until the FeedModel reports end-of-DB.
        self._sentinel = Gtk.Label(
            label="You're all caught up",
            css_classes=["caption", "dim-label"],
        )
        self._sentinel.set_margin_top(12)
        self._sentinel.set_visible(False)
        self.posts_box.append(self._sentinel)
        c.set_child(self.posts_box)
        self._scrolled.set_child(c)
        self.box.append(self._scrolled)

        # Scroll plumbing.
        self._vadj = self._scrolled.get_vadjustment()
        self._vadj.connect("changed", self.on_scroll_changed)

    # ------------------------------------------------------------------
    # Scroll plumbing
    # ------------------------------------------------------------------

    def on_scroll_changed(self, adjustment):
        """React to scroll position. No-op for non-model feeds."""
        if self.feed_model is None or self._syncing:
            return
        upper = adjustment.get_upper()
        page = adjustment.get_page_size()
        value = adjustment.get_value()
        # Distances are only meaningful when content overflows the viewport.
        if upper <= page:
            return
        dist_top = value
        dist_bottom = (upper - page) - value
        # Fire only on a genuine value change (real user scroll). A page
        # load changes upper, not value, so an at-rest feed can't cascade;
        # the next real scroll re-fires. Programmatic moves run under
        # _syncing and are ignored. Both zones are checked (not elif): a
        # short feed at value=0 is legitimately near BOTH top and bottom.
        if self._last_value == value:
            return
        self._last_value = value
        # A genuine scroll (value actually moved) is a new gesture — reset
        # the auto-continuation counters so one gesture's work is bounded.
        self._bottom_auto = 0
        self._top_auto = 0
        if dist_bottom <= NEAR_BOTTOM_PX:
            self._on_near_bottom()
        if dist_top <= NEAR_TOP_PX:
            self._on_near_top()

    def _on_near_bottom(self):
        """Load the next (older) page and keep the viewport pinned at the
        bottom while there's still more history.

        With a bounded window the content height is ~constant, so once the
        user reaches the absolute bottom the scroll value is clamped and
        can't move — a value-change trigger alone could never page to
        end-of-DB. So after each page we re-check: if the user is still at
        the bottom and the feed isn't exhausted, load the next page (via an
        idle hop, to avoid deep recursion). A consecutive-load cap stops a
        single scroll gesture from draining an unbounded feed; the cap
        resets whenever a genuine (value-changing) scroll arrives."""
        if self._loading_older:
            return
        m = self.feed_model
        if m is None or m.exhausted:
            return
        self._loading_older = True
        try:
            added = m.load_older()
        finally:
            self._loading_older = False
        # Sync on new rows, or when we just hit the end-of-DB (empty page)
        # so the "caught up" sentinel can appear. The model self-bounds the
        # window (evicting the newest overflow off the top); the view just
        # re-syncs the box and restores the scroll anchor.
        if added or m.exhausted:
            self._sync_window(anchor_id=self._topmost_visible_id())
        self._maybe_continue_bottom()

    def _maybe_continue_bottom(self):
        """Continue paging to end-of-DB while the user stays at the bottom."""
        m = self.feed_model
        if m is None or m.exhausted or self._bottom_auto >= _BOTTOM_AUTO_CAP:
            return
        upper = self._vadj.get_upper()
        page = self._vadj.get_page_size()
        value = self._vadj.get_value()
        at_bottom = (upper - page) - value <= NEAR_BOTTOM_PX
        if at_bottom:
            self._bottom_auto += 1
            GLib.idle_add(self._on_near_bottom)


    def _on_near_top(self):
        """At the top edge: re-pull the evicted newer posts, or auto-flush
        the new-posts buffer when we're already at the absolute top."""
        m = self.feed_model
        if m is None:
            return
        if not m.at_top and m.can_load_newer():
            anchor = self._topmost_visible_id()
            added = m.load_newer()
            # The model self-bounds the window (evicting the oldest overflow
            # off the bottom); the view re-syncs the box and restores the
            # scroll anchor, then auto-continues if still pinned at the top.
            if added or len(m.event_ids) != 0:
                self._sync_window(anchor_id=anchor)
            self._maybe_continue_top()
        elif m.new_ids:
            self.flush_new()

    def _maybe_continue_top(self):
        """Continue re-pulling to the true top while the user stays pinned at
        the top. Symmetric to the bottom continuation: with a bounded window
        the scroll value is clamped at the top, so a value-change trigger
        alone could not re-pull a multi-page evicted span back."""
        m = self.feed_model
        if m is None or m.at_top or self._top_auto >= _BOTTOM_AUTO_CAP:
            return
        value = self._vadj.get_value()
        if value <= NEAR_TOP_PX:
            self._top_auto += 1
            GLib.idle_add(self._on_near_top)

    # ------------------------------------------------------------------
    # Widget <-> model sync
    # ------------------------------------------------------------------

    def _content_y(self, child):
        """Y of `child`'s top edge in scroll-content space (0 = top of
        the feed), or None when it can't be resolved (not mapped yet).
        The ScrolledWindow's viewport is a SIBLING of the scrolled
        widget, so the translation runs viewport -> child (the target
        must be a descendant of the source)."""
        viewport = self._scrolled.get_first_child()
        if viewport is None:
            return None
        # GTK4 translate_coordinates -> (x, y) on success, None when the
        # widgets are not in a common tree (unmapped / detached).
        r = viewport.translate_coordinates(child, 0, 0)
        return r[1] if r is not None else None

    def _topmost_visible_id(self):
        """event_id of the first post whose top edge is at/below the
        viewport top, or None. Used as the scroll anchor after a
        rebuild."""
        value = self._vadj.get_value()
        child = self.posts_box.get_first_child()
        while child is not None:
            if child is self._sentinel:
                child = child.get_next_sibling()
                continue
            y = self._content_y(child)
            if y is not None and y >= value:
                return getattr(child, "event_id", None)
            child = child.get_next_sibling()
        return None

    def _scroll_to_top(self):
        self._syncing = True
        try:
            self._vadj.set_value(0.0)
        finally:
            self._syncing = False

    def _restore_anchor(self, anchor_id):
        """Scroll so the anchored post sits at the viewport top again
        (best-effort across widget reuse).

        The rebuild invalidates allocations, so the offset is computed in
        a GLib.idle callback — after the next layout pass — using
        translate_coordinates (content-space, margin/clamp safe)."""
        mw = self.main_window
        w = mw.event_widgets.get(anchor_id)
        if w is None:
            return
        GLib.idle_add(self._do_restore_anchor, w)

    def _do_restore_anchor(self, w):
        if self.feed_model is None or not w.get_visible():
            return
        y = self._content_y(w)
        if y is None or y < 0:
            return
        self._syncing = True
        try:
            self._vadj.set_value(float(y))
        finally:
            self._syncing = False

    def sync_window(self):
        """Full sync after load_first(): build the box to match the
        model window and scroll to the top (newest)."""
        self._sync_window(anchor_id=None, scroll_to_top=True)

    def _sync_window(self, anchor_id=None, scroll_to_top=False):
        """Rebuild posts_box to match feed_model.event_ids (newest-first),
        reusing PostWidgets by event id so content is never re-rendered.

        Evicted ids are dropped from the box and from mw.event_widgets.
        The sentinel toggles on the end-of-DB flag; the pill reflects the
        new-posts buffer.
        """
        m = self.feed_model
        if m is None:
            return
        mw = self.main_window
        ids = m.event_ids
        id_set = set(ids)

        # 1. Hydrate: ensure a widget exists for every in-window id.
        for eid in ids:
            if eid not in mw.event_widgets:
                mw.make_post_widget(eid)

        # 2. Drop widgets evicted out of the window (either end).
        for eid in list(mw.event_widgets):
            if eid not in id_set:
                mw.event_widgets.pop(eid, None)

        # 3. Rebuild the box in model order (sentinel stays last).
        #    Walk with get_next_sibling: removing the first child while
        #    re-reading get_first_child() would re-yield a surviving
        #    child (the sentinel) forever.
        child = self.posts_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            if child is not self._sentinel:
                self.posts_box.remove(child)
            child = nxt
        for eid in ids:
            w = mw.event_widgets.get(eid)
            if w is not None:
                self.posts_box.append(w)
        # The sentinel survived in its original slot (index 0); posts are
        # appended AFTER it — push it back to the tail.
        self.posts_box.remove(self._sentinel)
        self.posts_box.append(self._sentinel)

        # 4. Pill + sentinel visibility.
        if m.new_ids:
            n = len(m.new_ids)
            self._pill.set_label(
                "1 new post" if n == 1 else "%d new posts" % n
            )
            self._pill.set_visible(True)
        else:
            self._pill.set_visible(False)
        self._sentinel.set_visible(bool(m.exhausted) and bool(ids))

        # 5. Restore scroll position.
        if scroll_to_top:
            self._scroll_to_top()
        elif anchor_id is not None:
            self._restore_anchor(anchor_id)

    # ------------------------------------------------------------------
    # Feed lifecycle (called by MainWindow)
    # ------------------------------------------------------------------

    def start_model_feed(self):
        """Build a FeedModel for the active following feed and load the
        first page. Called by MainWindow.switch_feed('following')."""
        mw = self.main_window
        from ..service.feed_model import FeedModel

        self.feed_model = FeedModel(
            owner_pubkey=mw.pub_key,
            database=mw.db,
            page_size=50,
            max_window=100,
        )
        self.feed_model.load_first()
        self.sync_window()

    def end_feed(self):
        """Tear down a model-driven feed (switching to another feed type)."""
        if self.feed_model is not None:
            self.feed_model.reset()
            self.feed_model = None
        for eid in list(self.main_window.event_widgets):
            self.main_window.event_widgets.pop(eid, None)
        self.clear_posts()
        self._pill.set_visible(False)
        self._sentinel.set_visible(False)

    def clear_posts(self):
        """Remove all rendered posts from the box (sentinel stays)."""
        child = self.posts_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            if child is not self._sentinel:
                self.posts_box.remove(child)
            child = nxt

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def on_pill_clicked(self, btn):
        """User tapped 'N new posts' -> flush the buffered events to the top."""
        self.flush_new()

    def flush_new(self):
        """Prepend the buffered live events to the window and re-sync."""
        m = self.feed_model
        if m is None or not m.new_ids:
            return
        m.flush_new_at_top()
        self._sync_window()

    def buffered_new(self, inserted_ids):
        """Live events were buffered while the user is mid-feed: refresh
        the pill count only — the posts render when the user scrolls to
        the top or taps the pill (no box rebuild)."""
        m = self.feed_model
        if m is None or not m.new_ids:
            return
        n = len(m.new_ids)
        self._pill.set_label("1 new post" if n == 1 else "%d new posts" % n)
        self._pill.set_visible(True)

    def on_refresh_clicked(self, btn):
        self.main_window.on_refresh_clicked()
