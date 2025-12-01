#!/usr/bin/env python3
import sys
import json
import time
import gi
import traceback
from key_manager import KeyManager
import nostr_utils
from database import Database
from client import NostrClient
from renderer import ContentRenderer, ImageLoader
from dialogs import LoginDialog, RelayPreferencesWindow

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio, Pango

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_title("Gnostr"); self.set_default_size(950, 700)
        self.db = Database(); self.client = NostrClient(self.db)
        self.client.connect("event-received", self.on_event_received)
        self.client.connect("status-changed", self.on_status_changed)
        self.client.connect("contacts-updated", self.on_contacts_updated)
        self.client.connect("profile-updated", self.on_profile_updated)
        self.client.connect("metrics-updated", self.on_metrics_updated)
        self.priv_key = None; self.pub_key = None; self.active_feed_type = "global"; self.event_widgets = {} 
        self.active_profile_pubkey = None

        self.split_view = Adw.NavigationSplitView(); self.set_content(self.split_view)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.new_length(Adw.BreakpointConditionLengthType.MAX_WIDTH, 800, Adw.LengthUnit.SP))
        bp.add_setter(self.split_view, "collapsed", True); self.add_breakpoint(bp)

        self.setup_sidebar(); self.setup_content_area()
        self.main_stack = Adw.ViewStack(); self.set_content(self.main_stack)
        
        self.login_page = Adw.StatusPage(title="Welcome", icon_name="avatar-default-symbolic")
        lb = Gtk.Button(label="Login", css_classes=["pill", "suggested-action"]); lb.connect("clicked", self.on_login_clicked)
        bx = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER); bx.append(lb)
        self.login_page.set_child(bx)

        self.main_stack.add_named(self.login_page, "login"); self.main_stack.add_named(self.split_view, "app")
        saved = KeyManager.load_key()
        if saved: self.perform_login(saved)
        else: self.main_stack.set_visible_child_name("login")
        GLib.idle_add(self.client.connect_all)

    def setup_sidebar(self):
        self.sidebar_page = Adw.NavigationPage(title="Menu", tag="sidebar")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        h = Adw.HeaderBar(); h.set_show_end_title_buttons(False); 
        sb = Gtk.Button(icon_name="emblem-system-symbolic"); sb.connect("clicked", self.on_settings_clicked); h.pack_end(sb)
        box.append(h)
        ml = Gtk.ListBox(css_classes=["navigation-sidebar"]); ml.set_selection_mode(Gtk.SelectionMode.NONE)
        ml.set_activate_on_single_click(True); ml.connect("row-activated", self.on_menu_selected)
        
        self.rows = {}
        items = [
            ("global","Global","network-server"),
            ("following","Following","system-users"),
            ("me","My Posts","user-info"),
            ("profile","Profile","avatar-default"),
            ("search", "Search User", "system-search")
        ]

        for r_id, title, icon in items:
            r = Adw.ActionRow(title=title, icon_name=f"{icon}-symbolic"); r.set_activatable(True)
            ml.append(r); self.rows[r_id] = r
        
        box.append(ml); box.append(Gtk.Box(vexpand=True))
        lb = Gtk.Button(label="Logout", css_classes=["flat"]); lb.connect("clicked", self.on_logout_clicked); box.append(lb)
        self.status_label = Gtk.Label(label="Offline", css_classes=["dim-label"]); box.append(self.status_label)
        self.sidebar_page.set_child(box); self.split_view.set_sidebar(self.sidebar_page)

    def setup_content_area(self):
        self.content_nav = Adw.NavigationView()
        wrapper = Adw.NavigationPage(title="Content", tag="wrapper"); wrapper.set_child(self.content_nav)
        self.split_view.set_content(wrapper)

        self.feed_page = Adw.NavigationPage(title="Feed", tag="feed")
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL); b.append(Adw.HeaderBar())
        s = Gtk.ScrolledWindow(vexpand=True); c = Adw.Clamp(maximum_size=600)
        self.posts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        c.set_child(self.posts_box); s.set_child(c); b.append(s); self.feed_page.set_child(b); self.content_nav.add(self.feed_page)

        self.thread_page = Adw.NavigationPage(title="Thread", tag="thread")
        t_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        t_box.append(Adw.HeaderBar())
        t_scroll = Gtk.ScrolledWindow(vexpand=True)
        t_clamp = Adw.Clamp(maximum_size=600)
        self.thread_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        t_clamp.set_child(self.thread_container)
        t_scroll.set_child(t_clamp)
        t_box.append(t_scroll)
        self.thread_page.set_child(t_box)

        self.profile_page = Adw.NavigationPage(title="Profile", tag="profile")
        p_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        p_box.set_margin_top(40); p_box.set_margin_bottom(20); p_box.set_margin_start(12); p_box.set_margin_end(12)
        p_box.set_halign(Gtk.Align.CENTER)
        
        p_header_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        p_header_box.append(Adw.HeaderBar())
        
        p_scroll = Gtk.ScrolledWindow(vexpand=True); p_clamp = Adw.Clamp(maximum_size=600)
        p_clamp.set_child(p_box); p_scroll.set_child(p_clamp); p_header_box.append(p_scroll)
        self.profile_page.set_child(p_header_box)

        self.prof_avatar = Adw.Avatar(size=96, show_initials=True)
        self.lbl_name = Gtk.Label(css_classes=["title-1"])
        self.lbl_npub = Gtk.Label(css_classes=["caption", "dim-label"], selectable=True)
        self.lbl_npub.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.lbl_about = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=40)
        self.lbl_about.set_wrap_mode(Pango.WrapMode.WORD_CHAR)

        self.follow_btn_box = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=10)

        for w in [self.prof_avatar, self.lbl_name, self.lbl_npub, self.lbl_about, self.follow_btn_box]: p_box.append(w)

        p_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        p_box.append(Gtk.Label(label="Recent Posts", css_classes=["heading"], xalign=0))
        self.profile_posts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        p_box.append(self.profile_posts_box)

    def show_thread(self, event_id, pubkey, content, tags=[]):
        page = Adw.NavigationPage(title="Thread")

        # Determine logical root of the thread to fetch context
        # If the clicked event has a root tag, that is the root.
        # Otherwise, the clicked event itself is the root start point for us.
        root_id = nostr_utils.get_thread_root(tags)

        # We track both the ID of the clicked post (hero) and the thread root
        page.hero_id = event_id
        page.root_id = root_id if root_id else event_id

        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL); b.append(Adw.HeaderBar())
        s = Gtk.ScrolledWindow(vexpand=True); c = Adw.Clamp(maximum_size=600)
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        
        # Structure: Ancestors -> Hero -> Replies
        page.ancestors_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.replies_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        container.append(page.ancestors_box)

        hero = self.create_post_widget(pubkey, content, event_id, tags, is_hero=True)
        # Store hero timestamp for sorting logic later
        hero.created_at = time.time() # Approximation until loaded, but usually we just treat it as pivot
        page.hero_widget = hero
        page.is_loaded = False

        container.append(hero)
        container.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        container.append(Gtk.Label(label="Replies", css_classes=["heading"], xalign=0))
        container.append(page.replies_box)

        page.thread_container = container # Keep ref just in case

        c.set_child(container); s.set_child(c); b.append(s); page.set_child(b)
        self.content_nav.push(page)

        # Fetch the entire conversation tree starting from the root
        self.client.fetch_thread(page.root_id)

        if pubkey == "Unknown" and content == "Loading...":
            self.schedule_refresh(page, page.root_id)

    def schedule_refresh(self, page, event_id, attempt=1):
        def _refresh():
            if page.is_loaded: return False
            if attempt > 5:
                print(f"❌ Give up loading thread {event_id} after 5 attempts.")
                return False

            print(f"🔄 Auto-refreshing thread {event_id} (Attempt {attempt})...")
            self.client.fetch_thread(event_id)
            GLib.timeout_add(3000, lambda: self.schedule_refresh(page, event_id, attempt + 1))
            return False

        GLib.timeout_add(3000, _refresh)

    def show_profile(self, pubkey):
        self.active_profile_pubkey = pubkey
        if self.content_nav.get_visible_page() != self.profile_page:
            self.content_nav.push(self.profile_page)

        self.client.fetch_profile(pubkey)
        npub = nostr_utils.hex_to_nsec(pubkey).replace("nsec", "npub")
        self.lbl_npub.set_text(npub[:12] + "..." + npub[-12:])

        profile = self.db.get_profile(pubkey)
        if profile:
            name = profile.get('display_name') or profile.get('name') or "Anonymous"
            self.lbl_name.set_text(name); self.prof_avatar.set_text(name)
            self.lbl_about.set_text(profile.get('about') or "")
            if profile.get('picture'): ImageLoader.load_avatar(profile['picture'], lambda t: self.prof_avatar.set_custom_image(t))
        else:
            self.lbl_name.set_text("Loading...")
            self.prof_avatar.set_text("?")
            self.lbl_about.set_text("")
            self.prof_avatar.set_custom_image(None)
            self.schedule_profile_refresh(pubkey)

        c = self.follow_btn_box.get_first_child()
        if c: self.follow_btn_box.remove(c)

        if self.pub_key and pubkey != self.pub_key:
            following = self.db.get_following_list(self.pub_key)
            if pubkey not in following:
                btn_follow = Gtk.Button(label="Follow", css_classes=["pill", "suggested-action"])
                btn_follow.connect("clicked", lambda b: self.client.follow_user(pubkey))
                self.follow_btn_box.append(btn_follow)
            else:
                btn_unfollow = Gtk.Button(label="Unfollow", css_classes=["flat"])
                btn_unfollow.connect("clicked", lambda b: self.client.unfollow_user(pubkey))
                self.follow_btn_box.append(btn_unfollow)

        c = self.profile_posts_box.get_first_child()
        while c: self.profile_posts_box.remove(c); c = self.profile_posts_box.get_first_child()

        posts = self.db.get_feed_for_user(pubkey, limit=20)
        for ev in posts:
            w = self.create_post_widget(ev['pubkey'], ev['content'], ev['id'], ev.get('tags', []))
            self.profile_posts_box.append(w)

        self.client.subscribe("sub_profile_view", {"kinds": [1], "authors": [pubkey], "limit": 20})
        self.split_view.set_show_content(True)

    def schedule_profile_refresh(self, pubkey, attempt=1):
        def _refresh():
            if self.active_profile_pubkey != pubkey: return False
            if self.lbl_name.get_text() != "Loading...": return False

            if attempt > 5:
                print(f"❌ Give up loading profile {pubkey[:8]} after 5 attempts.")
                return False

            print(f"🔄 Auto-refreshing profile {pubkey[:8]} (Attempt {attempt})...")
            self.client.fetch_profile(pubkey)
            GLib.timeout_add(2000, lambda: self.schedule_profile_refresh(pubkey, attempt + 1))
            return False
        GLib.timeout_add(2000, _refresh)

    def show_search_dialog(self):
        dialog = Adw.Window(title="Search User", modal=True, transient_for=self)
        dialog.set_default_size(400, 150)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=24, margin_bottom=24, margin_start=24, margin_end=24)
        lbl = Gtk.Label(label="Enter npub (nostr:npub1...)", xalign=0)
        box.append(lbl)

        entry = Gtk.Entry(placeholder_text="npub1...")
        box.append(entry)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, halign=Gtk.Align.END)
        btn_cancel = Gtk.Button(label="Cancel")
        btn_cancel.connect("clicked", lambda b: dialog.close())
        btn_box.append(btn_cancel)

        btn_go = Gtk.Button(label="Go", css_classes=["suggested-action"])

        def _on_go(*args):
            text = entry.get_text().strip().replace("nostr:", "")
            try:
                if text.startswith("npub"):
                    hrp, data = nostr_utils.bech32_decode(text)
                    if hrp == "npub" and data:
                        decoded = nostr_utils.convertbits(data, 5, 8, False)
                        if decoded:
                            hex_key = bytes(decoded).hex()
                            dialog.close()
                            self.show_profile(hex_key)
                            return
            except Exception as e:
                print(f"Search Error: {e}")

            entry.add_css_class("error")

        btn_go.connect("clicked", _on_go)
        btn_box.append(btn_go)
        box.append(btn_box)
        dialog.set_content(box)
        dialog.present()

    def create_post_widget(self, pubkey, content, event_id, tags=[], is_hero=False):
        try:
            card = Adw.Bin(css_classes=["card"])
            if is_hero: card.add_css_class("hero")
            main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
            hb = Gtk.Box(spacing=12)
            prof = self.db.get_profile(pubkey)
            name = pubkey[:8]
            if prof: name = prof.get('display_name') or prof.get('name') or name
            av = Adw.Avatar(size=48 if is_hero else 40, show_initials=True, text=name)
            if prof and prof.get('picture'): ImageLoader.load_avatar(prof['picture'], lambda t: av.set_custom_image(t))

            btn_av = Gtk.Button(css_classes=["flat"])
            btn_av.set_child(av)
            btn_av.connect("clicked", lambda b: self.show_profile(pubkey))
            hb.append(btn_av)

            nb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            nb.append(Gtk.Label(label=name, xalign=0, css_classes=["heading"]))
            nb.append(Gtk.Label(label=pubkey[:12]+"...", xalign=0, css_classes=["caption", "dim-label"]))
            hb.append(nb)
            main_box.append(hb)

            try:
                main_box.append(ContentRenderer.render(content, self))
            except Exception as re:
                print(f"❌ ERROR [Widget]: Render failed for {event_id[:8]}. Content repr: {repr(content)}")
                print(f"   Reason: {re}")
                lbl = Gtk.Label(label=f"[Content Render Failed: {re}]")
                main_box.append(lbl)

            footer = Gtk.Box(spacing=20, margin_top=8)
            def mk_met(icon):
                b = Gtk.Box(spacing=6); b.append(Gtk.Image.new_from_icon_name(icon))
                l = Gtk.Label(label="0", css_classes=["caption", "dim-label"]); b.append(l)
                return b, l
            r_box, l_rep = mk_met("chat-bubble-symbolic")
            rt_box, l_ret = mk_met("media-playlist-repeat-symbolic")
            l_box, l_like = mk_met("starred-symbolic")
            footer.append(r_box); footer.append(rt_box); footer.append(l_box)
            main_box.append(footer)
            card.set_child(main_box)
            card.lbl_replies = l_rep; card.lbl_reposts = l_ret; card.lbl_likes = l_like
            self.event_widgets[event_id] = card
            if not is_hero:
                ctrl = Gtk.GestureClick()
                # Pass tags here correctly
                ctrl.connect("released", lambda c, n, x, y: self.show_thread(event_id, pubkey, content, tags))
                card.add_controller(ctrl)
            return card
        except Exception as e:
            print(f"❌ CRITICAL [Widget]: Failed to create post widget for {event_id[:8]}: {e}")
            traceback.print_exc()
            return Gtk.Label(label="[Widget Error]")

    def on_event_received(self, client, eid, pubkey, content, tags_json):
        if not self.db.get_profile(pubkey): self.client.fetch_profile(pubkey)
        target = self.posts_box
        page = self.content_nav.get_visible_page()
        
        try:
            tags = json.loads(tags_json)
        except: tags = []

        if page == self.profile_page and pubkey == self.active_profile_pubkey:
            w = self.create_post_widget(pubkey, content, eid, tags)
            self.profile_posts_box.prepend(w)

        # Thread View Logic
        if hasattr(page, 'hero_id'):
            # If this is the hero (clicked) event updating
            if eid == page.hero_id:
                 page.is_loaded = True
                 if hasattr(page, 'thread_container'):
                     new_hero = self.create_post_widget(pubkey, content, eid, tags, is_hero=True)
                     # Swap logic roughly... actually simply replacing the child in box is hard in Gtk4 without refs
                     # For now, we assume the hero widget is already set and static content doesn't change much
                     # except metadata. We skip replacing it to avoid flicker, or could refine this.
                     return

            # If it's part of the thread context
            # Simplistic check: does it reference the root? Or is it the root?
            is_relevant = False
            if eid == page.root_id: is_relevant = True
            else:
                for t in tags:
                    if t[0] == 'e' and t[1] == page.root_id: is_relevant = True; break

            if is_relevant:
                w = self.create_post_widget(pubkey, content, eid, tags)
                w.created_at = time.time() # Needs actual event timestamp from client for proper sort

                # Sort into Ancestors or Replies based on Hero ID comparison
                # Since we don't have easy timestamps passed in event-received (it's separate),
                # we rely on the fact that Ancestors usually come before Hero in a full fetch,
                # but let's use a heuristic: if it IS the root, it's an ancestor.

                if eid == page.root_id and eid != page.hero_id:
                    self._insert_sorted(page.ancestors_box, w)
                elif eid != page.hero_id:
                    self._insert_sorted(page.replies_box, w)
                return

        if page == self.feed_page:
             w = self.create_post_widget(pubkey, content, eid, tags)
             self.posts_box.prepend(w)

    def _insert_sorted(self, box, widget):
        # Naive append for now as we lack easy timestamp access in this scope without DB lookup
        # Ideally, we would sort by created_at.
        box.append(widget)

    def on_contacts_updated(self, client):
        if self.active_feed_type == "following":
            self.switch_feed("following")
        if self.content_nav.get_visible_page() == self.profile_page and self.active_profile_pubkey:
            self.show_profile(self.active_profile_pubkey)

    def on_profile_updated(self, client, pubkey):
        if pubkey == self.pub_key: self.load_my_profile_ui()
        if pubkey == self.active_profile_pubkey and self.content_nav.get_visible_page() == self.profile_page:
             self.show_profile(pubkey)

    def on_metrics_updated(self, client, eid, likes, reposts, replies):
        if eid in self.event_widgets:
            w = self.event_widgets[eid]
            if hasattr(w, 'lbl_likes'): w.lbl_likes.set_label(str(likes))
            if hasattr(w, 'lbl_reposts'): w.lbl_reposts.set_label(str(reposts))
            if hasattr(w, 'lbl_replies'): w.lbl_replies.set_label(str(replies))

    def perform_login(self, priv_hex):
        self.priv_key = priv_hex; self.pub_key = nostr_utils.get_public_key(priv_hex)
        self.client.set_keys(self.pub_key, self.priv_key)
        self.main_stack.set_visible_child_name("app")
        self.active_feed_type = "global"
        self.load_my_profile_ui()
        GLib.timeout_add(1000, lambda: self.switch_feed("global"))
        GLib.timeout_add(2000, lambda: self.client.fetch_profile(self.pub_key))
        GLib.timeout_add(2500, self.client.fetch_contacts)
        GLib.timeout_add(4000, self.client.fetch_user_relays)

    def load_my_profile_ui(self):
        if not self.pub_key: return
        npub = nostr_utils.hex_to_nsec(self.pub_key).replace("nsec", "npub")
        self.lbl_npub.set_text(npub[:12] + "..." + npub[-12:])
        profile = self.db.get_profile(self.pub_key)
        if profile:
            name = profile.get('display_name') or profile.get('name') or "Anonymous"
            self.lbl_name.set_text(name); self.prof_avatar.set_text(name); self.lbl_about.set_text(profile.get('about') or "")
            if profile.get('picture'): ImageLoader.load_avatar(profile['picture'], lambda t: self.prof_avatar.set_custom_image(t))

    def on_login_clicked(self, btn): LoginDialog(self.client, self).present()
    def on_logout_clicked(self, btn):
        KeyManager.delete_key(); self.main_stack.set_visible_child_name("login"); self.client.set_keys(None, None)
    def on_menu_selected(self, box, row):
        if not row: return
        if row == self.rows["global"]: self.switch_feed("global")
        elif row == self.rows["following"]: self.switch_feed("following")
        elif row == self.rows["me"]: self.switch_feed("me")
        elif row == self.rows["profile"]: 
             if self.pub_key: self.show_profile(self.pub_key)
        elif row == self.rows["search"]:
             self.show_search_dialog()

        self.split_view.set_show_content(True)

    def on_settings_clicked(self, btn): RelayPreferencesWindow(self.client, self).present()
    def on_status_changed(self, client, status): self.status_label.set_text(status)

    def switch_feed(self, feed_type):
        self.active_feed_type = feed_type
        self.content_nav.pop_to_page(self.feed_page)
        self.feed_page.set_title("Feed") 
        c = self.posts_box.get_first_child()
        while c: self.posts_box.remove(c); c = self.posts_box.get_first_child()
        self.event_widgets.clear()

        cached = []
        if feed_type == "me" and self.pub_key: cached = self.db.get_feed_for_user(self.pub_key)
        elif feed_type == "following" and self.pub_key: cached = self.db.get_feed_following(self.pub_key)
        for ev in cached:
            w = self.create_post_widget(ev['pubkey'], ev['content'], ev['id'], ev.get('tags', []))
            self.posts_box.prepend(w)

        if feed_type == "global": self.client.subscribe("sub_global", {"kinds": [1], "limit": 20})
        elif feed_type == "me" and self.pub_key: self.client.subscribe("sub_me", {"kinds": [1], "authors": [self.pub_key], "limit": 20})
        elif feed_type == "following" and self.pub_key:
            contacts = self.db.get_following_list(self.pub_key)
            if contacts: self.client.subscribe("sub_following", {"kinds": [1], "authors": contacts[:300], "limit": 50})

class GnostrApp(Adw.Application):
    def __init__(self, **kwargs):
        super().__init__(application_id="me.velocitynet.Gnostr", flags=Gio.ApplicationFlags.FLAGS_NONE, **kwargs)
    def do_activate(self):
        win = self.props.active_window
        if not win: win = MainWindow(application=self)
        win.present()

def main(version): app = GnostrApp(); return app.run(sys.argv)
if __name__ == "__main__": main(None)
