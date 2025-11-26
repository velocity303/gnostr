#!/usr/bin/env python3
import sys
import json
import threading
import time
import gi
from key_manager import KeyManager

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

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Use an Adw.ViewStack to switch between Login and Main App
        self.stack = Adw.ViewStack()
        self.set_content(self.stack)

        # -- PAGE 1: Login Screen --
        self.login_page = Adw.StatusPage(
            title="Welcome to Gnostr",
            description="Enter your private key (nsec) to start.",
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

        # -- PAGE 2: Main App (Your existing SplitView code) --
        self.split_view = Adw.NavigationSplitView()
        # ... (Your existing UI setup code here) ...
        self.stack.add_named(self.split_view, "app")

        # -- Auto-Login Check --
        saved_key = KeyManager.load_key()
        if saved_key:
            print(f"Found saved key: {saved_key[:10]}...")
            self.stack.set_visible_child_name("app")
            # Start Nostr connection here
        else:
            self.stack.set_visible_child_name("login")

    def on_login_clicked(self, btn):
        key = self.key_entry.get_text()
        if key.startswith("nsec"):
            KeyManager.save_key(key)
            self.stack.set_visible_child_name("app")
            # Start Nostr connection
        else:
            # Show toast/error
            print("Invalid key format")
            
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

# --- UPDATED: Function for GNOME Builder Wrapper ---
def main(version):
    app = GnostrApp()
    return app.run(sys.argv)

# --- Standard Python Execution ---
if __name__ == "__main__":
    main(None)

