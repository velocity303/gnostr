import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
from gnostr.ui.post_widget import PostWidget


class ThreadView(Adw.Bin):
    def __init__(self, main_window, event_id, pubkey, content, tags=[]):
        super().__init__(css_classes=["card"])
        self.main_window = main_window
        self.event_id = event_id
        self.client = main_window.client

        self.layout = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
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

        btn_refresh = Gtk.Button(
            icon_name="view-refresh-symbolic", css_classes=["flat"]
        )
        btn_refresh.set_tooltip_text("Refresh Thread")
        btn_refresh.connect("clicked", lambda b: self.refresh())
        back_row.append(btn_refresh)
        header.append(back_row)

        # Title with event ID
        lbl_title = Gtk.Label(
            label=f"Thread {event_id[:8]}", xalign=0, css_classes=["heading"]
        )
        header.append(lbl_title)

        self.layout.append(header)
        self.layout.append(Gtk.Separator())

        # --- NEW: Context Container for Parents ---
        self.context_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.layout.append(self.context_box)

        # Load the parent chain if it exists
        self.load_parent_context(tags)
        # ------------------------------------------

        # Hero Post — truncate long posts so replies stay reachable
        self._hero_pubkey = pubkey
        self._hero_content = content
        self._hero_tags = tags
        self._hero_truncated = False
        self._hero_toggle = None

        display_content = content
        if len(content) > 500:
            display_content = content[:500] + "…"
            self._hero_truncated = True

        hero = PostWidget(
            self.main_window, pubkey, display_content, event_id, tags, is_hero=True
        )
        self.hero_widget = hero
        self.layout.append(hero)

        if self._hero_truncated:
            self._hero_toggle = Gtk.Button(label="Show more", css_classes=["flat"])
            self._hero_toggle.set_halign(Gtk.Align.START)
            self._hero_toggle.connect("clicked", lambda b: self.toggle_hero())
            self.layout.append(self._hero_toggle)

        # Replies (Simplified: fetch from DB based on event_id in tags)
        # Replies with scrolling - remove clamp to fill width
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        # --- ADD THESE LINES TO FILL THE SCREEN ---
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        # ------------------------------------------

        # Use a box directly to fill available width
        self.replies_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        # Ensure the box inside expands horizontally too
        self.replies_box.set_hexpand(True)

        scroll.set_child(self.replies_box)
        scroll.set_halign(Gtk.Align.FILL)
        self.layout.append(scroll)

        # We'll rely on the DB having the replies indexed
        replies = self.main_window.db.get_replies(event_id)
        for ev in replies:
            w = PostWidget(
                self.main_window,
                ev["pubkey"],
                ev["content"],
                ev["id"],
                ev.get("tags", []),
            )
            self.replies_box.append(w)

        # Listen for new events to update replies
        self.client.connect("event-received", self.on_event_received)

    def load_parent_context(self, tags):
        """Finds and renders the parent post if this post is a reply."""
        parent_id = None

        # Nostr NIP-10: 'reply' marker is preferred, otherwise the last 'e' tag
        e_tags = [t for t in tags if len(t) >= 2 and t[0] == "e"]

        # Try to find a tag explicitly marked as "reply"
        for t in e_tags:
            if len(t) >= 4 and t[3] == "reply":
                parent_id = t[1]
                break

        # Fallback: if no reply marker, take the last 'e' tag
        if not parent_id and e_tags:
            parent_id = e_tags[-1][1]

        if parent_id:
            # Fetch the parent from the database
            parent_ev = self.main_window.db.get_event_by_id(parent_id)

            if parent_ev:
                # Recursively load the grandparent first
                parent_tags = parent_ev.get("tags", [])
                self.load_parent_context(parent_tags)

                # Render the parent widget
                parent_widget = PostWidget(
                    self.main_window,
                    parent_ev["pubkey"],
                    parent_ev["content"],
                    parent_ev["id"],
                    parent_tags,
                    is_hero=False,
                )
                # Apply a slight visual style difference
                parent_widget.set_opacity(0.7)

                self.context_box.append(parent_widget)
                # Add a subtle connecting line indicator
                self.context_box.append(
                    Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
                )

    def on_event_received(self, client, eid, pubkey, content, tags_json):
        # Check if this event is a reply to our thread
        import json

        tags = json.loads(tags_json)
        for t in tags:
            if len(t) >= 2 and t[0] == "e" and t[1] == self.event_id:
                # This is a reply, add it
                w = PostWidget(self.main_window, pubkey, content, eid, tags)
                self.replies_box.append(w)
                break

    @staticmethod
    def _box_children(box):
        children = []
        child = box.get_first_child()
        while child is not None:
            children.append(child)
            child = child.get_next_sibling()
        return children

    def refresh(self):
        # Force reload of the thread: re-fetch root/replies/reactions from relays,
        # then re-render the hero, replies, and metrics from the DB.
        self.client.fetch_thread(self.event_id)
        ev = self.main_window.db.get_event_by_id(self.event_id)
        if ev:
            self._replace_hero(ev)
        self.reload_replies()
        self.reload_metrics()

    def _rebuild_hero(self, pubkey, content, tags):
        new_hero = PostWidget(
            self.main_window, pubkey, content, self.event_id, tags, is_hero=True
        )
        children = self._box_children(self.layout)
        try:
            idx = children.index(self.hero_widget)
        except ValueError:
            return
        self.layout.remove(self.hero_widget)
        self.layout.insert(new_hero, idx)
        self.hero_widget = new_hero

    def toggle_hero(self):
        self._hero_truncated = not self._hero_truncated
        if self._hero_truncated:
            self._rebuild_hero(self._hero_pubkey, self._hero_content[:500] + "…", self._hero_tags)
            self._hero_toggle.set_label("Show more")
        else:
            self._rebuild_hero(self._hero_pubkey, self._hero_content, self._hero_tags)
            self._hero_toggle.set_label("Show less")

    def _replace_hero(self, ev):
        # Refresh: rebuild from the DB's latest event, keeping the current
        # collapsed/expanded hero state so replies stay reachable.
        self._hero_pubkey = ev["pubkey"]
        self._hero_content = ev["content"]
        self._hero_tags = ev.get("tags", [])
        display = ev["content"]
        if self._hero_truncated:
            display = ev["content"][:500] + "…"
        self._rebuild_hero(ev["pubkey"], display, self._hero_tags)

    def reload_replies(self):
        for child in self._box_children(self.replies_box):
            self.replies_box.remove(child)
        replies = self.main_window.db.get_replies(self.event_id)
        for ev in replies:
            w = PostWidget(
                self.main_window,
                ev["pubkey"],
                ev["content"],
                ev["id"],
                ev.get("tags", []),
            )
            self.replies_box.append(w)

    def reload_metrics(self):
        m = self.client.metrics.get(self.event_id)
        if not m:
            return
        self.hero_widget.lbl_likes.set_label(str(m.get("likes", 0)))
        self.hero_widget.lbl_reposts.set_label(str(m.get("reposts", 0)))
        self.hero_widget.lbl_replies.set_label(str(m.get("replies", 0)))
