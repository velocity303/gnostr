# src/gnostr/ui/dm_recipient_view.py
"""New Message recipient picker (spec: docs/specs/
dm-initiation-and-feed-freshness.md Phase A2).

Layout: back row -> Gtk.SearchEntry ("Search follows, or paste npub /
hex") -> ScrolledWindow -> AdwClamp(720/504) -> Gtk.ListBox (boxed-list,
single-click activate). Rows are Adw.ActionRow (64px, 40px avatar,
name + short npub) built from db.get_following_list(own pubkey); each
row kicks client.fetch_profile so names/avatars fill in live.

Filter (view-local, no relay search — NIP-50 is out of scope):
case-insensitive substring on display name / npub. A pasted Nostr
identifier (hex / npub / nprofile / nevent-author) resolves via
nostr_utils.resolve_profile_identifier and pins a synthetic first row
"Message <npub…>" -> show_dm_thread(pk). An nsec is NOT a recipient
(DM-to-self is a no-op): the resolver maps it to our own pubkey and we
drop it. NIP-05 addresses resolve only when P2 (lookup_nip05) lands;
until then the paste path is hex/npub only.

Empty follows: Adw.StatusPage prompting to follow people or paste an
npub. Row tap -> main_window.show_dm_thread(pk); the picker stays under
the thread in the NavigationView stack.

A5 contract: no reachability gating here — the async kind-10050 lookup
inside client.publish_dm is the single decision point; protocol logic
stays out of widgets.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, Pango

from ..nostr_utils import hex_to_npub, resolve_profile_identifier
from .icons import resolve_icon

ROW_MIN_HEIGHT = 64
AVATAR_SIZE = 40
DM_ICON_CANDIDATES = (
    "mail-message-new-symbolic",
    "chat-message-new-symbolic",
    "list-add-symbolic",
)


class DmRecipientView(Gtk.Box):
    def __init__(self, main_window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.main_window = main_window
        self._all_rows = []  # (pk, name, npub) dataset, filter source
        self._pinned_pk = None  # synthetic identifier row (paste hit)
        self.list_rows = []

        # Toolbar row (house style: plain Gtk.Box upper-left).
        back_row = Gtk.Box(spacing=12, halign=Gtk.Align.START)
        btn_back = Gtk.Button(icon_name="go-previous-symbolic", css_classes=["flat"])
        btn_back.set_tooltip_text("Back")
        btn_back.connect("clicked", lambda b: main_window.content_nav.pop())
        back_row.append(btn_back)
        self.append(back_row)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search follows, or paste npub / hex")
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_entry.connect("activate", self._on_entry_activate)
        self.append(self.search_entry)

        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_box.set_activate_on_single_click(True)
        self.list_box.add_css_class("boxed-list")
        self.list_box.connect("row-activated", self._on_row_activated)

        self.clamp = Adw.Clamp()
        self.clamp.set_maximum_size(720)
        self.clamp.set_tightening_threshold(504)
        self.clamp.set_child(self.list_box)

        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_vexpand(True)
        self.scroll.set_child(self.clamp)
        self.append(self.scroll)

        self.empty_page = None  # Adw.StatusPage when the follows list is empty

        self.reload()

    # ------------------------------------------------------------------
    # data
    # ------------------------------------------------------------------

    def reload(self):
        """Build the dataset from the owner's follows table."""
        pk = self.main_window.pub_key
        self._all_rows = []
        if pk:
            for peer in self.main_window.db.get_following_list(pk):
                prof = self.main_window.db.get_profile(peer) or {}
                name = prof.get("display_name") or prof.get("name") or peer[:8]
                npub = hex_to_npub(peer) or peer
                self._all_rows.append((peer, name, npub))
                # Kick off metadata fetch (TTL-cached) so names fill in live.
                self.main_window.client.fetch_profile(peer)
        self._render()

    def _resolve_paste(self, text):
        """Identifier paste -> recipient pubkey, or None.

        Decision table (spec A2): hex / npub / nprofile / nevent-author
        resolve via resolve_profile_identifier; nsec resolves to OUR OWN
        pubkey and is rejected (DM-to-self is a no-op); anything else —
        including a NIP-05 address — is None until P2 lands.
        """
        pk = resolve_profile_identifier(text)
        if pk and pk == self.main_window.pub_key:
            return None  # nsec / own npub: never a recipient
        return pk

    def _matches(self, entry, query):
        _pk, name, npub = entry
        return query in name.lower() or query in npub.lower()

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------

    def _render(self):
        for r in self.list_rows:
            self.list_box.remove(r)
        self.list_rows = []

        if self.empty_page is not None:
            self.scroll.set_child(self.clamp)
            self.empty_page = None

        query = (self.search_entry.get_text() or "").strip().lower()
        pinned = None
        if query:
            pinned = self._resolve_paste(query)
        if pinned:
            row = self._make_pinned_row(pinned)
            self.list_box.append(row)
            self.list_rows.append(row)

        visible = [e for e in self._all_rows if not query or self._matches(e, query)]
        for pk, name, npub in visible:
            if pinned and pk == pinned:
                continue  # pinned row already represents this peer
            row = self._make_row(pk, name, npub)
            self.list_box.append(row)
            self.list_rows.append(row)

        if not self.list_rows:
            if not self._all_rows:
                self._show_empty_follows()
            else:
                empty = Adw.ActionRow(title="No matches", activatable=False)
                empty.add_css_class("dim-label")
                self.list_box.append(empty)
                self.list_rows.append(empty)

    def _show_empty_follows(self):
        self.empty_page = Adw.StatusPage(
            icon_name="system-users-symbolic",
            title="No follows yet",
            description="Follow some people, or paste an npub above to start a conversation.",
        )
        self.scroll.set_child(self.empty_page)

    def _make_row(self, pk, name, npub):
        row = Adw.ActionRow(title=name, subtitle=f"{npub[:16]}…")
        row.set_activatable(True)
        row.set_size_request(-1, ROW_MIN_HEIGHT)
        row.pubkey = pk

        avatar = Adw.Avatar(size=AVATAR_SIZE, text=name, show_initials=True)
        prof = self.main_window.db.get_profile(pk) or {}
        if prof.get("picture"):
            from ..renderer import ContentRenderer, ImageLoader

            if not ContentRenderer.is_video_url(prof["picture"]):
                ImageLoader.load_avatars(prof["picture"], avatar.set_custom_image)
        row.add_prefix(avatar)
        return row

    def _make_pinned_row(self, pk):
        npub = hex_to_npub(pk) or pk
        row = self._make_row(pk, f"Message {npub[:16]}…", npub)
        row.add_suffix(Gtk.Image.new_from_icon_name(resolve_icon(DM_ICON_CANDIDATES)))
        return row

    # ------------------------------------------------------------------
    # signals
    # ------------------------------------------------------------------

    def _on_search_changed(self, entry):
        self._render()

    def _on_entry_activate(self, entry):
        """Enter with a resolved identifier in the box goes straight to the
        thread (the pinned row is the same target)."""
        pk = self._resolve_paste(entry.get_text())
        if pk:
            self.main_window.show_dm_thread(pk)

    def _on_row_activated(self, list_box, row):
        pk = getattr(row, "pubkey", None)
        if pk:
            self.main_window.show_dm_thread(pk)
