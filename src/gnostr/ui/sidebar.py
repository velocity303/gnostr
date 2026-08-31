# src/ui/sidebar.py
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw


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
        ]

        for r_id, title, icon in items:
            r = Adw.ActionRow(title=title, icon_name=f"{icon}-symbolic")
            r.set_activatable(True)
            self.menu_list.append(r)
            self.rows[r_id] = r

        self.append(self.menu_list)
        self.append(Gtk.Box(vexpand=True))

        # Relay Activity — collapsible log of publish/OK/connect activity so
        # follows/likes/reposts are verifiable against relays, not just the DB.
        self.relay_expander = Adw.ExpanderRow(title="Relay Activity")
        self.relay_log_label = Gtk.Label(
            label="No relay activity yet",
            css_classes=["monospace", "dim-label"],
            halign=Gtk.Align.START,
            wrap=False,
        )
        self.relay_log_label.set_selectable(True)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_max_content_height(180)
        sw.set_child(self.relay_log_label)
        self.relay_expander.add_row(sw)
        self.append(self.relay_expander)

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

    def update_status(self, status_emoji):
        self.status_label.set_text(status_emoji)
