# src/ui/followers_list_view.py
"""Follows list — who an owner follows. Reusable for the user's own
pubkey (sidebar "Follows" row, is_own=True, carries the Sync button)
and any foreign profile (tappable "Following N" in ProfileView).

Data: db.get_following_list(owner_pubkey) — the following table is
owner-keyed, and client._handle_event reconciles ANY author's kind-3
into that owner's rows, so foreign lists populate without extra schema.
Refresh: client.fetch_contacts_for(owner_pubkey); when the author's
kind-3 lands the client emits contacts-updated(pubkey) and this view
reloads if it matches. Row tap opens that user's profile.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from ..nostr_utils import hex_to_npub


class FollowersListView(Gtk.Box):
    def __init__(self, main_window, owner_pubkey, is_own=False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.main_window = main_window
        self.owner_pubkey = owner_pubkey
        self.is_own = is_own
        self.list_rows = []

        # Toolbar row (house style: plain Gtk.Box, upper-left, like
        # ProfileView's back/refresh row).
        back_row = Gtk.Box(spacing=12, halign=Gtk.Align.START)
        btn_back = Gtk.Button(icon_name="go-previous-symbolic", css_classes=["flat"])
        btn_back.set_tooltip_text("Back")
        btn_back.connect("clicked", lambda b: main_window.content_nav.pop())
        back_row.append(btn_back)

        self.btn_refresh = Gtk.Button(
            icon_name="view-refresh-symbolic", css_classes=["flat"]
        )
        self.btn_refresh.set_tooltip_text("Fetch follows from relays")
        self.btn_refresh.connect("clicked", lambda b: self.refresh())
        back_row.append(self.btn_refresh)

        self.btn_sync = None
        if is_own:
            self.btn_sync = Gtk.Button(
                icon_name="network-transmit-receive-symbolic", css_classes=["flat"]
            )
            self.btn_sync.set_tooltip_text("Push follows to relays")
            self.btn_sync.connect("clicked", lambda b: main_window.sync_follows())
            back_row.append(self.btn_sync)

        self.append(back_row)

        self.title_label = Gtk.Label(label="", xalign=0, css_classes=["heading"])
        self.append(self.title_label)

        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_box.set_activate_on_single_click(True)
        self.list_box.connect("row-activated", self._on_row_activated)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(self.list_box)
        self.append(scroll)

        # Self-refresh when the owner's kind-3 reconciles (any-author pull).
        main_window.client.connect("contacts-updated", self._on_contacts_updated)

        self.reload()

    def _display_name(self):
        prof = self.main_window.db.get_profile(self.owner_pubkey) or {}
        return prof.get("name") or prof.get("display_name") or self.owner_pubkey[:8]

    def _title(self, count=None):
        base = (
            "You're following"
            if self.is_own
            else f"{self._display_name()} is following"
        )
        return f"{base} — {count}" if count is not None else base

    def reload(self):
        for r in self.list_rows:
            self.list_box.remove(r)
        self.list_rows = []
        pks = self.main_window.db.get_following_list(self.owner_pubkey)
        for pk in pks:
            row = self._make_row(pk)
            self.list_box.append(row)
            self.list_rows.append(row)
        self.title_label.set_label(self._title(len(pks)))

    def _make_row(self, pk):
        prof = self.main_window.db.get_profile(pk) or {}
        name = prof.get("name") or prof.get("display_name") or pk[:8]
        row = Adw.ActionRow(title=name, subtitle=hex_to_npub(pk) or pk)
        avatar = Adw.Avatar(size=32, text=name, show_initials=True)
        if prof.get("picture"):
            # Static images only — animated profile pics stay in ProfileView.
            from ..renderer import ContentRenderer, ImageLoader

            if not ContentRenderer.is_video_url(prof["picture"]):
                ImageLoader.load_avatars(prof["picture"], avatar.set_custom_image)
        row.add_prefix(avatar)
        row.set_activatable(True)
        row.pubkey = pk
        # Kick off metadata fetch (TTL-cached) so name/avatar fill in on arrival.
        self.main_window.client.fetch_profile(pk)
        return row

    def _on_row_activated(self, list_box, row):
        self.main_window.show_profile(row.pubkey)

    def _on_contacts_updated(self, client, pubkey):
        if pubkey == self.owner_pubkey:
            self.reload()

    def refresh(self):
        self.main_window.client.fetch_contacts_for(self.owner_pubkey)
