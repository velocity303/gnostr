# src/gnostr/ui/dm_inbox_view.py
"""DM inbox — the Messages page (spec: docs/specs/nip44-nip17-dms.md, UI
contract from @linux-desktop-researcher).

Layout: AdwClamp -> GtkListBox of Adw.ActionRow (64px min-height, 40px
avatar, name + short npub subtitle, last-message preview, relative
timestamp, unread dot). Rows activate into DMThreadView via
main_window.show_dm_thread.

Data: the DM gateway only (client.dm_gateway.get_conversations /
total_unread) — never crypto, never the UI thread doing decryption. Live
refresh rides the client's `dm-conversations-updated` signal (emitted after
the gateway persists a rumor), so the inbox updates when a DM arrives.

Skeleton: 3 fixed 64px shimmer rows shown until the FIRST gateway query
result arrives — removed on that result, never on a timer (contract).
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, Pango

from ..nostr_utils import hex_to_npub

SKELETON_ROWS = 3
ROW_MIN_HEIGHT = 64
AVATAR_SIZE = 40


def format_relative_time(ts):
    """Compact relative timestamp (now / Nm / Nh / Nd, then a date) — same
    caption style PostWidget uses for feed cards."""
    if not ts:
        return ""
    try:
        import datetime as _dt
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
        return _dt.datetime.fromtimestamp(int(ts), _dt.timezone.utc).strftime("%b %d")
    except Exception:
        return ""


class DMInboxView(Gtk.Box):
    def __init__(self, main_window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.main_window = main_window
        self.list_rows = []
        self._skeleton_rows = []
        self._gateway_result_seen = False

        # Toolbar row (house style: plain Gtk.Box upper-left, like
        # FollowersListView / ProfileView).
        back_row = Gtk.Box(spacing=12, halign=Gtk.Align.START)
        btn_back = Gtk.Button(icon_name="go-previous-symbolic", css_classes=["flat"])
        btn_back.set_tooltip_text("Back")
        btn_back.connect("clicked", lambda b: main_window.content_nav.pop())
        back_row.append(btn_back)
        self.btn_refresh = Gtk.Button(
            icon_name="view-refresh-symbolic", css_classes=["flat"]
        )
        self.btn_refresh.set_tooltip_text("Reload conversations")
        self.btn_refresh.connect("clicked", lambda b: self.reload())
        back_row.append(self.btn_refresh)
        self.append(back_row)

        self.title_label = Gtk.Label(
            label="Messages", xalign=0, css_classes=["heading"]
        )
        self.append(self.title_label)

        # ListBox inside an AdwClamp (contract) inside a ScrolledWindow.
        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_box.set_activate_on_single_click(True)
        self.list_box.add_css_class("boxed-list")
        self.list_box.connect("row-activated", self._on_row_activated)

        self.clamp = Adw.Clamp()
        self.clamp.set_maximum_size(720)
        self.clamp.set_tightening_ratio(0.7)
        self.clamp.set_child(self.list_box)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(self.clamp)
        self.append(scroll)

        self._append_skeleton()

        # Live refresh: the client emits dm-conversations-updated after the
        # gateway persists a rumor (inbound or our own outbound self-copy).
        main_window.client.connect("dm-conversations-updated", self._on_dm_updated)

        self.reload()

    # ------------------------------------------------------------------
    # skeleton
    # ------------------------------------------------------------------

    def _append_skeleton(self):
        """3 fixed 64px shimmer rows (no per-row GtkSpinner — contract)."""
        for _ in range(SKELETON_ROWS):
            row = Adw.ActionRow()
            row.set_activatable(False)
            bar = Gtk.Box(css_classes=["dm-skeleton"])
            bar.set_size_request(-1, ROW_MIN_HEIGHT - 16)
            bar.set_vexpand(False)
            row.set_child(bar)
            row.set_size_request(-1, ROW_MIN_HEIGHT)
            self.list_box.append(row)
            self._skeleton_rows.append(row)

    def _remove_skeleton(self):
        for row in self._skeleton_rows:
            self.list_box.remove(row)
        self._skeleton_rows = []

    # ------------------------------------------------------------------
    # data
    # ------------------------------------------------------------------

    def _gateway(self):
        return getattr(self.main_window.client, "dm_gateway", None)

    def reload(self):
        """Query the gateway and rebuild rows. The skeleton is removed on the
        FIRST gateway result — never on a timer (contract)."""
        gw = self._gateway()
        if gw is None:
            # No gateway yet (not logged in / no DMs seen): keep the skeleton
            # rather than pretending an empty inbox is a real result.
            return
        convs = gw.get_conversations(50)
        self._gateway_result_seen = True
        self._remove_skeleton()
        for r in self.list_rows:
            self.list_box.remove(r)
        self.list_rows = []

        for conv in convs:
            row = self._make_row(conv)
            self.list_box.append(row)
            self.list_rows.append(row)

        if not convs:
            empty = Adw.ActionRow(title="No messages yet", activatable=False)
            empty.add_css_class("dim-label")
            self.list_box.append(empty)
            self.list_rows.append(empty)

        self.main_window.update_dm_badge()

    def _make_row(self, conv):
        pk = conv["peer_pk"]
        prof = self.main_window.db.get_profile(pk) or {}
        name = prof.get("display_name") or prof.get("name") or pk[:8]
        npub = hex_to_npub(pk) or pk
        row = Adw.ActionRow(title=name, subtitle=f"{npub[:16]}…")
        row.set_activatable(True)
        row.set_size_request(-1, ROW_MIN_HEIGHT)
        row.pubkey = pk

        avatar = Adw.Avatar(size=AVATAR_SIZE, text=name, show_initials=True)
        if prof.get("picture"):
            from ..renderer import ContentRenderer, ImageLoader

            if not ContentRenderer.is_video_url(prof["picture"]):
                ImageLoader.load_avatars(prof["picture"], avatar.set_custom_image)
        row.add_prefix(avatar)

        # Unread dot (prefix, after the avatar) — only for unread conversations.
        if conv.get("unread_count"):
            dot = Gtk.Label(label="●")
            dot.add_css_class("dm-unread-dot")
            dot.set_valign(Gtk.Align.CENTER)
            row.add_prefix(dot)

        preview = Gtk.Label(label=(conv.get("preview") or "")[:80])
        preview.add_css_class("dim-label")
        preview.set_ellipsize(Pango.EllipsizeMode.END)
        preview.set_max_width_chars(24)
        preview.set_xalign(0)
        row.add_suffix(preview)

        ts = Gtk.Label(label=format_relative_time(conv.get("last_ts")))
        ts.add_css_class("dim-label")
        ts.add_css_class("caption")
        ts.set_valign(Gtk.Align.CENTER)
        row.add_suffix(ts)

        # Kick off metadata fetch (TTL-cached) so name/avatar fill in live.
        self.main_window.client.fetch_profile(pk)
        return row

    # ------------------------------------------------------------------
    # signals
    # ------------------------------------------------------------------

    def _on_row_activated(self, list_box, row):
        pk = getattr(row, "pubkey", None)
        if pk:
            self.main_window.show_dm_thread(pk)

    def _on_dm_updated(self, client, peer_pk):
        # A popped view stays connected to the client signal; skip work when
        # this view is no longer in the widget tree.
        if self.get_parent() is None:
            return
        self.reload()
