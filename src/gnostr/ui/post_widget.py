# src/gnostr/ui/post_widget.py
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Pango, Gdk
from ..renderer import ContentRenderer, ImageLoader
from ..util.scroll_gate import ScrollGate
from ..util import collapse as collapse_util


class PostWidget(Adw.Bin):
    def __init__(
        self,
        main_window,
        pubkey,
        content,
        event_id,
        tags=[],
        is_hero=False,
        created_at=None,
        root_id=None,
        root_pk=None,
        collapse_cap=0,
    ):
        super().__init__(css_classes=["card"])
        self.main_window = main_window
        self.pubkey = pubkey
        self.event_id = event_id
        # FEED-TRUNC: feed cards pass collapse_cap>0 and render a token-safe
        # cut of the post behind a 'Show more' row. The FULL content is kept
        # on the widget: copy and open-thread must never see the truncated
        # string (a truncated open-thread would persist the cut as the
        # thread hero).
        self._full_content = content
        self._collapsed = False
        display_content = content
        cap = collapse_util.normalize_cap(collapse_cap) if collapse_cap else 0
        if cap and len(content) > cap:
            display_content = collapse_util.safe_truncate(content, cap)
            self._collapsed = True
        self.content = display_content
        self._root_id = root_id or event_id  # thread root (self if root post)
        self._root_pk = root_pk or pubkey  # thread root author

        # Resolve the post's timestamp from the DB when the caller didn't provide
        # it (e.g. live event-received posts). Unobtrusive relative-time caption.
        if created_at is None:
            ev = main_window.db.get_event_by_id(event_id)
            created_at = ev.get("created_at") if ev else None
        self.created_at = created_at

        if is_hero:
            self.add_css_class("hero")

        # Store reference for metrics updates
        self.main_window.event_widgets[event_id] = self

        self.main_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        self.set_child(self.main_box)

        # Header: Avatar + Name
        hb = Gtk.Box(spacing=12)

        prof = self.main_window.db.get_profile(pubkey)
        name = pubkey[:8]
        if not prof:
            # Unknown author — fetch their metadata so the name/avatar resolves once
            # the kind-0 arrives (TTL-cached, deduped). Covers feed, replies, quotes.
            self.main_window.client.fetch_profile(pubkey)
        else:
            name = prof.get("display_name") or prof.get("name") or name

        self.avatar = Adw.Avatar(
            size=48 if is_hero else 40, show_initials=True, text=name
        )
        if prof and prof.get("picture"):
            # Fix: use load_avatars (plural) not load_avatar
            ImageLoader.load_avatars(
                prof["picture"], lambda t: self.avatar.set_custom_image(t)
            )

        btn_av = Gtk.Button(css_classes=["flat"])
        btn_av.set_child(self.avatar)
        btn_av.connect("clicked", lambda b: self.main_window.show_profile(pubkey))
        hb.append(btn_av)

        nb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        nb.set_hexpand(True)
        self.lbl_name = Gtk.Label(label=name, xalign=0, css_classes=["heading"])
        nb.append(self.lbl_name)

        lbl_npub = Gtk.Label(
            label=pubkey[:12] + "...", xalign=0, css_classes=["caption", "dim-label"]
        )
        nb.append(lbl_npub)
        hb.append(nb)

        # Unobtrusive relative timestamp at the top-right of the header
        self.lbl_time = Gtk.Label(
            label=self._format_time(created_at),
            xalign=1,
            css_classes=["caption", "dim-label"],
        )
        self.lbl_time.set_halign(Gtk.Align.END)
        hb.append(self.lbl_time)
        self.main_box.append(hb)

        # Content
        try:
            rendered_content = ContentRenderer.render(
                display_content, self.main_window, self
            )
            self.main_box.append(rendered_content)
        except Exception:
            self.main_box.append(Gtk.Label(label="[Content Error]"))

        # Footer: Metrics + social actions (like/repost/reply).
        # Each is a Gtk.Button holding an icon + count label. The card-wide
        # open-thread GestureClick must NOT fire when a footer button is tapped,
        # so each button gets its own GestureClick that CLAIMS the sequence on
        # press (same pattern as the video frame) — GTK4's cooperative
        # sequence-state check then DENIES the card's gesture for that press.
        footer = Gtk.Box(spacing=20, margin_top=8)

        def mk_met(icon, fallbacks, label):
            btn = Gtk.Button()
            b = Gtk.Box(spacing=6)
            # Resolve against the active theme so the icon never renders
            # blank on a fresh install (chat-bubble-symbolic is absent from
            # Adwaita/Phosh — that's the "broken icon" bug). The like button
            # already did this via _update_like_icon; do it here for ALL three
            # so a missing theme glyph falls back to a theme-present icon.
            img = Gtk.Image.new_from_icon_name(
                self._resolve_icon_name(icon, fallbacks)
            )
            b.append(img)
            lbl = Gtk.Label(label=label, css_classes=["caption", "dim-label"])
            b.append(lbl)
            btn.set_child(b)
            btn.set_css_classes(["flat"])
            btn.icon_img = img
            return lbl, btn

        # Reply: mail-reply-sender-symbolic is the classic curved reply arrow
        # (present in Adwaita); chat-bubble-symbolic is absent so it falls
        # back — but never blank.
        self.lbl_replies, self.btn_reply = mk_met(
            "chat-bubble-symbolic",
            ("mail-reply-sender-symbolic", "chat-message-new-symbolic"),
            "0",
        )
        self.lbl_reposts, self.btn_repost = mk_met(
            "media-playlist-repeat-symbolic",
            ("view-refresh-symbolic",),
            "0",
        )
        self.lbl_likes, self.btn_like = mk_met("star-symbolic", (), "0")

        # Zap (spec zaps-payments-v1 §2.1): hidden unless the author's cached
        # kind-0 carries an LNURL candidate AND it isn't my own post. The
        # authoritative §1.2 gate runs at click (fetch_metadata).
        my_pk = getattr(self.main_window, "pub_key", None)
        self.btn_zap = None
        try:
            from ..zaps.lnurl import zapable_profile

            author = self.main_window.db.get_profile(pubkey)
            # is_hero is NOT excluded: the thread view renders the root post
            # as a hero, and the root note is the PRIMARY zap target on Nostr.
            # Excluding it made the zap icon vanish when opening a thread
            # (regression found in feed-vs-thread parity check).
            if my_pk and pubkey != my_pk and zapable_profile(author):
                _lbl, self.btn_zap = mk_met(
                    "thunderbolt-symbolic",
                    ("battery-level-symbolic", "emblem-money-symbolic"),
                    "",
                )
        except Exception:
            self.btn_zap = None

        # Zap total chip (spec zaps-payments-v1 §1.4 deliverable 3): bolt +
        # Σ sats of VERIFIED receipts for this post; tap -> zapper list.
        # Created lazily by set_zap_total() — absent at zero, so an
        # unverified zap is indistinguishable from absent (the zaps table
        # only ever holds verified rows).
        self.lbl_zap = None
        self.btn_zap_total = None

        footer.append(self.btn_reply)
        footer.append(self.btn_repost)
        footer.append(self.btn_like)
        if self.btn_zap is not None:
            footer.append(self.btn_zap)
        self._footer_box = footer
        try:
            totals = self.main_window.db.zap_totals_for_events([event_id])
            if totals.get(event_id, 0):
                self.set_zap_total(totals[event_id] // 1000)
        except Exception:
            pass

        self.main_box.append(footer)

        # FEED-TRUNC: 'Show more' row for collapsed cards, below the footer so
        # the action buttons stay visible in the collapsed state. Flat style +
        # start alignment mirror thread_view._hero_toggle; the 44px min height
        # is the touch-target floor (Librem 5). set_content keeps widget
        # identity, so expanding never re-runs the header/footer or the
        # open-thread gesture wiring.
        self._collapse_toggle = None
        if self._collapsed:
            btn = Gtk.Button(label="Show more", css_classes=["flat"])
            btn.set_halign(Gtk.Align.START)
            btn.set_size_request(-1, 44)
            btn.connect("clicked", self._on_collapse_toggle)
            self._collapse_toggle = btn
            self.main_box.append(btn)

        # Wire the social actions (only when logged in). The hero card in a
        # thread gets its like button wired + state applied too — the thread
        # view's hero is a PostWidget(is_hero=True) and previously never
        # reflected liked state nor had a working like button. Repost/reply
        # stay off the hero (the thread view owns those controls).
        my_pk = getattr(self.main_window, "pub_key", None)
        if my_pk:
            self._liked = bool(self.main_window.db.user_reaction(event_id, my_pk))
            self._update_like_icon()
            self.btn_like.connect("clicked", self._on_like)
            if self.btn_zap is not None:
                # Zap is wired for the hero too (see creation note above):
                # the thread root must be zappable from the thread view.
                self.btn_zap.connect("clicked", self._on_zap)
            if not is_hero:
                self.btn_repost.connect("clicked", self._on_repost)
                self.btn_reply.connect("clicked", self._on_reply)
        else:
            # not logged in: disable actions
            for b in (self.btn_like, self.btn_repost, self.btn_reply):
                b.set_sensitive(False)

        # Interaction
        if not is_hero:
            ctrl = Gtk.GestureClick()
            # Slice 3 (issue #22): record the press origin so _on_card_clicked
            # can tell a swipe from a tap by travel distance. The cooldown gate
            # alone misses swipes that start where the feed can't scroll (top
            # of feed: no value-changed, no stamp, gate never engages).
            ctrl.connect(
                "pressed",
                lambda g, n, x, y: self._note_press_for_gate(x, y),
            )
            ctrl.connect(
                "released",
                lambda c, n, x, y: self._on_card_clicked(
                    event_id, pubkey, content, tags, x, y, n
                ),
            )
            self.add_controller(ctrl)

            # Long-press copies the whole post; group it with the click gesture so
            # a long-press doesn't also open the thread on release.
            lp = Gtk.GestureLongPress()
            # Lengthen the long-press timeout a bit: delay-factor multiplies the
            # gtk-long-press-time setting (~500ms default -> ~750ms), so a casual
            # tap doesn't trigger a copy.
            lp.set_delay_factor(1.5)
            lp.connect("pressed", lambda g, x, y: self._copy_post())
            # Attach to the same widget BEFORE grouping — gtk_gesture_group()
            # asserts both gestures share a widget (gtk_gesture.c:1587), and
            # lp's widget is only set once it's added to a controller host.
            self.add_controller(lp)
            ctrl.group(lp)

    def _note_press_for_gate(self, x, y):
        """Record the press origin on the feed's ScrollGate so the release
        handler can measure travel distance (slice 3, issue #22). No-op when
        no gate is wired (thread hero, tests)."""
        gate = getattr(self.main_window, "scroll_gate", None)
        if gate is not None:
            gate.note_press(x, y)

    def _activation_gated(self, gesture_node=None):
        """Return True if this card tap is inside the post-scroll cooldown AND
        originated from a touch source.

        Unlike the renderer's static helper (which only has the window ref), we
        can inspect the real click here: ``gesture_node.get_current_event_device``
        gives the device that fired the sequence, so we classify it directly
        instead of guessing off the scroll stamp. Falls back to the touch-origin
        scroll stamp when no device info is available (keeps touch devices gated).
        """
        gate = getattr(self.main_window, "scroll_gate", None)
        if gate is None or not gate.in_cooldown():
            return False
        # Prefer the actual click device; fall back to the last-scroll class.
        source = None
        try:
            if gesture_node is not None and gesture_node.get_current_event_device():
                dev = gesture_node.get_current_event_device()
                src = dev.get_source_device() if dev else None
                if src is not None:
                    source = ScrollGate.is_touch_source(src)
        except Exception:
            source = None
        if source is not None:
            return bool(source)
        # No device info — trust the scroll's recorded class so a touch feed stays
        # protected even before the last event was classified.
        return gate.last_scroll_was_touch

    def _on_card_clicked(self, event_id, pubkey, content, tags, x, y, n=None):
        # Touch-activation cooldown: during the post-scroll glide window a press
        # on the card is almost certainly a swipe, not a deliberate tap. Swallow
        # it so an incidental flick doesn't open the thread. The gate records the
        # last-scroll device class, so only touch-originated scrolls cool down
        # (a desktop wheel scroll must not block a mouse click). First statement
        # because opening the thread is cheap but spurious here.
        if self._activation_gated(n):
            print("[scroll-gate] swallowed card activation")
            return
        # Slice 3 (issue #22): geometric swipe guard. The cooldown above is
        # temporal and misses swipes that start where the feed cannot scroll
        # (top of feed, rubber-band) — no value-changed, no stamp, gate inert.
        # Travel distance is position-independent: a release >30 logical px
        # from the press is a swipe no matter what the adjustment did.
        gate = getattr(self.main_window, "scroll_gate", None)
        if gate is not None and gate.swipe_since_press(x, y):
            print("[scroll-gate] swallowed card activation (swipe travel)")
            return
        # Open the thread on a normal click, UNLESS the click landed on one of
        # the footer action buttons (like/repost/reply). Those buttons have
        # their own clicked handlers; opening the thread too would be wrong.
        # The old code added a CLAIM gesture to each action button to deny the
        # card's gesture, but that also blocked the button's own clicked signal
        # (Gtk.Button's internal click gesture lost the sequence) — so the like
        # button never fired. Checking the click target here is the fix.
        try:
            target = self.get_widget_at_coords(x, y)
        except Exception:
            target = None
        if target is not None:
            # Walk up from the target to see if it's inside an action button.
            w = target
            while w is not None:
                if w in (
                    self.btn_like,
                    self.btn_repost,
                    self.btn_reply,
                    self.btn_zap,
                    getattr(self, "btn_zap_total", None),
                    # FEED-TRUNC: the Show-more row expands in place; it must
                    # never open the thread (same skip rule as the footer
                    # buttons — do NOT add a CLAIM gesture here, it would
                    # kill the button's own clicked signal).
                    getattr(self, "_collapse_toggle", None),
                ):
                    return
                w = w.get_parent()
        # Open with the FULL content, never the collapsed display string —
        # a truncated hero would persist the cut into the thread view.
        self.main_window.show_thread(
            event_id, pubkey, self._full_content, tags
        )

    @staticmethod
    def _resolve_icon_name(name, fallbacks=()):
        """Confirm an icon resolves in the active icon theme; if not, walk a
        caller-supplied fallback list (then the generic star list) so a footer
        button never renders blank. GTK4 Gtk.IconTheme exposes has_icon();
        has_icon_pixbuf is GTK2/3.

        `fallbacks` is a tuple of theme-safe alternates, in preference order —
        each footer button passes its own (the like button relies on the
        default star list below).
        """
        theme = (
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
            if Gdk.Display.get_default()
            else None
        )
        if theme is None:
            return name
        if theme.has_icon(name):
            return name
        generic = ("emblem-favorite-symbolic", "starred-symbolic", "star-symbolic")
        for alt in (*fallbacks, *generic):
            if theme.has_icon(alt):
                return alt
        return name

    def _update_like_icon(self):
        self._liked = bool(getattr(self, "_liked", False))
        icon = self._resolve_icon_name(
            "starred-symbolic" if self._liked else "star-symbolic"
        )
        self.btn_like.icon_img.set_from_icon_name(icon)
        # Clear accent class toggles (no-op if class not present).
        self.btn_like.remove_css_class("liked")
        if self._liked:
            self.btn_like.add_css_class("liked")

    def _on_like(self, btn):
        # Touch-activation cooldown (slice 2): footer buttons have no gesture
        # device, so _activation_gated(None) falls back to the touch-origin
        # scroll stamp — a flick during the glide can't fire a reaction.
        if self._activation_gated(None):
            print("[scroll-gate] swallowed like activation")
            return
        client = self.main_window.client
        my_pk = self.main_window.pub_key
        if not my_pk:
            return
        # NIP-25: publish '+' to like, '-' to undo; flip state optimistically.
        action = "-" if self._liked else "+"
        ok = client.publish_reaction(self.event_id, self.pubkey, action)
        if not ok:
            self.main_window.add_toast(Adw.Toast(title="Failed to react"))
            return
        self._liked = not self._liked
        self._update_like_icon()
        cur = int(self.lbl_likes.get_label() or 0)
        self.lbl_likes.set_label(str(cur + (1 if self._liked else -1)))

    def _on_repost(self, btn):
        if self._activation_gated(None):
            print("[scroll-gate] swallowed repost activation")
            return
        client = self.main_window.client
        if client.publish_repost(
            self.event_id, self.pubkey, target_kind=1, original_event=None
        ):
            self.main_window.add_toast(Adw.Toast(title="Reposted"))
        else:
            self.main_window.add_toast(Adw.Toast(title="Failed to repost"))

    def _on_reply(self, btn):
        if self._activation_gated(None):
            print("[scroll-gate] swallowed reply activation")
            return
        from gnostr.dialogs import ComposeWindow

        def handle_post(text, media_tags=None):
            # Reply to this post: root = thread root, parent = this post.
            root = self._root_id or self.event_id
            root_pk = self._root_pk or self.pubkey
            ok = self.main_window.client.publish_post(
                text,
                reply_to={
                    "root": root,
                    "root_pk": root_pk,
                    "parent": self.event_id,
                    "parent_pk": self.pubkey,
                },
                extra_tags=media_tags,
            )
            if ok:
                self.main_window.add_toast(Adw.Toast(title="Reply Published"))
            else:
                self.main_window.add_toast(Adw.Toast(title="Failed to Publish Reply"))

        win = ComposeWindow(self.main_window, handle_post)
        win.present()

    def _on_zap(self, btn):
        if self._activation_gated(None):
            print("[scroll-gate] swallowed zap activation")
            return
        # Note-zap: e + k tags from this post's kind (spec §1.3).
        ev = self.main_window.db.get_event_by_id(self.event_id)
        kind = ev.get("kind", 1) if ev else 1
        from ..zaps.ui import start_zap

        start_zap(self.main_window, self.pubkey, event=(self.event_id, kind))

    def set_zap_total(self, sats):
        """Show/refresh the zap-total chip (bolt + Σ sats of verified
        receipts). sats<=0 removes the chip — no 'maybe zap' state."""
        if sats <= 0:
            if getattr(self, "btn_zap_total", None) is not None:
                self._footer_box.remove(self.btn_zap_total)
                self.btn_zap_total = None
                self.lbl_zap = None
            return
        if getattr(self, "btn_zap_total", None) is None:
            btn = Gtk.Button()
            b = Gtk.Box(spacing=6)
            img = Gtk.Image.new_from_icon_name(
                self._resolve_icon_name(
                    "thunderbolt-symbolic",
                    ("battery-level-symbolic", "emblem-money-symbolic"),
                )
            )
            b.append(img)
            lbl = Gtk.Label(label="", css_classes=["caption", "dim-label"])
            b.append(lbl)
            btn.set_child(b)
            btn.set_css_classes(["flat"])
            btn.connect("clicked", self._on_zap_total)
            self.btn_zap_total = btn
            self.lbl_zap = lbl
            self._footer_box.append(btn)
        self.lbl_zap.set_label(f"{sats:,}")

    def _on_zap_total(self, _btn):
        """Tap the zap chip -> list of zappers + amounts (spec §1.4 v1)."""
        from ..zaps.ui import ZapReceiptsDialog

        rows = self.main_window.db.zaps_for_event(self.event_id)
        ZapReceiptsDialog(self.main_window, rows).present()

    def _copy_post(self):
        display = Gdk.Display.get_default()
        clipboard = display.get_clipboard() if display else None
        if clipboard is None:
            return
        # Copy the FULL post even while the card is collapsed (FEED-TRUNC).
        provider = Gdk.ContentProvider.new_for_value(self._full_content)
        clipboard.set_content(provider)
        self.main_window.add_toast(Adw.Toast(title="Copied post"))

    def _on_collapse_toggle(self, _btn):
        """FEED-TRUNC: expand a collapsed feed card in place. Expanding swaps
        in the full content via set_content (widget identity kept — the same
        mechanism thread_view's hero toggle uses) and drops the row; the
        collapsed state is one-way per widget lifetime, matching the feed's
        never-re-render contract (widgets are reused by event id)."""
        if not self._collapsed:
            return
        self._collapsed = False
        self.set_content(self._full_content)
        btn = getattr(self, "_collapse_toggle", None)
        if btn is not None:
            self._collapse_toggle = None
            parent = btn.get_parent()
            if parent is not None:
                parent.remove(btn)

    def set_content(self, content):
        """Swap just the rendered content box (child 1, between header and
        footer). Unlike rebuilding the whole PostWidget, this keeps the same
        widget identity and avoids re-running the async render chain (image
        loads, video pipelines, profile/avatar fetch) — which is what made
        rapid Show-more/less toggles glitchy and orphan mid-load widgets.

        Gtk.Box has no index-based insert() in these bindings, so the content
        box is replaced by removing ALL children and re-appending them in
        order with the new rendered box in slot 1. Header/footer are re-appended
        as the SAME widget objects (no re-render); only the content box is new.
        """
        self.content = content
        children = []
        child = self.main_box.get_first_child()
        while child is not None:
            children.append(child)
            child = child.get_next_sibling()
        for c in children:
            self.main_box.remove(c)
        try:
            rendered = ContentRenderer.render(content, self.main_window, self)
        except Exception:
            rendered = Gtk.Label(label="[Content Error]")
        # Rebuild in order: header(0), rendered content(1), footer(2+).
        self.main_box.append(children[0])
        self.main_box.append(rendered)
        for c in children[2:]:
            self.main_box.append(c)

    @staticmethod
    def insert_time_sorted(box, widget):
        """Insert `widget` into `box` keeping newest-at-top order (descending
        created_at). Children must be PostWidgets. Used for LIVE insertions so
        a new/backfilled post slots into the correct time position instead of
        blindly appending.

        GTK4 Gtk.Box exposes no index-based insert (Gtk3's gtk_box_insert and
        gtk_box_reorder_child don't exist here), so this rebuilds the box using
        only the guaranteed append/remove primitives: collect current children,
        splice the new widget at the right index, then re-append in order.
        """
        created_at = getattr(widget, "created_at", None)
        # Collect current children in display order (newest-at-top).
        ordered = []
        child = box.get_first_child()
        while child is not None:
            ordered.append(child)
            child = child.get_next_sibling()

        if created_at is not None:
            # Find the first child older than the new post — insert before it.
            insert_idx = len(ordered)
            for i, c in enumerate(ordered):
                ca = getattr(c, "created_at", None)
                if ca is not None and ca < created_at:
                    insert_idx = i
                    break
            ordered.insert(insert_idx, widget)
        else:
            ordered.append(widget)

        # Remove all (only actual children — the new widget isn't a child yet,
        # and removing a non-child raises gtk_box_remove's parent assertion),
        # then re-append in the new sorted order.
        for c in ordered:
            if c.get_parent() is box:
                box.remove(c)
        for c in ordered:
            box.append(c)

    @staticmethod
    def _format_time(ts):
        """Compact, unobtrusive relative timestamp: now / Nm / Nh / Nd, then a date."""
        if not ts:
            return ""
        try:
            import time as _time

            diff = _time.time() - float(ts)
            if diff < 60:
                return "now"
            mins = int(diff / 60)
            if mins < 60:
                return f"{mins}m"
            hours = int(diff / 3600)
            if hours < 24:
                return f"{hours}h"
            days = int(diff / 86400)
            if days < 30:
                return f"{days}d"
            import datetime as _dt

            return _dt.datetime.fromtimestamp(int(ts), _dt.timezone.utc).strftime(
                "%b %d"
            )
        except Exception:
            return ""


class TrustStubWidget(Adw.Bin):
    """trust-shield-v1 §3.1: collapsed card for a low-rank author.

    Identity stays visible (avatar + name — no shadow-banning), content is
    replaced by the first line only, a 'Low rank' pill explains why, and
    'Show post' reveals instantly (content is already hydrated). Deliberately
    NOT a PostWidget: no gestures, no footer, no media — the point is a
    visually quieter card. Registers in main_window.event_widgets like
    PostWidget so feed bookkeeping (dedupe, eviction, time-sorted insert)
    treats both uniformly; the metrics/zap handlers skip it via hasattr.
    """

    def __init__(self, main_window, pubkey, content, event_id, created_at=None):
        super().__init__(css_classes=["card"])
        self.main_window = main_window
        self.pubkey = pubkey
        self.event_id = event_id
        self.is_trust_stub = True
        if created_at is None:
            ev = main_window.db.get_event_by_id(event_id)
            created_at = ev.get("created_at") if ev else None
        self.created_at = created_at
        main_window.event_widgets[event_id] = self

        self.main_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        self.set_child(self.main_box)

        # Identity row: avatar + name + Low-rank pill
        hb = Gtk.Box(spacing=12)
        prof = main_window.db.get_profile(pubkey)
        if not prof:
            main_window.client.fetch_profile(pubkey)
        name = (prof or {}).get("display_name") or (prof or {}).get("name") or pubkey[:8]
        self.avatar = Adw.Avatar(size=40, show_initials=True, text=name)
        if prof and prof.get("picture"):
            ImageLoader.load_avatars(
                prof["picture"], lambda t: self.avatar.set_custom_image(t)
            )
        hb.append(self.avatar)
        nb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        nb.set_hexpand(True)
        nb.append(Gtk.Label(label=name, xalign=0, css_classes=["heading"]))
        hb.append(nb)
        hb.append(
            Gtk.Label(label="Low rank", css_classes=["caption", "pill-low-trust"])
        )
        self.main_box.append(hb)

        # First line of content only (§3.1), single-line ellipsized.
        first = (content or "").splitlines()[0] if content else ""
        first = collapse_util.safe_truncate(first, 120) if first else ""
        lbl = Gtk.Label(label=first or "[hidden]", xalign=0)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        lbl.set_max_width_chars(48)
        lbl.add_css_class("dim-label")
        self.main_box.append(lbl)

        show = Gtk.Button(label="Show post", css_classes=["flat"])
        show.set_halign(Gtk.Align.START)
        show.connect(
            "clicked", lambda _b: self.main_window.trust_reveal(self.pubkey)
        )
        self.main_box.append(show)
