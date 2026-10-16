# src/ui/sidebar.py
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, Pango


class Sidebar(Gtk.Box):
    def __init__(self, main_window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.main_window = main_window

        # Header Bar
        h = Adw.HeaderBar()
        h.set_show_end_title_buttons(False)
        sb = Gtk.Button(icon_name="emblem-system-symbolic")
        sb.connect("clicked", self.on_settings_clicked)
        h.pack_end(sb)
        self.append(h)

        # Navigation Menu
        self.menu_list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.menu_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.menu_list.set_activate_on_single_click(True)
        self.menu_list.connect("row-activated", self.on_menu_selected)

        self.rows = {}
        items = [
            ("following", "My Feed", "system-users"),
            ("global", "Global Feed", "network-server"),
            ("profile", "Profile", "avatar-default"),
            ("search", "Search User", "system-search"),
            ("followers", "Follows", "network-transmit-receive"),
            ("messages", "Messages", "mail-unread"),
        ]

        for r_id, title, icon in items:
            r = Adw.ActionRow(title=title, icon_name=f"{icon}-symbolic")
            r.set_activatable(True)
            self.menu_list.append(r)
            self.rows[r_id] = r

        # Unread-DM badge on the Messages row (hidden at 0). MainWindow
        # refreshes it via update_dm_badge() off gateway queries.
        self.dm_badge = Gtk.Label(label="0")
        self.dm_badge.add_css_class("badge")
        self.dm_badge.add_css_class("accent")
        self.dm_badge.hide()
        self.rows["messages"].add_suffix(self.dm_badge)

        self.append(self.menu_list)
        self.append(Gtk.Box(vexpand=True))

        # Connected remote signer (NIP-46, P2 #15) — hidden unless a bunker
        # backend is active. Shows which signer holds the key + disconnect.
        self.signer_row = Adw.ActionRow(title="Signer", subtitle="remote signer")
        self.signer_row.set_visible(False)
        disc = Gtk.Button(label="Disconnect", valign=Gtk.Align.CENTER)
        disc.connect("clicked", self.on_disconnect_signer_clicked)
        self.signer_row.add_suffix(disc)
        self.menu_list.append(self.signer_row)

        # Relay Activity — collapsible log of publish/OK/connect activity so
        # follows/likes/reposts are verifiable against relays, not just the DB.
        self.relay_expander = Adw.ExpanderRow(title="Relay Activity")
        # wrap WORD_CHAR + max_width_chars=1: a long relay line (e.g.
        # "wss://relay.example-very-long-domain.org:8443 publish OK ...") must
        # wrap inside the row instead of forcing its natural width on the
        # sidebar column (hscrollbar is NEVER on mobile). max_width_chars=1
        # collapses the label's *natural* width so it fills the row and wraps;
        # WORD_CHAR only breaks mid-token when one word alone exceeds the width.
        self.relay_log_label = Gtk.Label(
            label="No relay activity yet",
            css_classes=["monospace", "dim-label"],
            halign=Gtk.Align.START,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            max_width_chars=1,
            hexpand=True,
        )
        self.relay_log_label.set_selectable(True)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        # max-content-height is ONLY honored when propagate-natural-height is
        # TRUE (its default is FALSE, so the cap alone is silently ignored and
        # the log's full natural height — 700px+ of wrapped lines — leaks into
        # the row). Set both so the 180px cap holds: the log scrolls inside the
        # row instead of stretching the section.
        sw.set_propagate_natural_height(True)
        sw.set_max_content_height(180)
        sw.set_hexpand(True)
        sw.set_child(self.relay_log_label)
        self.relay_expander.add_row(sw)
        self.append(self.relay_expander)

        # Key-health (P3 #16): last backup verified + re-verify prompt.
        self.key_health_label = Gtk.Label(
            label="", css_classes=["dim-label"], wrap=True
        )
        self.key_health_label.set_xalign(0)
        self.append(self.key_health_label)
        self._refresh_key_health()

        # Back Up Key (NIP-49 export, P1 #14)
        bb = Gtk.Button(label="Back Up Key", css_classes=["flat"])
        bb.connect("clicked", self.on_backup_key_clicked)
        self.append(bb)

        # Logout Button
        lb = Gtk.Button(label="Logout", css_classes=["flat"])
        lb.connect("clicked", self.on_logout_clicked)
        self.append(lb)

        # Status Label
        self.status_label = Gtk.Label(label="🔴", css_classes=["dim-label"])
        self.append(self.status_label)

    def append_relay_log(self, line):
        """Append a relay-activity line, newest at top (prepend)."""
        cur = self.relay_log_label.get_text()
        if cur == "No relay activity yet":
            cur = ""
        self.relay_log_label.set_text(f"{line}\n{cur}")

    def on_settings_clicked(self, b):
        self.main_window.on_settings_clicked()

    def on_menu_selected(self, listbox, row):
        for r_id, row_obj in self.rows.items():
            if row == row_obj:
                self.main_window.on_menu_selected(r_id)
                break

    def on_logout_clicked(self, b):
        self.main_window.on_logout_clicked()

    def on_backup_key_clicked(self, b):
        self._refresh_key_health()
        self.main_window.on_export_key_clicked()

    def on_disconnect_signer_clicked(self, b):
        self.main_window.on_disconnect_signer()

    def set_signer_status(self, label):
        """Show/hide the connected-signer row (P2 #15). label=None hides."""
        if label is None:
            self.signer_row.set_visible(False)
        else:
            self.signer_row.set_subtitle(label)
            self.signer_row.set_visible(True)

    def _refresh_key_health(self):
        """Key-health copy: last backup verified, or a re-verify prompt."""
        from .. import key_health

        ts = key_health.last_backup_verified()
        if ts is None:
            self.key_health_label.set_text(
                "No backup verified yet — back up your key now."
            )
        else:
            self.key_health_label.set_text(
                "Backup verified: %s. Re-verify if unsure."
                % key_health.format_last_backup(ts)
            )

    def update_status(self, status_emoji):
        self.status_label.set_text(status_emoji)

    def update_dm_badge(self, count):
        """Show the unread-DM count on the Messages row; hidden at 0."""
        if count > 0:
            self.dm_badge.set_text(str(count))
            self.dm_badge.show()
        else:
            self.dm_badge.hide()
