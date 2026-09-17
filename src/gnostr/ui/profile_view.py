import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, GLib
from .post_widget import PostWidget
from ..renderer import ContentRenderer, ImageLoader, VideoPlayer
from ..nostr_utils import hex_to_npub
from .. import profile_nips


class ProfileView(Adw.Bin):
    def __init__(self, main_window, pubkey):
        super().__init__(css_classes=["card"])
        self.main_window = main_window
        self.pubkey = pubkey

        # The WHOLE page scrolls (banner, header, follow button, posts) in one
        # ScrolledWindow — field bug: a large banner inside a non-scrolling
        # header pushed the Follow button and posts off-screen on portrait
        # phones with no way to reach them.
        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroll.set_hscrollbar_policy(Gtk.PolicyType.NEVER)
        self.set_child(self._scroll)

        self.layout = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        self._scroll.set_child(self.layout)

        # Header with back button, avatar, npub
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        header.set_halign(Gtk.Align.CENTER)

        # Back button row
        back_row = Gtk.Box(spacing=12, halign=Gtk.Align.START)
        btn_back = Gtk.Button(icon_name="go-previous-symbolic", css_classes=["flat"])
        btn_back.set_tooltip_text("Back")
        btn_back.connect("clicked", lambda b: self.main_window.content_nav.pop())
        back_row.append(btn_back)

        btn_refresh = Gtk.Button(
            icon_name="view-refresh-symbolic", css_classes=["flat"]
        )
        btn_refresh.set_tooltip_text("Refresh Profile")
        btn_refresh.connect(
            "clicked", lambda b: self.main_window.refresh_profile(self.pubkey)
        )
        back_row.append(btn_refresh)
        # Toolbar row lives at the top of the layout (upper-left), not inside the
        # centered header, so back/refresh align with the app header.
        self.layout.append(back_row)

        prof = self.main_window.db.get_profile(pubkey)
        name = (
            prof.get("display_name") or prof.get("name") or pubkey[:8]
            if prof
            else pubkey[:8]
        )

        # Banner — wide background image at the top (NIP-24 `banner`, ~1024x768).
        # Height-capped: portrait/oversized banners used to eat the whole screen
        # and bury the Follow button below the fold. COVER crops to fill the
        # capped slot instead of scaling the full image down the page.
        if prof and prof.get("banner"):
            self.banner_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.banner_container.set_size_request(-1, 160)
            self.banner_container.set_halign(Gtk.Align.FILL)
            self.layout.append(self.banner_container)
            ImageLoader.load_image_into_widget(
                prof["banner"], self.banner_container, None, max_height=200
            )

        # Avatar - supports animated GIFs/videos
        self.avatar_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.avatar_container.set_size_request(120, 120)
        self.avatar_container.set_halign(Gtk.Align.CENTER)

        # Default: show avatar
        self.avatar = Adw.Avatar(size=120, show_initials=True, text=name)
        self.avatar_container.append(self.avatar)
        header.append(self.avatar_container)

        if prof and prof.get("picture"):
            picture_url = prof["picture"]
            # Check if it's animated (gif/webm)
            if ContentRenderer.is_video_url(picture_url):
                # Show video player for animated media
                self.avatar_container.remove(self.avatar)
                VideoPlayer.load_and_play(
                    picture_url, self.avatar_container, None, autoplay=True
                )
            else:
                # Show static image via avatar
                ImageLoader.load_avatars(
                    picture_url, lambda t: self.avatar.set_custom_image(t)
                )

        # Username (big bold)
        lbl_name = Gtk.Label(label=name, xalign=0.5, css_classes=["heading", "large"])
        header.append(lbl_name)

        # Npub with copy button
        npub = hex_to_npub(pubkey) or pubkey
        npub_box = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        lbl_npub = Gtk.Label(label=npub, xalign=0, css_classes=["caption", "dim-label"])
        npub_box.append(lbl_npub)

        btn_copy = Gtk.Button(icon_name="edit-copy-symbolic", css_classes=["flat"])
        btn_copy.set_tooltip_text("Copy npub")
        btn_copy.connect("clicked", lambda b: self.copy_to_clipboard(npub))
        npub_box.append(btn_copy)

        header.append(npub_box)

        # NIP-05 identity verification (async — never blocks the UI)
        if prof and prof.get("nip05"):
            self.nip05_label = Gtk.Label(label="", css_classes=["caption", "dim-label"])
            self.nip05_label.set_halign(Gtk.Align.CENTER)
            header.append(self.nip05_label)
            self._verify_nip05_async(prof["nip05"])

        # Bio / about (NIP-01 `about`)
        if prof and prof.get("about"):
            lbl_about = Gtk.Label(
                label=prof["about"], xalign=0.5, wrap=True, css_classes=["body"]
            )
            lbl_about.set_halign(Gtk.Align.CENTER)
            header.append(lbl_about)

        # Website link (NIP-24 `website`)
        if prof and prof.get("website"):
            website = prof["website"]
            btn_website = Gtk.Button(label=website, css_classes=["flat", "accent"])
            btn_website.set_halign(Gtk.Align.CENTER)
            btn_website.connect("clicked", lambda b: self._open_url(website))
            header.append(btn_website)

        # Lightning address (LUD-16 `lud16`)
        if prof and prof.get("lud16"):
            lud16 = prof["lud16"]
            lud16_row = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
            bolt = Gtk.Image(icon_name="emblem-important-symbolic")
            lud16_row.append(bolt)
            lbl_lud16 = Gtk.Label(label=lud16, css_classes=["caption", "dim-label"])
            lud16_row.append(lbl_lud16)
            header.append(lud16_row)

        # NIP-39 external identities (GitHub/Twitter/etc.)
        self.identities_box = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        header.append(self.identities_box)
        self._render_external_identities()

        # NIP-58 badges row
        self.badges_box = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        header.append(self.badges_box)
        self._render_badges()

        # Following count — tappable, opens the Follows list for this pubkey
        # (own or foreign — the list is owner-keyed).
        following = self.main_window.db.get_following_list(pubkey)
        btn_following = Gtk.Button(
            label=f"Following {len(following)}", css_classes=["flat"]
        )
        btn_following.set_halign(Gtk.Align.CENTER)
        btn_following.connect(
            "clicked", lambda b: self.main_window.show_follows(pubkey)
        )
        header.append(btn_following)

        # Follow/Unfollow toggle — hidden on your own profile
        my_pubkey = self.main_window.pub_key
        if my_pubkey and pubkey != my_pubkey:
            self._following = pubkey in self.main_window.db.get_following_list(
                my_pubkey
            )
            self.btn_follow = Gtk.Button(
                label="Unfollow" if self._following else "Follow",
                css_classes=["pill", "suggested-action"],
            )
            self.btn_follow.set_halign(Gtk.Align.CENTER)
            self.btn_follow.connect("clicked", lambda b: self.toggle_follow())
            header.append(self.btn_follow)
        elif my_pubkey and pubkey == my_pubkey:
            # Own profile — Edit Profile + Sync Follows instead of Follow
            # toggle. Sync re-publishes the DB following list (kind-3) —
            # the behavior the old sidebar 'Follow Sync' row had.
            btn_edit = Gtk.Button(
                label="Edit Profile", css_classes=["pill", "suggested-action"]
            )
            btn_edit.set_halign(Gtk.Align.CENTER)
            btn_edit.connect("clicked", lambda b: self.open_edit_dialog())
            header.append(btn_edit)

            btn_sync = Gtk.Button(label="Sync Follows", css_classes=["pill"])
            btn_sync.set_halign(Gtk.Align.CENTER)
            btn_sync.set_tooltip_text("Push follows to relays")
            btn_sync.connect("clicked", lambda b: self.main_window.sync_follows())
            header.append(btn_sync)

        self.layout.append(header)

        # Posts List — plain box; the whole page already scrolls in _scroll.
        # (A nested ScrolledWindow here fought the outer one for touch events.)
        self.posts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        c = Adw.Clamp(maximum_size=600)
        c.set_child(self.posts_box)
        self.layout.append(c)

        # Load posts from DB
        posts = self.main_window.db.get_feed_for_user(pubkey)
        for ev in posts:
            w = PostWidget(
                self.main_window,
                ev["pubkey"],
                ev["content"],
                ev["id"],
                ev.get("tags", []),
                is_hero=False,
            )
            self.posts_box.append(w)

        # Fetch missing NIP-39/58 data on open
        self.main_window.client.fetch_external_identities(pubkey)
        self.main_window.client.fetch_badges(pubkey)

    def _verify_nip05_async(self, nip05):
        """Verify a NIP-05 identifier off the UI thread, then update the label."""
        import threading

        def work():
            ok = profile_nips.verify_nip05(self.pubkey, nip05)
            GLib.idle_add(self._set_nip05_result, ok, nip05)

        threading.Thread(target=work, daemon=True).start()

    def _set_nip05_result(self, ok, nip05):
        if ok:
            self.nip05_label.set_label(f"✓ {nip05}")
            self.nip05_label.set_css_classes(["caption", "success"])
        else:
            self.nip05_label.set_label(f"{nip05} (unverified)")
        return False

    def _render_external_identities(self):
        """Render NIP-39 external identities as small link buttons."""
        ids = self.main_window.db.get_external_identities(self.pubkey)
        for ident in ids:
            if not isinstance(ident, dict):
                continue
            platform = ident.get("platform", "")
            identity = ident.get("identity", "")
            url = ident.get("url")
            label = f"{platform}:{identity}" if identity else platform
            btn = Gtk.Button(label=label, css_classes=["flat", "caption"])
            if url:
                btn.connect("clicked", lambda b, u=url: self._open_url(u))
            self.identities_box.append(btn)

    def _render_badges(self):
        """Render NIP-58 profile badges. Resolves kind-30009 definitions for
        image URLs when cached; otherwise shows a placeholder count."""
        badges = self.main_window.db.get_profile_badges(self.pubkey)
        if not badges:
            return
        for pair in badges:
            if not isinstance(pair, list) or len(pair) < 2:
                continue
            coord = pair[0]
            definition = self.main_window.db.get_badge_definition(coord)
            if definition and definition.get("image"):
                ImageLoader.load_avatars(
                    definition["image"],
                    lambda t, d=definition: self._append_badge_image(t, d),
                )
            else:
                # No cached definition — show a placeholder chip
                chip = Gtk.Label(label="🏅", css_classes=["caption"])
                self.badges_box.append(chip)

    def _append_badge_image(self, texture, definition):
        if texture:
            img = Gtk.Image.new_from_paintable(texture)
            img.set_size_request(48, 48)
            img.set_tooltip_text(definition.get("name", ""))
            self.badges_box.append(img)
        return False

    def _open_url(self, url):
        try:
            Gtk.show_uri(self.main_window, url, 0)
        except Exception:
            pass

    def copy_to_clipboard(self, text):
        display = Gdk.Display.get_default()
        if display is None:
            return
        clipboard = display.get_clipboard()
        if clipboard is None:
            return
        provider = Gdk.ContentProvider.new_for_value(text)
        clipboard.set_content(provider)
        toast = Adw.Toast(title="Copied npub")
        self.main_window.toast_overlay.add_toast(toast)

    def toggle_follow(self):
        self._following = not self._following
        self.btn_follow.set_label("Unfollow" if self._following else "Follow")
        self.main_window.db.set_following(
            self.main_window.pub_key, self.pubkey, self._following
        )
        self.main_window.client.publish_contacts()
        self.main_window.add_toast(
            Adw.Toast(title="Following" if self._following else "Unfollowed")
        )

    def open_edit_dialog(self):
        """Open the Edit Profile dialog for the user's own profile."""
        from ..dialogs import EditProfileDialog

        prof = self.main_window.db.get_profile(self.pubkey) or {}
        dialog = EditProfileDialog(self.main_window.client, self.main_window, prof)
        dialog.present()
