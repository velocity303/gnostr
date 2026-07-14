# src/ui/feed_view.py
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw


class FeedView(Adw.NavigationPage):
    def __init__(self, main_window):
        super().__init__(title="Feed", tag="feed")
        self.main_window = main_window

        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(self.box)

        # Header bar with refresh button
        hb = Adw.HeaderBar()
        btn_refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        btn_refresh.set_tooltip_text("Refresh Feed")
        btn_refresh.connect("clicked", self.on_refresh_clicked)
        hb.pack_end(btn_refresh)
        self.box.append(hb)

        # Content area
        s = Gtk.ScrolledWindow(vexpand=True)
        s.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        c = Adw.Clamp(maximum_size=600)
        self.posts_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        c.set_child(self.posts_box)
        s.set_child(c)
        self.box.append(s)

    def on_refresh_clicked(self, btn):
        self.main_window.on_refresh_clicked()
