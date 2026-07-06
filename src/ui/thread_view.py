
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw
from gnostr.ui.post_widget import PostWidget

class ThreadView(Adw.Bin):
    def __init__(self, main_window, event_id, pubkey, content, tags=[]):
        super().__init__(css_classes=["card"])
        self.main_window = main_window
        self.event_id = event_id
        self.client = main_window.client

        self.layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                             margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.set_child(self.layout)
        self.layout.set_vexpand(True)

        # Header with back button and title
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        header.set_halign(Gtk.Align.START)

        # Back button row
        back_row = Gtk.Box(spacing=12, halign=Gtk.Align.START)
        btn_back = Gtk.Button(icon_name="go-previous-symbolic", css_classes=["flat"])
        btn_back.set_tooltip_text("Back")
        btn_back.connect("clicked", lambda b: self.main_window.content_nav.pop())
        back_row.append(btn_back)
        header.append(back_row)

        # Title with event ID
        lbl_title = Gtk.Label(label=f"Thread {event_id[:8]}", xalign=0, css_classes=["heading"])
        header.append(lbl_title)

        self.layout.append(header)
        self.layout.append(Gtk.Separator())

        # Hero Post
        hero = PostWidget(self.main_window, pubkey, content, event_id, tags, is_hero=True)
        self.layout.append(hero)

        # Replies (Simplified: fetch from DB based on event_id in tags)
        # Replies with scrolling
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        c = Adw.Clamp(maximum_size=600)
        self.replies_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        c.set_child(self.replies_box)
        scroll.set_child(c)
        self.layout.append(scroll)

        # We'll rely on the DB having the replies indexed
        replies = self.main_window.db.get_replies(event_id)
        for ev in replies:
            w = PostWidget(self.main_window, ev['pubkey'], ev['content'], ev['id'], ev.get('tags', []))
            self.replies_box.append(w)

        # Listen for new events to update replies
        self.client.connect("event-received", self.on_event_received)

    def on_event_received(self, client, eid, pubkey, content, tags_json):
        # Check if this event is a reply to our thread
        import json
        tags = json.loads(tags_json)
        for t in tags:
            if len(t) >= 2 and t[0] == 'e' and t[1] == self.event_id:
                # This is a reply, add it
                w = PostWidget(self.main_window, pubkey, content, eid, tags)
                self.replies_box.append(w)
                break
