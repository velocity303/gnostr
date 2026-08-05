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
        self._pending_parents = []  # parent event ids being fetched for context

        self.layout = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        # Make the WHOLE thread scrollable (hero + context + toggle + replies),
        # not just the replies — a long hero used to fill the screen with no way
        # to scroll down to the toggle or replies (bugs: collapse vanished the
        # post, long threads were unnavigable).
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_hexpand(True)
        self.scroll.set_vexpand(True)
        self.scroll.set_child(self.layout)
        self.set_child(self.scroll)
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
        # Hero + toggle live in a dedicated sub-container so rebuilds only touch
        # this box — no fragile index math against the shared layout (which also
        # holds header, context_box, replies_box). This fixes the collapse bug
        # where the post vanished and couldn't be restored.
        self.hero_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.hero_section.append(hero)
        self.layout.append(self.hero_section)

        if self._hero_truncated:
            self._hero_toggle = Gtk.Button(label="Show more", css_classes=["flat"])
            self._hero_toggle.set_halign(Gtk.Align.START)
            self._hero_toggle.connect("clicked", lambda b: self.toggle_hero())
            self.hero_section.append(self._hero_toggle)

        # Replies (fetch from DB based on event_id in tags). Now that the whole
        # thread is one ScrolledWindow, the replies box goes directly into the
        # layout — no nested scrolled window (ScrolledWindow-in-ScrolledWindow
        # breaks scrolling).
        self.replies_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.replies_box.set_hexpand(True)
        self.layout.append(self.replies_box)

        # We'll rely on the DB having the replies indexed
        replies = self.main_window.db.get_replies(event_id)
        for ev in replies:
            w = PostWidget(
                self.main_window,
                ev["pubkey"],
                ev["content"],
                ev["id"],
                ev.get("tags", []),
                created_at=ev.get("created_at"),
            )
            # get_replies is newest-first (ORDER BY created_at DESC); append
            # builds newest→oldest top-to-bottom.
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
            else:
                # Parent not cached — fetch it so the reply context loads once the
                # event arrives (rendered in on_event_received).
                if parent_id not in self._pending_parents:
                    self._pending_parents.append(parent_id)
                    self.client.request_once(
                        f"parent_{parent_id[:8]}", {"ids": [parent_id], "limit": 1}
                    )

    def on_event_received(self, client, eid, pubkey, content, tags_json):
        # Check if this event is a reply to our thread, or a pending parent context
        import json

        tags = json.loads(tags_json)
        if eid in self._pending_parents:
            self._pending_parents.remove(eid)
            self._render_arrived_parent(eid, pubkey, content, tags)
            return
        for t in tags:
            if len(t) >= 2 and t[0] == "e" and t[1] == self.event_id:
                # This is a reply to the thread — re-render the full reply tree so
                # it lands beneath its direct parent (a reply to a reply nests
                # under that reply, even if newer). Reloading from the DB also
                # dedups against anything already rendered.
                self.reload_replies()
                # Ask the relay for this reply's own replies so deeper nesting
                # fills in live (fetch_thread only requests #e:[root]).
                self.client.request_once(
                    f"thread_replies_{eid[:8]}",
                    {"kinds": [1], "#e": [eid], "limit": 50},
                )
                return

    def _render_arrived_parent(self, parent_id, pubkey, content, tags):
        # Recursively load this parent's own parents (fetching any missing ones),
        # then render it into the context box.
        self.load_parent_context(tags)
        w = PostWidget(self.main_window, pubkey, content, parent_id, tags)
        w.set_opacity(0.7)
        self.context_box.append(w)
        self.context_box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

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
        # Swap only the hero's content box via PostWidget.set_content — the same
        # widget identity is kept, so no async render chain is re-run and no
        # widget is removed/re-inserted. This fixes the toggle glitchiness and
        # the "post disappears" orphaning (the old code rebuilt a whole new
        # PostWidget every toggle, racing image/video loads).
        self.hero_widget.set_content(content)

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
        # Build a parent->children reply tree and render depth-first, so a reply
        # to a reply sits beneath the post it directly responds to (even if it's
        # newer), instead of a flat time-order list.
        children = {}
        for ev in replies:
            parent = self._direct_parent(ev.get("tags", []))
            children.setdefault(parent, []).append(ev)
        for pid in children:
            # Newest-first within each sibling group for coherent reading.
            children[pid].sort(key=lambda e: e.get("created_at") or 0, reverse=True)
        self._append_replies_dfs(self.event_id, children, depth=0)

    def _direct_parent(self, tags):
        """The event this post directly replies to. NIP-01: for a reply to a
        reply the tags carry [root, direct_parent] with the direct parent LAST;
        fall back to the thread root when no e-tag is present."""
        parent = self.event_id
        for t in tags:
            if len(t) >= 2 and t[0] == "e":
                parent = t[1]
        return parent

    def _append_replies_dfs(self, parent_id, children, depth):
        for ev in children.get(parent_id, []):
            w = PostWidget(
                self.main_window,
                ev["pubkey"],
                ev["content"],
                ev["id"],
                ev.get("tags", []),
                created_at=ev.get("created_at"),
            )
            if depth:
                # Indent nested replies so the parent-child structure reads.
                w.set_margin_start(depth * 24)
            self.replies_box.append(w)
            self._append_replies_dfs(ev["id"], children, depth + 1)

    def reload_metrics(self):
        m = self.client.metrics.get(self.event_id)
        if not m:
            return
        self.hero_widget.lbl_likes.set_label(str(m.get("likes", 0)))
        self.hero_widget.lbl_reposts.set_label(str(m.get("reposts", 0)))
        self.hero_widget.lbl_replies.set_label(str(m.get("replies", 0)))
