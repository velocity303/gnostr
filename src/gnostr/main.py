#!/usr/bin/env python3
import sys
import os
import json
import time
import gi
import traceback
import gnostr
from .key_manager import KeyManager
from .database import Database
from .client import NostrClient
from .renderer import ContentRenderer, ImageLoader
from .dialogs import LoginDialog, RelayPreferencesWindow
from .ui.sidebar import Sidebar
from .ui.feed_view import FeedView
from .ui.post_widget import PostWidget

# Ensure package imports work when run as __main__
if __name__ == "__main__":
    # Add parent directory to sys.path so `import gnostr` works
    parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent not in sys.path:
        sys.path.insert(0, parent)

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gio, Pango, Gdk


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_title("Gnostr")
        self.set_default_size(950, 700)

        self.db = Database()
        self.client = NostrClient(self.db)
        self.client.connect("event-received", self.on_event_received)
        self.client.connect("quote-event-received", self.on_quote_event_received)
        self.client.connect("status-changed", self.on_status_changed)
        self.client.connect("contacts-updated", self.on_contacts_updated)
        self.client.connect("profile-updated", self.on_profile_updated)
        self.client.connect("metrics-updated", self.on_metrics_updated)
        self.client.connect("publish-result", self.on_publish_result)
        self.client.connect("relay-log-updated", self.on_relay_log_updated)

        self.priv_key = None
        self.pub_key = None
        self.active_feed_type = "following"
        self.event_widgets = {}
        self.active_profile_pubkey = None

        # 1. Root: Toast Overlay
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        # 2. Main Stack (direct child of ToastOverlay — no Gtk.Overlay wrapper)
        self.main_stack = Adw.ViewStack()
        self.toast_overlay.set_child(self.main_stack)

        # FAB Button — place on the split view's content page via its own overlay
        self.fab_post = Gtk.Button(
            icon_name="edit-create-symbolic", css_classes=["suggested-action", "pill"]
        )
        self.fab_post.set_valign(Gtk.Align.END)
        self.fab_post.set_halign(Gtk.Align.END)
        self.fab_post.set_margin_bottom(20)
        self.fab_post.set_margin_end(20)
        self.fab_post.connect("clicked", self.on_fab_post_clicked)
        self.fab_post.set_visible(False)

        # 3. App View: Split View
        self.split_view = Adw.NavigationSplitView()
        bp = Adw.Breakpoint.new(
            Adw.BreakpointCondition.new_length(
                Adw.BreakpointConditionLengthType.MAX_WIDTH, 800, Adw.LengthUnit.SP
            )
        )
        bp.add_setter(self.split_view, "collapsed", True)
        self.add_breakpoint(bp)

        # UI Components
        self.sidebar = Sidebar(self)
        self.sidebar_page = Adw.NavigationPage(title="Menu", tag="sidebar")
        self.sidebar_page.set_child(self.sidebar)
        self.split_view.set_sidebar(self.sidebar_page)

        self.feed_view = FeedView(self)
        self.content_nav = Adw.NavigationView()

        # Overlay wraps the navigation view, FAB floats on top
        self.content_overlay = Gtk.Overlay()
        self.content_overlay.set_child(self.content_nav)
        self.content_overlay.add_overlay(self.fab_post)

        # NavigationPage contains the overlay — split_view expects a NavigationPage
        self.feed_page = Adw.NavigationPage(title="Content", child=self.content_overlay)
        self.content_nav.push(self.feed_view)
        self.split_view.set_content(self.feed_page)

        # 4. Login View
        self.login_page = Adw.StatusPage(
            title="Welcome", icon_name="avatar-default-symbolic"
        )
        lb = Gtk.Button(label="Login", css_classes=["pill", "suggested-action"])
        lb.connect("clicked", self.on_login_clicked)
        bx = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
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
        # Surface publishes that never got a relay OK (~30s expiry, sweep every 15s).
        GLib.timeout_add_seconds(15, self._sweep_publishes)

        # Populate the feed from the DB right away so Back never lands on a blank
        # feed (the startup subscribe is a no-op until relays connect; on_status_changed
        # re-runs switch_feed once the first relay is live).
        self._feed_live = False
        if saved:
            GLib.idle_add(lambda: self.switch_feed(self.active_feed_type))

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
        except Exception:
            ImageLoader.MAX_WIDTH = 800

    def on_settings_clicked(self):
        RelayPreferencesWindow(self.client, self).present()

    def on_menu_selected(self, r_id):
        if r_id == "global":
            self.switch_feed("global")
        elif r_id == "following":
            self.switch_feed("following")
        elif r_id == "profile":
            if self.pub_key:
                self.show_profile(self.pub_key)
        elif r_id == "search":
            self.show_search_dialog()
        self.split_view.set_show_content(True)

    def on_login_clicked(self, btn):
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
        try:
            from gnostr.ui.profile_view import ProfileView
        except ImportError:
            from .ui.profile_view import ProfileView
        view = ProfileView(self, pubkey)
        page = Adw.NavigationPage(title=f"Profile {pubkey[:8]}", child=view)
        self.content_nav.push(page)

    def refresh_profile(self, pubkey):
        # Manual profile refresh: re-fetch metadata, then re-push a fresh
        # ProfileView so posts reload from the DB.
        self.client.check_connections()
        self.client.fetch_profile(pubkey)
        self.content_nav.pop()
        self.show_profile(pubkey)

    def show_thread(self, event_id, pubkey, content, tags=[]):
        try:
            from gnostr.ui.thread_view import ThreadView
        except ImportError:
            from .ui.thread_view import ThreadView
        # Prefer the DB-cached event so the hero renders accurate post data even
        # when the feed copy was stale/incomplete (e.g. quote cards opened with
        # "Loading...").
        cached = self.db.get_event_by_id(event_id)
        if cached:
            pubkey = cached["pubkey"]
            content = cached["content"]
            tags = cached.get("tags", [])
        view = ThreadView(self, event_id, pubkey, content, tags)
        page = Adw.NavigationPage(title="Thread", child=view)
        self.content_nav.push(page)
        # Fetch thread data including reactions
        self.client.fetch_thread(event_id)

    def show_search_dialog(self):
        from gnostr.dialogs import SearchDialog

        SearchDialog(self).present()

    def on_event_received(self, client, eid, pubkey, content, tags_json):
        import json

        tags = json.loads(tags_json)

        # If we are on the feed and this post belongs here, add it
        if self.content_nav.get_visible_page() == self.feed_view:
            # Dedup against posts already rendered: switch_feed loads the DB cache,
            # then the live subscription re-delivers the same events (they
            # persisted from a prior session, so they're not in seen_events and
            # would otherwise render twice).
            child = self.feed_view.posts_box.get_first_child()
            while child is not None:
                if getattr(child, "event_id", None) == eid:
                    return
                child = child.get_next_sibling()
            w = PostWidget(self, pubkey, content, eid, tags)
            # Slot into the correct time position (newest-at-top), not a blind
            # prepend — backfilled/older posts keep the feed in time order.
            PostWidget.insert_time_sorted(self.feed_view.posts_box, w)

    def on_quote_event_received(self, client, event_json):
        """Populate pending quote/naddr cards when their event arrives.
        Fires for ALL kinds (addressable events never hit event-received)."""
        try:
            ev = json.loads(event_json)
        except Exception:
            return
        eid = ev.get("id")
        kind = ev.get("kind")
        pubkey = ev.get("pubkey")
        tags = ev.get("tags", [])

        # Addressable coordinate for naddr cards: kind:pubkey:d-tag.
        d_tag = ""
        for t in tags:
            if len(t) >= 2 and t[0] == "d":
                d_tag = t[1]
                break
        coordinate = f"{kind}:{pubkey}:{d_tag}" if d_tag else None

        for w in self.event_widgets.values():
            for match_key, quote_box in getattr(w, "quote_widgets", None) or ():
                mtype, mval = match_key
                matched = False
                if mtype == "id" and mval == eid:
                    matched = True
                elif mtype == "a" and coordinate and mval == coordinate:
                    matched = True
                if not matched:
                    continue
                # Clear the "Loading Quoted Event..." label, then render content.
                child = quote_box.get_first_child()
                while child is not None:
                    quote_box.remove(child)
                    child = quote_box.get_first_child()
                ContentRenderer._build_quote_content(quote_box, ev, self)

    def on_status_changed(self, client, status):
        emoji = {"CONNECTED": "🟢", "WARNING": "🟡", "DISCONNECTED": "🔴"}.get(
            status, "⚪"
        )
        self.sidebar.update_status(emoji)
        # First relay connection -> re-run switch_feed so the feed subscribes live
        # (the startup switch_feed ran before relays connected, so its subscribe
        # was a no-op).
        if status == "CONNECTED" and not self._feed_live:
            self._feed_live = True
            GLib.idle_add(lambda: self.switch_feed(self.active_feed_type))

    def on_contacts_updated(self, client):
        pass

    def on_profile_updated(self, client, pubkey):
        # Profile metadata (kind 0) arrived — refresh avatars/names in existing
        # post widgets so previously-missing profile pics/names actually appear.
        prof = self.db.get_profile(pubkey)
        if not prof:
            return
        name = prof.get("display_name") or prof.get("name") or pubkey[:8]
        pic = prof.get("picture")
        for w in self.event_widgets.values():
            if getattr(w, "pubkey", None) == pubkey:
                if name:
                    w.lbl_name.set_label(name)
                if pic:
                    ImageLoader.load_avatars(
                        pic, lambda t, w=w: w.avatar.set_custom_image(t)
                    )
            # Update inline @mention labels that reference this pubkey.
            for lbl in getattr(w, "inline_mention_labels", None) or ():
                if not hasattr(lbl, "mention_fragments"):
                    continue
                new_frags = []
                changed = False
                for pk, frag in lbl.mention_fragments:
                    if pk == pubkey:
                        nm = prof.get("display_name") or prof.get("name") or pk[:8]
                        disp = (
                            f'<span weight="bold">@{GLib.markup_escape_text(nm)}</span>'
                        )
                        new_frags.append((pk, f'<a href="nostr:{pk}">{disp}</a>'))
                        changed = True
                    else:
                        new_frags.append((pk, frag))
                if changed:
                    lbl.mention_fragments = new_frags
                    lbl.set_label("".join(frag for _, frag in new_frags))

    def on_metrics_updated(self, client, eid, likes, reposts, replies):
        # Update labels on PostWidgets by event_id
        if eid in self.event_widgets:
            widget = self.event_widgets[eid]
            widget.lbl_likes.set_label(str(likes))
            widget.lbl_reposts.set_label(str(reposts))
            widget.lbl_replies.set_label(str(replies))

    def _sweep_publishes(self):
        # Callback for GLib.timeout_add_seconds — returns True to keep repeating.
        self.client.sweep_pending_publishes()
        return True

    def on_publish_result(self, client, event_id, accepted, message, relay_url, label):
        # Feedback for whether likes/follows/reposts/replies reached a relay.
        if accepted:
            self.add_toast(Adw.Toast(title=f"{label} ✓"))
        else:
            self.add_toast(Adw.Toast(title=f"{label} failed: {message}"))

    def on_relay_log_updated(self, client, line):
        # Append to the sidebar Relay Activity pane.
        self.sidebar.append_relay_log(line)

    def perform_login(self, priv_hex):
        self.priv_key = priv_hex
        self.pub_key = gnostr.nostr_utils.get_public_key(priv_hex)
        # Push keys to the client — the fresh-login dialog path also calls
        # set_keys, so the saved-key startup path must too or publish_* (like,
        # repost, reply, follow) silently no-ops with "No private key loaded".
        self.client.set_keys(self.pub_key, self.priv_key)
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
                self.client.subscribe(
                    "sub_following",
                    {"kinds": [1], "authors": contacts[:300], "limit": 50},
                )
        elif feed_type == "global":
            self.client.subscribe(
                "sub_global", {"kinds": [1], "limit": 20}, snapshot=True
            )
        elif feed_type == "me" and self.pub_key:
            cached = self.db.get_feed_for_user(self.pub_key)
            self.client.subscribe(
                "sub_me", {"kinds": [1], "authors": [self.pub_key], "limit": 20}
            )

        for ev in cached:
            w = PostWidget(
                self,
                ev["pubkey"],
                ev["content"],
                ev["id"],
                ev.get("tags", []),
                created_at=ev.get("created_at"),
            )
            # cached is newest-first (ORDER BY created_at DESC) — append builds
            # newest→oldest top-to-bottom. No per-item sorted rebuild needed.
            self.feed_view.posts_box.append(w)


class GnostrApp(Adw.Application):
    def __init__(self, **kwargs):
        super().__init__(
            application_id="tech.livingonlinux.gnostr",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
            **kwargs,
        )

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = MainWindow(application=self)
        win.present()
        # Initialize GStreamer for video/GIF support
        try:
            from gi.repository import Gst

            Gst.init(None)
        except Exception as e:
            print(f"GStreamer init failed: {e}")
        # Register the liked-state accent rule (idempotent per provider load).
        css = Gtk.CssProvider()
        css.load_from_string(
            ".liked { color: @accent_color; }" ".liked image { color: @accent_color; }"
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )


def main(version):
    app = GnostrApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    main(None)
