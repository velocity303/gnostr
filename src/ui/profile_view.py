
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw
from gnostr.ui.post_widget import PostWidget

class ProfileView(Adw.Bin):
    def __init__(self, main_window, pubkey):
        super().__init__(css_classes=["card"])
        self.main_window = main_window
        self.pubkey = pubkey

        self.layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, 
                             margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.set_child(self.layout)

        # Simple Profile Header
        prof = self.main_window.db.get_profile(pubkey)
        name = prof.get('display_name') or prof.get('name') or pubkey[:8] if prof else pubkey[:8]
        
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        lbl_name = Gtk.Label(label=name, xalign=0, css_classes=["heading"])
        lbl_pub = Gtk.Label(label=pubkey, xalign=0, css_classes=["caption", "dim-label"])
        header.append(lbl_name)
        header.append(lbl_pub)
        self.layout.append(header)
        self.layout.append(Gtk.Separator())

        # Posts List
        self.posts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.layout.append(self.posts_box)

        # Load posts from DB
        posts = self.main_window.db.get_feed_for_user(pubkey)
        for ev in posts:
            w = PostWidget(self.main_window, ev['pubkey'], ev['content'], ev['id'], ev.get('tags', []), is_hero=False)
            self.posts_box.append(w)
