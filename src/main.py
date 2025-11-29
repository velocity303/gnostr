#!/usr/bin/env python3
import sys
import json
import threading
import time
import gi
from key_manager import KeyManager
import nostr_utils  # Import the new utilities

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio, GObject

# Try importing websocket, handle failure gracefully if not installed
try:
    import websocket
except ImportError:
    print("Websocket-client not found. Install it or run via Flatpak manifest.")
    websocket = None

# --- Constants ---
RELAY_URL = "wss://relay.damus.io"
NOSTR_FILTER = {
    "kinds": [1],
    "limit": 20
}

class NostrClient(GObject.Object):
    """
    Handles the Nostr Protocol connection in a background thread.
    """
    __gsignals__ = {
        'event-received': (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        'status-changed': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self):
        super().__init__()
        self.ws = None
        self.running = False

    def connect_and_listen(self):
        if not websocket:
            self.emit('status-changed', "Error: websocket-client library missing")
            return

        def on_message(ws, message):
            data = json.loads(message)
            if data[0] == "EVENT" and len(data) >= 3:
                event_content = data[2]['content']
                pubkey = data[2]['pubkey'][:8] + "..."
                GLib.idle_add(self.emit, 'event-received', pubkey, event_content)

        def on_error(ws, error):
            GLib.idle_add(self.emit, 'status-changed', f"Error: {error}")

        def on_close(ws, close_status_code, close_msg):
            GLib.idle_add(self.emit, 'status-changed', "Disconnected")

        def on_open(ws):
            GLib.idle_add(self.emit, 'status-changed', "Connected! Fetching notes...")
            req = ["REQ", "subscription_id_1", NOSTR_FILTER]
            ws.send(json.dumps(req))

        self.ws = websocket.WebSocketApp(RELAY_URL,
                                         on_open=on_open,
                                         on_message=on_message,
                                         on_error=on_error,
                                         on_close=on_close)

        self.running = True
        wst = threading.Thread(target=self.ws.run_forever)
        wst.daemon = True
        wst.start()

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.set_title("Gnostr")
        self.set_default_size(800, 600)

        # Use an Adw.ViewStack to switch between Login and Main App
        self.stack = Adw.ViewStack()
        self.set_content(self.stack)

        # -- PAGE 1: Login Screen --
        self.login_page = Adw.StatusPage(
            title="Welcome to Gnostr",
            description="Enter your private key (nsec or hex) to start.",
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

        # -- PAGE 2: Main App (Adaptive Split View) --
        self.split_view = Adw.NavigationSplitView()

        # Sidebar
        self.sidebar_page = Adw.NavigationPage(title="Feeds", tag="sidebar")
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        sidebar_header = Adw.HeaderBar()
        sidebar_header.set_show_end_title_buttons(False)
        sidebar_box.append(sidebar_header)

        self.status_label = Gtk.Label(label="Connecting...")
        sidebar_box.append(self.status_label)

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        list_box.add_css_class("navigation-sidebar")
        row = Adw.ActionRow(title="Global Feed", subtitle=RELAY_URL)
        row.add_suffix(Gtk.Image.new_from_icon_name("network-wired-symbolic"))
        list_box.append(row)
        list_box.connect("row-activated", self.on_feed_selected)
        sidebar_box.append(list_box)

        # Logout Button
        logout_btn = Gtk.Button(label="Logout")
        logout_btn.set_margin_top(20)
        logout_btn.connect("clicked", self.on_logout_clicked)
        sidebar_box.append(logout_btn)

        self.sidebar_page.set_child(sidebar_box)
        self.split_view.set_sidebar(self.sidebar_page)

        # Content
        self.content_page = Adw.NavigationPage(title="Global Feed", tag="content")
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content_header = Adw.HeaderBar()
        content_box.append(content_header)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        clamp = Adw.Clamp()
        clamp.set_maximum_size(600)
        scrolled.set_child(clamp)
        self.posts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.posts_box.set_margin_top(12)
        self.posts_box.set_margin_bottom(12)
        self.posts_box.set_margin_start(12)
        self.posts_box.set_margin_end(12)
        clamp.set_child(self.posts_box)
        content_box.append(scrolled)
        self.content_page.set_child(content_box)
        self.split_view.set_content(self.content_page)

        self.stack.add_named(self.split_view, "app")

        # -- Auto-Login Check --
        saved_key = KeyManager.load_key()
        if saved_key:
            # We assume saved_key is stored as HEX
            print(f"Found saved key (Hex): {saved_key[:6]}...")
            self.do_login_success()
        else:
            self.stack.set_visible_child_name("login")

    def do_login_success(self):
        self.stack.set_visible_child_name("app")
        # Initialize Client if not already running
        if not hasattr(self, 'client'):
            self.client = NostrClient()
            self.client.connect("event-received", self.on_event_received)
            self.client.connect("status-changed", self.on_status_changed)
            GLib.timeout_add(500, self.client.connect_and_listen)

    def on_login_clicked(self, btn):
        raw_input = self.key_entry.get_text().strip()
        final_hex_key = None

        # 1. Check if it's already Hex
        if nostr_utils.is_valid_hex_key(raw_input):
            print("Detected Raw Hex Key")
            final_hex_key = raw_input

        # 2. Check if it's nsec and decode it
        elif raw_input.startswith("nsec"):
            print("Detected Bech32 Key")
            hex_result = nostr_utils.nsec_to_hex(raw_input)
            if hex_result:
                final_hex_key = hex_result
            else:
                print("Error: Invalid checksum or encoding")

        # 3. Save or Fail
        if final_hex_key:
            print(f"Saving normalized Hex Key: {final_hex_key[:6]}...")
            if KeyManager.save_key(final_hex_key):
                self.do_login_success()
            else:
                print("Error: Failed to save to keyring")
        else:
            # TODO: Show a real UI Toast here
            print("Invalid key format. Please enter 'nsec1...' or 64-char Hex.")

    def on_logout_clicked(self, btn):
        KeyManager.delete_key()
        self.stack.set_visible_child_name("login")
        self.key_entry.set_text("") # Clear input

    def on_feed_selected(self, box, row):
        self.split_view.push(self.content_page)

    def on_status_changed(self, client, status):
        self.status_label.set_text(status)

    def on_event_received(self, client, pubkey, content):
        card = Adw.Bin()
        card.add_css_class("card")
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)
        vbox.set_margin_start(12)
        vbox.set_margin_end(12)

        author = Gtk.Label(label=pubkey, xalign=0)
        author.add_css_class("heading")
        author.get_style_context().add_class("dim-label")
        vbox.append(author)

        body = Gtk.Label(label=content, xalign=0, wrap=True)
        body.set_selectable(True)
        vbox.append(body)

        card.set_child(vbox)
        self.posts_box.prepend(card)

class GnostrApp(Adw.Application):
    def __init__(self, **kwargs):
        super().__init__(application_id="me.velocitynet.Gnostr",
                         flags=Gio.ApplicationFlags.FLAGS_NONE,
                         **kwargs)

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = MainWindow(application=self)
        win.present()

def main(version):
    app = GnostrApp()
    return app.run(sys.argv)

if __name__ == "__main__":
    main(None)
