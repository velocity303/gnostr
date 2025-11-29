#!/usr/bin/env python3
import sys
import json
import threading
import time
import urllib.request
import gi
from key_manager import KeyManager
import nostr_utils
from database import Database

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio, GObject, Gdk, GdkPixbuf

try:
    import websocket
except ImportError:
    print("❌ Websocket-client not found.")
    websocket = None

RELAYS = [
    "wss://relay.damus.io",
    "wss://relay.nostr.band",
    "wss://nos.lol",
    "wss://relay.primal.net"
]

# --- Helper: Image Loader ---
class ImageLoader:
    @staticmethod
    def load_avatar(url, callback):
        def _fetch():
            try:
                if not url or not url.startswith("http"):
                    return

                with urllib.request.urlopen(url, timeout=5) as response:
                    data = response.read()

                loader = GdkPixbuf.PixbufLoader()
                loader.write(data)
                loader.close()
                pixbuf = loader.get_pixbuf()

                w, h = pixbuf.get_width(), pixbuf.get_height()
                if w > 128 or h > 128:
                    pixbuf = pixbuf.scale_simple(64, 64, GdkPixbuf.InterpType.BILINEAR)

                texture = Gdk.Texture.new_for_pixbuf(pixbuf)
                GLib.idle_add(callback, texture)
            except Exception as e:
                pass

        threading.Thread(target=_fetch, daemon=True).start()

class NostrRelay(GObject.Object):
    def __init__(self, url, on_event_callback, on_status_callback):
        super().__init__()
        self.url = url
        self.ws = None
        self.on_event = on_event_callback
        self.on_status = on_status_callback
        self.is_connected = False
        self.current_sub_id = None

    def start(self):
        def on_message(ws, message):
            try:
                data = json.loads(message)
                if data[0] == "EVENT" and len(data) >= 3:
                    self.on_event(data[2])
            except Exception as e:
                pass

        def on_open(ws):
            self.is_connected = True
            GLib.idle_add(self.on_status, self.url, "Connected")

        def on_error(ws, error):
            self.is_connected = False
            GLib.idle_add(self.on_status, self.url, "Error")

        def on_close(ws, c, m):
            self.is_connected = False
            GLib.idle_add(self.on_status, self.url, "Disconnected")

        self.ws = websocket.WebSocketApp(self.url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
        wst = threading.Thread(target=self.ws.run_forever)
        wst.daemon = True
        wst.start()

    def subscribe(self, sub_id, filters):
        if not self.is_connected: return
        if self.current_sub_id and self.current_sub_id != sub_id:
            self.ws.send(json.dumps(["CLOSE", self.current_sub_id]))
        self.current_sub_id = sub_id
        req = ["REQ", sub_id] + (filters if isinstance(filters, list) else [filters])
        self.ws.send(json.dumps(req))

    def request_once(self, sub_id, filters):
        if not self.is_connected: return
        req = ["REQ", sub_id] + (filters if isinstance(filters, list) else [filters])
        self.ws.send(json.dumps(req))

    def close(self):
        if self.ws: self.ws.close()

class NostrClient(GObject.Object):
    __gsignals__ = {
        'event-received': (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        'profile-updated': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'contacts-updated': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'status-changed': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, db):
        super().__init__()
        self.relays = []
        self.seen_events = set()
        self.db = db
        self.my_pubkey = None
        self.requested_profiles = set()

    def set_pubkey(self, pubkey):
        self.my_pubkey = pubkey

    def connect_all(self):
        if not websocket: return
        for url in RELAYS:
            relay = NostrRelay(url, self._handle_event, self._handle_status)
            relay.start()
            self.relays.append(relay)

    def _handle_event(self, event_data):
        eid = event_data.get('id')
        if eid in self.seen_events: return
        self.seen_events.add(eid)

        kind = event_data['kind']
        pubkey = event_data['pubkey']

        self.db.save_event(event_data)

        if kind == 0:
            self.db.save_profile(pubkey, event_data['content'], event_data['created_at'])
            GLib.idle_add(self.emit, 'profile-updated', pubkey)

        elif kind == 3:
            if self.my_pubkey and pubkey == self.my_pubkey:
                contacts = nostr_utils.extract_followed_pubkeys(event_data)
                self.db.save_contacts(self.my_pubkey, contacts)
                GLib.idle_add(self.emit, 'contacts-updated')

        elif kind == 1:
            content = event_data.get('content')
            GLib.idle_add(self.emit, 'event-received', pubkey, content)

    def _handle_status(self, url, status):
        self.emit('status-changed', f"{status}")

    def subscribe(self, sub_id, filters):
        for relay in self.relays:
            relay.subscribe(sub_id, filters)

    # --- MISSING METHOD ADDED HERE ---
    def fetch_contacts(self):
        """Fetches the user's contact list (Kind 3)."""
        if self.my_pubkey:
            print("DEBUG: Fetching Contact List...")
            filter = {"kinds": [3], "authors": [self.my_pubkey], "limit": 1}
            # We use a subscription so we get updates if they change it elsewhere
            self.subscribe("sub_contacts", filter)

    def fetch_profile(self, pubkey):
        if pubkey in self.requested_profiles: return
        self.requested_profiles.add(pubkey)

        filter = {"kinds": [0], "authors": [pubkey], "limit": 1}
        sub_id = f"meta_{pubkey[:8]}"
        for relay in self.relays:
            relay.request_once(sub_id, filter)

    def close(self):
        for relay in self.relays:
            relay.close()

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_title("Gnostr")
        self.set_default_size(900, 700)
        self.priv_key = None
        self.pub_key = None
        self.active_feed_type = "global"
        self.db = Database()
        self.stack = Adw.ViewStack()
        self.set_content(self.stack)

        self.login_page = Adw.StatusPage(title="Welcome", description="Login with Nostr Key", icon_name="dialog-password-symbolic")
        login_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        login_box.set_halign(Gtk.Align.CENTER)
        self.key_entry = Adw.PasswordEntryRow(title="Private Key")
        login_box.append(self.key_entry)
        login_btn = Gtk.Button(label="Login", css_classes=["suggested-action"])
        login_btn.connect("clicked", self.on_login_clicked)
        login_box.append(login_btn)
        self.login_page.set_child(login_box)
        self.stack.add_named(self.login_page, "login")

        self.split_view = Adw.NavigationSplitView()
        self.setup_sidebar()
        self.setup_content_area()
        self.stack.add_named(self.split_view, "app")

        saved_key = KeyManager.load_key()
        if saved_key: self.perform_login(saved_key)
        else: self.stack.set_visible_child_name("login")

    def setup_sidebar(self):
        self.sidebar_page = Adw.NavigationPage(title="Menu", tag="sidebar")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        box.append(header)
        menu_list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        menu_list.set_selection_mode(Gtk.SelectionMode.NONE)
        menu_list.set_activate_on_single_click(True)
        menu_list.connect("row-activated", self.on_menu_selected)

        self.row_global = Adw.ActionRow(title="Global Feed", icon_name="network-server-symbolic")
        self.row_global.set_activatable(True)
        menu_list.append(self.row_global)
        self.row_following = Adw.ActionRow(title="Following", icon_name="system-users-symbolic")
        self.row_following.set_activatable(True)
        menu_list.append(self.row_following)
        self.row_me = Adw.ActionRow(title="My Posts", icon_name="user-info-symbolic")
        self.row_me.set_activatable(True)
        menu_list.append(self.row_me)
        self.row_profile = Adw.ActionRow(title="Profile", icon_name="avatar-default-symbolic")
        self.row_profile.set_activatable(True)
        menu_list.append(self.row_profile)

        box.append(menu_list)
        box.append(Gtk.Box(vexpand=True))
        logout_btn = Gtk.Button(label="Logout", icon_name="system-log-out-symbolic", css_classes=["flat"], margin_bottom=10, margin_start=10, margin_end=10)
        logout_btn.connect("clicked", self.on_logout_clicked)
        box.append(logout_btn)
        self.status_label = Gtk.Label(label="Offline", css_classes=["dim-label"], margin_bottom=12)
        box.append(self.status_label)
        self.sidebar_page.set_child(box)
        self.split_view.set_sidebar(self.sidebar_page)

    def setup_content_area(self):
        self.content_nav = Adw.NavigationView()
        self.content_wrapper_page = Adw.NavigationPage(title="Content", tag="content_wrapper")
        self.content_wrapper_page.set_child(self.content_nav)
        self.split_view.set_content(self.content_wrapper_page)

        self.feed_page = Adw.NavigationPage(title="Feed", tag="feed")
        feed_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.feed_header = Adw.HeaderBar()
        feed_box.append(self.feed_header)
        scrolled = Gtk.ScrolledWindow(vexpand=True)
        clamp = Adw.Clamp(maximum_size=600)
        self.posts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        clamp.set_child(self.posts_box)
        scrolled.set_child(clamp)
        feed_box.append(scrolled)
        self.feed_page.set_child(feed_box)
        self.content_nav.add(self.feed_page)

        self.profile_page = Adw.NavigationPage(title="My Profile", tag="profile")
        prof_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20, margin_top=40)
        prof_box.set_halign(Gtk.Align.CENTER)
        prof_box.append(Adw.HeaderBar())

        self.prof_avatar = Adw.Avatar(size=96, show_initials=True)
        prof_box.append(self.prof_avatar)

        self.lbl_name = Gtk.Label(css_classes=["title-1"])
        prof_box.append(self.lbl_name)

        self.lbl_npub = Gtk.Label(css_classes=["caption", "dim-label"], selectable=True)
        prof_box.append(self.lbl_npub)

        self.lbl_about = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=40)
        prof_box.append(self.lbl_about)

        self.profile_page.set_child(prof_box)

    def perform_login(self, priv_hex):
        self.priv_key = priv_hex
        self.pub_key = nostr_utils.get_public_key(priv_hex)
        self.stack.set_visible_child_name("app")

        if not hasattr(self, 'client'):
            self.client = NostrClient(self.db)
            self.client.set_pubkey(self.pub_key)
            self.client.connect("event-received", self.on_event_received)
            self.client.connect("status-changed", lambda c, s: self.status_label.set_text(s))
            self.client.connect("contacts-updated", lambda c: self.switch_feed("following") if self.active_feed_type == "following" else None)
            self.client.connect("profile-updated", self.on_profile_updated)
            GLib.idle_add(self.client.connect_all)

        self.active_feed_type = "global"
        self.load_my_profile_ui()

        GLib.timeout_add(1500, lambda: self.switch_feed("global"))
        GLib.timeout_add(2000, lambda: self.client.fetch_profile(self.pub_key))
        # Fixed the crash here: fetch_contacts is now defined
        GLib.timeout_add(2500, self.client.fetch_contacts)

    def load_my_profile_ui(self):
        if not self.pub_key: return
        npub = nostr_utils.hex_to_nsec(self.pub_key).replace("nsec", "npub")
        self.lbl_npub.set_text(npub[:12] + "..." + npub[-12:])

        profile = self.db.get_profile(self.pub_key)
        if profile:
            name = profile.get('display_name') or profile.get('name') or "Anonymous"
            self.lbl_name.set_text(name)
            self.prof_avatar.set_text(name)
            self.lbl_about.set_text(profile.get('about') or "")
            if profile.get('picture'):
                ImageLoader.load_avatar(profile['picture'], lambda t: self.prof_avatar.set_custom_image(t))

    def on_profile_updated(self, client, pubkey):
        if pubkey == self.pub_key:
            self.load_my_profile_ui()

    def on_login_clicked(self, btn):
        raw = self.key_entry.get_text().strip()
        final_hex = None
        if nostr_utils.is_valid_hex_key(raw): final_hex = raw
        elif raw.startswith("nsec"): final_hex = nostr_utils.nsec_to_hex(raw)
        if final_hex:
            KeyManager.save_key(final_hex)
            self.perform_login(final_hex)

    def on_logout_clicked(self, btn):
        KeyManager.delete_key()
        self.stack.set_visible_child_name("login")
        self.key_entry.set_text("")
        if hasattr(self, 'client'): self.client.close(); del self.client

    def on_menu_selected(self, box, row):
        if not row: return
        if row == self.row_global: self.switch_feed("global")
        elif row == self.row_following: self.switch_feed("following")
        elif row == self.row_me: self.switch_feed("me")
        elif row == self.row_profile: self.content_nav.push(self.profile_page)

    def switch_feed(self, feed_type):
        self.active_feed_type = feed_type
        if feed_type == "global": self.feed_page.set_title("Global Feed")
        elif feed_type == "following": self.feed_page.set_title("Following")
        elif feed_type == "me": self.feed_page.set_title("My Posts")

        self.content_nav.pop_to_page(self.feed_page)

        child = self.posts_box.get_first_child()
        while child: self.posts_box.remove(child); child = self.posts_box.get_first_child()

        cached = []
        if feed_type == "me" and self.pub_key: cached = self.db.get_feed_for_user(self.pub_key)
        elif feed_type == "following" and self.pub_key: cached = self.db.get_feed_following(self.pub_key)

        for ev in cached: self.render_event(ev['pubkey'], ev['content'])

        if not hasattr(self, 'client'): return

        if feed_type == "global":
            self.client.subscribe("sub_global", {"kinds": [1], "limit": 20})
        elif feed_type == "me" and self.pub_key:
            self.client.subscribe("sub_me", {"kinds": [1], "authors": [self.pub_key], "limit": 20})
        elif feed_type == "following" and self.pub_key:
            contacts = self.db.get_following_list(self.pub_key)
            if contacts: self.client.subscribe("sub_following", {"kinds": [1], "authors": contacts[:300], "limit": 50})

    def on_event_received(self, client, pubkey, content):
        if not self.db.get_profile(pubkey):
            self.client.fetch_profile(pubkey)
        self.render_event(pubkey, content)

    def render_event(self, pubkey, content):
        card = Adw.Bin(css_classes=["card"])
        row = Adw.ActionRow()

        profile = self.db.get_profile(pubkey)
        name = pubkey[:8]
        if profile:
            name = profile.get('display_name') or profile.get('name') or name

        row.set_title(name)

        # FIX: Escape text to prevent markup errors with & symbols
        safe_content = GLib.markup_escape_text(content)
        row.set_subtitle(safe_content)
        row.set_subtitle_lines(0)

        avatar = Adw.Avatar(size=40, show_initials=True, text=name)
        if profile and profile.get('picture'):
            ImageLoader.load_avatar(profile['picture'], lambda t: avatar.set_custom_image(t))
        row.add_prefix(avatar)

        card.set_child(row)
        self.posts_box.prepend(card)

class GnostrApp(Adw.Application):
    def __init__(self, **kwargs):
        super().__init__(application_id="me.velocitynet.Gnostr", flags=Gio.ApplicationFlags.FLAGS_NONE, **kwargs)
    def do_activate(self):
        win = self.props.active_window
        if not win: win = MainWindow(application=self)
        win.present()

def main(version):
    app = GnostrApp()
    return app.run(sys.argv)

if __name__ == "__main__":
    main(None)
