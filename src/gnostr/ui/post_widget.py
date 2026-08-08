# src/gnostr/ui/post_widget.py
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Pango, Gdk
from ..renderer import ContentRenderer, ImageLoader


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
    ):
        super().__init__(css_classes=["card"])
        self.main_window = main_window
        self.pubkey = pubkey
        self.event_id = event_id
        self.content = content
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
            rendered_content = ContentRenderer.render(content, self.main_window, self)
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

        def mk_met(icon, label):
            btn = Gtk.Button()
            b = Gtk.Box(spacing=6)
            img = Gtk.Image.new_from_icon_name(icon)
            b.append(img)
            l = Gtk.Label(label=label, css_classes=["caption", "dim-label"])
            b.append(l)
            btn.set_child(b)
            btn.set_css_classes(["flat"])
            # Keep the icon image reference on the button so _update_like_icon
            # can call set_from_icon_name directly (no fragile child-walk).
            btn.icon_img = img
            return l, btn

        self.lbl_replies, self.btn_reply = mk_met("chat-bubble-symbolic", "0")
        self.lbl_reposts, self.btn_repost = mk_met(
            "media-playlist-repeat-symbolic", "0"
        )
        self.lbl_likes, self.btn_like = mk_met("star-symbolic", "0")

        footer.append(self.btn_reply)
        footer.append(self.btn_repost)
        footer.append(self.btn_like)

        self.main_box.append(footer)

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
            ctrl.connect(
                "released",
                lambda c, n, x, y: self._on_card_clicked(
                    event_id, pubkey, content, tags, x, y
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

    def _on_card_clicked(self, event_id, pubkey, content, tags, x, y):
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
                if w in (self.btn_like, self.btn_repost, self.btn_reply):
                    return
                w = w.get_parent()
        self.main_window.show_thread(event_id, pubkey, content, tags)

    @staticmethod
    def _resolve_icon_name(name):
        """Confirm an icon resolves in the active icon theme; if not, walk a
        known-good fallback list so the like button never renders blank.
        GTK4 Gtk.IconTheme exposes has_icon(); has_icon_pixbuf is GTK2/3."""
        theme = (
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
            if Gdk.Display.get_default()
            else None
        )
        if theme is None:
            return name
        if theme.has_icon(name):
            return name
        for alt in ("emblem-favorite-symbolic", "starred-symbolic", "star-symbolic"):
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
        client = self.main_window.client
        if client.publish_repost(
            self.event_id, self.pubkey, target_kind=1, original_event=None
        ):
            self.main_window.add_toast(Adw.Toast(title="Reposted"))
        else:
            self.main_window.add_toast(Adw.Toast(title="Failed to repost"))

    def _on_reply(self, btn):
        from gnostr.dialogs import ComposeWindow

        def handle_post(text):
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
            )
            if ok:
                self.main_window.add_toast(Adw.Toast(title="Reply Published"))
            else:
                self.main_window.add_toast(Adw.Toast(title="Failed to Publish Reply"))

        win = ComposeWindow(self.main_window, handle_post)
        win.present()

    def _copy_post(self):
        display = Gdk.Display.get_default()
        clipboard = display.get_clipboard() if display else None
        if clipboard is None:
            return
        provider = Gdk.ContentProvider.new_for_value(self.content)
        clipboard.set_content(provider)
        self.main_window.add_toast(Adw.Toast(title="Copied post"))

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
