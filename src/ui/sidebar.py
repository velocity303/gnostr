# src/ui/sidebar.py
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
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
            ("following","Following","system-users"),
            ("global","Global","network-server"),
            ("profile","Profile","avatar-default"),
            ("search", "Search User", "system-search")
        ]

        for r_id, title, icon in items:
            r = Adw.ActionRow(title=title, icon_name=f"{icon}-symbolic")
            r.set_activatable(True)
            self.menu_list.append(r)
            self.rows[r_id] = r
        
        self.append(self.menu_list)
        self.append(Gtk.Box(vexpand=True))

        # Logout Button
        lb = Gtk.Button(label="Logout", css_classes=["flat"])
        lb.connect("clicked", self.on_logout_clicked)
        self.append(lb)

        # Status Label
        self.status_label = Gtk.Label(label="🔴", css_classes=["dim-label"])
        self.append(self.status_label)

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
