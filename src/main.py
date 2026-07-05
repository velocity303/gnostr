#!/usr/bin/env python3
import sys
import json
import time
import gi
import traceback
import gnostr
from gnostr.key_manager import KeyManager
from gnostr.database import Database
from gnostr.client import NostrClient
from gnostr.renderer import ContentRenderer, ImageLoader
from gnostr.dialogs import LoginDialog, RelayPreferencesWindow
from gnostr.ui.sidebar import Sidebar
from gnostr.ui.feed_view import FeedView
from gnostr.ui.post_widget import PostWidget

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio, Pango, Gdk

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_title("Gnostr")
        self.set_default_size(950, 700)

        self.db = Database()
        self.client = NostrClient(self.db)
        self.client.connect("event-received", self.on_event_received)
        self.client.connect("status-changed", self.on_status_changed)
        self.client.connect("contacts-updated", self.on_contacts_updated)
        self.client.connect("profile-updated", self.on_profile_updated)
        self.client.connect("metrics-updated", self.on_metrics_updated)

        self.priv_key = None
        self.pub_key = None
        self.active_feed_type = "following"
        self.event_widgets = {}
        self.active_profile_pubkey = None

        # 1. Root: Toast Overlay
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        # FAB Wrapper
        self.global_overlay = Gtk.Overlay()
        self.toast_overlay.set_child(self.global_overlay)

        # 2. Main Stack
        self.main_stack = Adw.ViewStack()
        self.global_overlay.set_child(self.main_stack)

        # FAB Button
        self.fab_post = Gtk.Button(icon_name="edit-create-symbolic", css_classes=["suggested-action", "pill"])
        self.fab_post.set_valign(Gtk.Align.END)
        self.fab_post.set_halign(Gtk.Align.END)
        self.fab_post.set_margin_bottom(20)
        self.fab_post.set_margin_end(20)
        self.fab_post.connect("clicked", self.on_fab_post_clicked)
        self.fab_post.set_visible(False)
        self.global_overlay.add_overlay(self.fab_post)

        # 3. App View: Split View
        self.split_view = Adw.NavigationSplitView()
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.new_length(Adw.BreakpointConditionLengthType.MAX_WIDTH, 800, Adw.LengthUnit.SP))
        bp.add_setter(self.split_view, "collapsed", True)
        self.add_breakpoint(bp)

        # UI Components
        self.sidebar = Sidebar(self)
        self.sidebar_page = Adw.NavigationPage(title="Menu", tag="sidebar")
        self.sidebar_page.set_child(self.sidebar)
        self.split_view.set_sidebar(self.sidebar_page)

        self.feed_view = FeedView(self)
        self.content_nav = Adw.NavigationView()
        self.split_view.set_content(self.content_nav)
        self.feed_page = self.content_nav.add(self.feed_view)

        # 4. Login View
        self.login_page = Adw.StatusPage(title="Welcome", icon_name="avatar-default-symbolic")
        lb = Gtk.Button(label="Login", css_classes=["pill", "suggested-action"])
        lb.connect("clicked", self.on_login_clicked)
        bx = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        bx.append(lb)
        self.login_page.set_child(bx)

        # 5. Assemble Stack
        self.main_stack.add_named(self.login_page, "login")
        self.main_stack.add_named(self.split_view, "app")

        self.detect_display_metrics()
        saved = KeyManager.load_key()
        if saved:
            self.perform_login(saved)
        else:
            self.main_stack.set_visible_child_name("login")

        GLib.idle_add(self.client.connect_all)
        GLib.timeout_add_seconds(300, self.on_auto_refresh)

    def detect_display_metrics(self):
        try:
            display = Gdk.Display.get_default()
            monitors = display.get_monitors()
            if monitors.get_n_items() > 0:
                monitor = monitors.get_item(0)
                geo = monitor.get_geometry()
                scale = monitor.get_scale_factor()
                if geo.width < 600:
                    target_width = geo.width * scale
                    ImageLoader.MAX_WIDTH = int(target_width)
                else:
                    ImageLoader.MAX_WIDTH = 800
        except:
            ImageLoader.MAX_WIDTH = 800

    def on_settings_clicked(self):
        RelayPreferencesWindow(self.client, self).present()

    def on_menu_selected(self, r_id):
        if r_id == "global": self.switch_feed("global")
        elif r_id == "following": self.switch_feed("following")
        elif r_id == "profile": 
            if self.pub_key: self.show_profile(self.pub_key)
        elif r_id == "search": self.show_search_dialog()
        self.split_view.set_show_content(True)
        self.content_nav.pop_to_page(self.feed_view)

    def on_login_clicked(self, btn):
        from gnostr.dialogs import LoginDialog
        LoginDialog(self.client, self).present()

    def on_logout_clicked(self):
        KeyManager.delete_key()
        self.main_stack.set_visible_child_name("login")
        self.client.set_keys(None, None)

    def on_fab_post_clicked(self, button):
        from gnostr.dialogs import ComposeWindow
        def handle_post(text):
            if self.client.publish_post(text):
                self.add_toast(Adw.Toast(title="Post Published"))
            else:
                self.add_toast(Adw.Toast(title="Failed to Publish Post"))
        win = ComposeWindow(self, handle_post)
        win.present()

    def add_toast(self, toast):
        self.toast_overlay.add_toast(toast)

    def on_refresh_clicked(self):
        self.client.check_connections()
        page = self.content_nav.get_visible_page()
        if page == self.feed_view:
            self.switch_feed(self.active_feed_type)
        self.add_toast(Adw.Toast(title="Refreshing..."))

    def on_auto_refresh(self):
        self.client.check_connections()
        return True

    def show_profile(self, pubkey):
        from .ui.profile_view import ProfileView
        view = ProfileView(self, pubkey)
        page = Adw.NavigationPage(title=f"Profile {pubkey[:8]}", child=view)
        self.content_nav.push_page(page)

    def show_thread(self, event_id, pubkey, content, tags=[]):
        from .ui.thread_view import ThreadView
        view = ThreadView(self, event_id, pubkey, content, tags)
        page = Adw.NavigationPage(title="Thread", child=view)
        self.content_nav.push_page(page)

    def show_search_dialog(self):
        self.add_toast(Adw.Toast(title="Search coming soon"))

    def on_event_received(self, client, eid, pubkey, content, tags_json):
        import json
        tags = json.loads(tags_json)
        
        # If we are on the feed and this post belongs here, add it
        if self.content_nav.get_visible_page() == self.feed_view:
            # Simple heuristic: only add to feed if it's a Kind 1 event
            # (Usually the signal already filtered this, but just in case)
            w = PostWidget(self, pubkey, content, eid, tags)
            self.feed_view.posts_box.prepend(w)

    def on_status_changed(self, client, status):
        emoji = {"CONNECTED": "🟢", "WARNING": "🟡", "DISCONNECTED": "🔴"}.get(status, "⚪")
        self.sidebar.update_status(emoji)

    def on_contacts_updated(self, client): pass
    def on_profile_updated(self, client, pubkey): pass
    def on_metrics_updated(self, client, eid, likes, reposts, replies): pass

    def perform_login(self, priv_hex):
        self.priv_key = priv_hex
        self.pub_key = gnostr.nostr_utils.get_public_key(priv_hex)
        self.main_stack.set_visible_child_name("app")
        self.sidebar.update_status("🟢") 
        self.fab_post.set_visible(True)
        GLib.idle_add(self.client.connect_all)

    def switch_feed(self, feed_type):
        self.active_feed_type = feed_type
        # Clear existing posts
        while self.feed_view.posts_box.get_first_child():
            self.feed_view.posts_box.remove(self.feed_view.posts_box.get_first_child())
        
        cached = []
        if feed_type == "following" and self.pub_key:
            cached = self.db.get_feed_following(self.pub_key)
            contacts = self.db.get_following_list(self.pub_key)
            if contacts:
                self.client.subscribe("sub_following", {"kinds": [1], "authors": contacts[:300], "limit": 50})
        elif feed_type == "global":
            self.client.subscribe("sub_global", {"kinds": [1], "limit": 20}, snapshot=True)
        elif feed_type == "me" and self.pub_key:
            cached = self.db.get_feed_for_user(self.pub_key)
            self.client.subscribe("sub_me", {"kinds": [1], "authors": [self.pub_key], "limit": 20})
        
        for ev in cached:
            w = PostWidget(self, ev['pubkey'], ev['content'], ev['id'], ev.get('tags', []))
            self.feed_view.posts_box.prepend(w)

class GnostrApp(Adw.Application):
    def __init__(self, **kwargs):
        super().__init__(application_id="tech.livingonlinux.gnostr", flags=Gio.ApplicationFlags.FLAGS_NONE, **kwargs)
    def do_activate(self):
        win = self.props.active_window
        if not win: win = MainWindow(application=self)
        win.present()

def main(version):
    app = GnostrApp()
    return app.run(sys.argv)

if __name__ == "__main__":
    main(None)
