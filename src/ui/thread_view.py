
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw
from gnostr.ui.post_widget import PostWidget

class ThreadView(Adw.Bin):
    def __init__(self, main_window, event_id, pubkey, content, tags=[]):
        super().__init__(css_classes=["card"])
        self.main_window = main_window
        
        self.layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, 
                             margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.set_child(self.layout)

        # Hero Post
        hero = PostWidget(self.main_window, pubkey, content, event_id, tags, is_hero=True)
        self.layout.append(hero)
        self.layout.append(Gtk.Separator())

        # Replies (Simplified: fetch from DB based on event_id in tags)
        self.replies_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.layout.append(self.replies_box)

        # We'll rely on the DB having the replies indexed
        replies = self.main_window.db.get_replies(event_id)
        for ev in replies:
            w = PostWidget(self.main_window, ev['pubkey'], ev['content'], ev['id'], ev.get('tags', []))
            self.replies_box.append(w)
