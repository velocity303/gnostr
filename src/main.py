#!/usr/bin/env python3
import sys
import json
import threading
import time
import gi
from key_manager import KeyManager
import nostr_utils

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio, GObject

try:
    import websocket
except ImportError:
    print("❌ Websocket-client not found.")
    websocket = None

# --- Constants ---
RELAYS = [
    "wss://relay.damus.io",
    "wss://relay.nostr.band",
    "wss://nos.lol"
]

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
                elif data[0] == "EOSE":
                    pass
            except Exception as e:
                print(f"❌ [{self.url}] Message Error: {e}")

        def on_open(ws):
            print(f"✅ [{self.url}] Connected.")
            self.is_connected = True
            GLib.idle_add(self.on_status, self.url, "Connected")

        def on_error(ws, error):
            print(f"❌ [{self.url}] Error: {error}")
            self.is_connected = False
            GLib.idle_add(self.on_status, self.url, "Error")

        def on_close(ws, close_status_code, close_msg):
            self.is_connected = False
            GLib.idle_add(self.on_status, self.url, "Disconnected")

        self.ws = websocket.WebSocketApp(self.url,
                                         on_open=on_open,
                                         on_message=on_message,
                                         on_error=on_error,
                                         on_close=on_close)
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

    def close(self):
        if self.ws: self.ws.close()

class NostrClient(GObject.Object):
    __gsignals__ = {
        'event-received': (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        'status-changed': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self):
        super().__init__()
        self.relays = []
        self.seen_events = set()

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

        content = event_data.get('content')
        pubkey = event_data.get('pubkey')
        display_name = pubkey[:8] + "..."

        GLib.idle_add(self.emit, 'event-received', display_name, content)

    def _handle_status(self, url, status):
        self.emit('status-changed', f"{status}")

    def subscribe(self, sub_id, filters):
        print(f"DEBUG: Broadcasting Subscription {sub_id}...")
        for relay in self.relays:
            relay.subscribe(sub_id, filters)

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

        self.stack = Adw.ViewStack()
        self.set_content(self.stack)

        # Login Page
        self.login_page = Adw.StatusPage(
            title="Welcome",
            description="Login with your Nostr Key (nsec or hex)",
            icon_name="dialog-password-symbolic"
        )
        login_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        login_box.set_halign(Gtk.Align.CENTER)
        self.key_entry = Adw.PasswordEntryRow(title="Private Key")
        login_box.append(self.key_entry)
        login_btn = Gtk.Button(label="Login", css_classes=["suggested-action"])
        login_btn.connect("clicked", self.on_login_clicked)
        login_box.append(login_btn)
        self.login_page.set_child(login_box)
        self.stack.add_named(self.login_page, "login")

        # Main App
        self.split_view = Adw.NavigationSplitView()
        self.setup_sidebar()
        self.setup_content_area()
        self.stack.add_named(self.split_view, "app")

        saved_key = KeyManager.load_key()
        if saved_key:
            self.perform_login(saved_key)
        else:
            self.stack.set_visible_child_name("login")

    def setup_sidebar(self):
        self.sidebar_page = Adw.NavigationPage(title="Menu", tag="sidebar")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        box.append(header)

        menu_list = Gtk.ListBox()
        menu_list.add_css_class("navigation-sidebar")

        # CRITICAL FIX: Disable selection mode. This forces clicks to register as 'activations'.
        menu_list.set_selection_mode(Gtk.SelectionMode.NONE)
        menu_list.set_activate_on_single_click(True)
        menu_list.connect("row-activated", self.on_menu_selected)

        # Global Row
        self.row_global = Adw.ActionRow(title="Global Feed", icon_name="network-server-symbolic")
        # Ensure row is activatable
        self.row_global.set_activatable(True)
        menu_list.append(self.row_global)

        # My Posts Row
        self.row_me = Adw.ActionRow(title="My Posts", icon_name="user-info-symbolic")
        self.row_me.set_activatable(True)
        menu_list.append(self.row_me)

        # Profile Row
        self.row_profile = Adw.ActionRow(title="Profile", icon_name="avatar-default-symbolic")
        self.row_profile.set_activatable(True)
        menu_list.append(self.row_profile)

        box.append(menu_list)
        box.append(Gtk.Box(vexpand=True))

        logout_btn = Gtk.Button(label="Logout", icon_name="system-log-out-symbolic")
        logout_btn.add_css_class("flat")
        logout_btn.set_margin_bottom(10)
        logout_btn.set_margin_start(10)
        logout_btn.set_margin_end(10)
        logout_btn.connect("clicked", self.on_logout_clicked)
        box.append(logout_btn)

        self.status_label = Gtk.Label(label="Offline", css_classes=["dim-label"])
        self.status_label.set_margin_bottom(12)
        box.append(self.status_label)

        self.sidebar_page.set_child(box)
        self.split_view.set_sidebar(self.sidebar_page)

    def setup_content_area(self):
        self.content_nav = Adw.NavigationView()
        self.content_wrapper_page = Adw.NavigationPage(title="Content", tag="content_wrapper")
        self.content_wrapper_page.set_child(self.content_nav)
        self.split_view.set_content(self.content_wrapper_page)

        # Feed Page
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

        # Profile Page
        self.profile_page = Adw.NavigationPage(title="My Profile", tag="profile")
        prof_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20, margin_top=40)
        prof_box.set_halign(Gtk.Align.CENTER)
        prof_box.append(Adw.HeaderBar())

        avatar = Gtk.Image.new_from_icon_name("avatar-default-symbolic")
        avatar.set_pixel_size(96)
        prof_box.append(avatar)

        self.lbl_npub = Gtk.Label(label="npub...", css_classes=["title-2"], selectable=True)
        prof_box.append(self.lbl_npub)
        self.profile_page.set_child(prof_box)

    def perform_login(self, priv_hex):
        self.priv_key = priv_hex
        self.pub_key = nostr_utils.get_public_key(priv_hex)

        if self.pub_key:
            npub = nostr_utils.hex_to_nsec(self.pub_key).replace("nsec", "npub")
            self.lbl_npub.set_text(npub[:12] + "..." + npub[-12:])
        else:
            self.lbl_npub.set_text("Error deriving public key")

        self.stack.set_visible_child_name("app")

        if not hasattr(self, 'client'):
            self.client = NostrClient()
            self.client.connect("event-received", self.on_event_received)
            self.client.connect("status-changed", self.on_client_status_changed)
            GLib.idle_add(self.client.connect_all)

        self.active_feed_type = "global"
        GLib.timeout_add(1500, lambda: self.switch_feed("global"))

    def on_client_status_changed(self, client, status):
        self.status_label.set_text(status)

    def on_login_clicked(self, btn):
        raw = self.key_entry.get_text().strip()
        final_hex = None
        if nostr_utils.is_valid_hex_key(raw):
            final_hex = raw
        elif raw.startswith("nsec"):
            final_hex = nostr_utils.nsec_to_hex(raw)

        if final_hex:
            KeyManager.save_key(final_hex)
            self.perform_login(final_hex)
        else:
            print("Invalid key")

    def on_logout_clicked(self, btn):
        KeyManager.delete_key()
        self.stack.set_visible_child_name("login")
        self.key_entry.set_text("")
        if hasattr(self, 'client'):
            self.client.close()
            del self.client

    def on_menu_selected(self, box, row):
        if not row: return
        print(f"DEBUG: Menu clicked on {row}")

        if row == self.row_global:
            self.switch_feed("global")
        elif row == self.row_me:
            self.switch_feed("me")
        elif row == self.row_profile:
            self.content_nav.push(self.profile_page)

    def switch_feed(self, feed_type):
        print(f"DEBUG: switch_feed called with {feed_type}")
        self.active_feed_type = feed_type

        if feed_type == "global":
            self.feed_page.set_title("Global Feed")
        elif feed_type == "me":
            self.feed_page.set_title("My Posts")

        self.content_nav.pop_to_page(self.feed_page)

        # Clear posts
        child = self.posts_box.get_first_child()
        while child:
            self.posts_box.remove(child)
            child = self.posts_box.get_first_child()

        if not hasattr(self, 'client'): return

        if feed_type == "global":
            filter = {"kinds": [1], "limit": 20}
            self.client.subscribe("sub_global", filter)

        elif feed_type == "me":
            if self.pub_key:
                filter = {"kinds": [1], "authors": [self.pub_key], "limit": 20}
                self.client.subscribe("sub_me", filter)
            else:
                self.status_label.set_text("Err: No PubKey")

    def on_event_received(self, client, name, content):
        card = Adw.Bin(css_classes=["card"])
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)

        lbl_author = Gtk.Label(label=name, xalign=0, css_classes=["heading", "dim-label"])
        lbl_content = Gtk.Label(label=content, xalign=0, wrap=True, selectable=True)

        vbox.append(lbl_author)
        vbox.append(lbl_content)
        card.set_child(vbox)
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
