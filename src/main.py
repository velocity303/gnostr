#!/usr/bin/env python3
import sys
import json
import threading
import time
import urllib.request
import os
import re
import html # Required for unescaping HTML entities
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

# Default relays
DEFAULT_RELAYS = [
    "wss://relay.damus.io",
    "wss://relay.nostr.band",
    "wss://nos.lol",
    "wss://relay.primal.net"
]

# --- Helper: Content Renderer ---
class ContentRenderer:
    # Regex to find URLs AND Nostr URIs
    # Captures http(s) links OR nostr: links
    LINK_REGEX = re.compile(r'((?:https?://|nostr:)[^\s]+)')

    IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.webp')
    VIDEO_EXTS = ('.mp4', '.mov', '.webm')

    @staticmethod
    def render(content):
        """
        Parses content and returns a Gtk.Box containing
        clickable text labels, rendered images, and nostr cards.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        # 1. FIX: Unescape HTML entities first (e.g. &quot; -> ")
        # This prevents double-escaping when we later call markup_escape_text
        clean_content = html.unescape(content)

        # Split by URL/URI
        parts = ContentRenderer.LINK_REGEX.split(clean_content)

        current_text_buffer = []

        for part in parts:
            if not part: continue

            if ContentRenderer.LINK_REGEX.match(part):
                # If we have buffered text, flush it first
                if current_text_buffer:
                    ContentRenderer._add_text(box, "".join(current_text_buffer))
                    current_text_buffer = []

                lower = part.lower()

                # Case A: Nostr Reference (nevent, nprofile, etc.)
                if part.startswith("nostr:"):
                    # Render inline button for reference
                    ContentRenderer._add_nostr_link(box, part)

                # Case B: Image
                elif lower.endswith(ContentRenderer.IMAGE_EXTS):
                    ContentRenderer._add_image(box, part)

                # Case C: Video (Link for now)
                elif lower.endswith(ContentRenderer.VIDEO_EXTS):
                    ContentRenderer._add_link_button(box, part, label="▶ Watch Video")

                # Case D: Standard Link
                else:
                    ContentRenderer._add_link(box, part)
            else:
                # Buffer regular text to merge small chunks
                current_text_buffer.append(part)

        # Flush remaining text
        if current_text_buffer:
            ContentRenderer._add_text(box, "".join(current_text_buffer))

        return box

    @staticmethod
    def _add_text(box, text):
        # Escape text for Pango markup
        safe_text = GLib.markup_escape_text(text)
        label = Gtk.Label(label=safe_text, xalign=0, wrap=True, selectable=True)
        label.set_use_markup(False) # We manually escaped, but using set_label handles plain text
        # actually for mixed links we want markup usually, but here we split components.
        # So plain label is safer for the text chunks.
        box.append(label)

    @staticmethod
    def _add_link(box, url, label=None):
        display_text = label if label else url
        if len(display_text) > 50:
            display_text = display_text[:47] + "..."

        safe_url = GLib.markup_escape_text(url)
        safe_text = GLib.markup_escape_text(display_text)

        # We use a LinkButton for clearer interactivity than a markup label
        btn = Gtk.LinkButton(uri=url, label=display_text)
        btn.set_halign(Gtk.Align.START)
        # Remove the internal alignment padding to make it look more like inline text
        btn.set_margin_top(0)
        btn.set_margin_bottom(0)
        box.append(btn)

    @staticmethod
    def _add_link_button(box, url, label):
        btn = Gtk.LinkButton(uri=url, label=label)
        btn.set_halign(Gtk.Align.START)
        box.append(btn)

    @staticmethod
    def _add_image(box, url):
        img_box = Gtk.Box(halign=Gtk.Align.START)
        img_box.set_margin_top(6)
        img_box.set_margin_bottom(6)

        spinner = Gtk.Spinner()
        spinner.start()
        img_box.append(spinner)
        box.append(img_box)
        ImageLoader.load_image_into_widget(url, img_box, spinner)

    @staticmethod
    def _add_nostr_link(box, uri):
        # Render a small, distinct button for Nostr references
        bech32_id = uri.split(":")[1] if ":" in uri else uri
        short_id = bech32_id[:10] + "..." + bech32_id[-4:]

        # Web viewer fallback
        web_url = f"https://njump.me/{bech32_id}"

        label = "Reference"
        icon_name = "text-x-generic-symbolic"

        if "nevent" in uri or "note" in uri:
            label = f"Event: {short_id}"
            icon_name = "document-open-symbolic"
        elif "nprofile" in uri or "npub" in uri:
            label = f"Profile: {short_id}"
            icon_name = "avatar-default-symbolic"

        btn = Gtk.LinkButton(uri=web_url, label=label)
        btn.set_halign(Gtk.Align.START)
        # Add a custom CSS class if you wanted styling, but standard link button is fine
        box.append(btn)

# --- Helper: Image Loader ---
class ImageLoader:
    @staticmethod
    def load_avatar(url, callback):
        ImageLoader._fetch(url, callback, size=(64,64), circular=True)

    @staticmethod
    def load_image_into_widget(url, container, spinner):
        def on_ready(texture):
            if spinner: container.remove(spinner)

            if texture:
                picture = Gtk.Picture.new_for_paintable(texture)
                picture.set_can_shrink(True)
                picture.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
                # Cap height so huge images don't push content off screen
                # Note: GTK4 doesn't have set_height_request on Picture directly in same way
                # Wrapping in a scrolled window or clamp is better, but this works:
                picture.set_halign(Gtk.Align.START)
                container.append(picture)
            else:
                icon = Gtk.Image.new_from_icon_name("image-missing-symbolic")
                icon.set_pixel_size(48)
                container.append(icon)

        ImageLoader._fetch(url, on_ready, size=None)

    @staticmethod
    def _fetch(url, callback, size=None, circular=False):
        def _bg_task():
            try:
                if not url or not url.startswith("http"):
                    GLib.idle_add(callback, None)
                    return

                req = urllib.request.Request(url, headers={'User-Agent': 'Gnostr/1.0'})
                with urllib.request.urlopen(req, timeout=10) as response:
                    data = response.read()

                loader = GdkPixbuf.PixbufLoader()
                loader.write(data)
                loader.close()
                pixbuf = loader.get_pixbuf()

                if not pixbuf:
                    GLib.idle_add(callback, None)
                    return

                if size:
                    w, h = size
                    pixbuf = pixbuf.scale_simple(w, h, GdkPixbuf.InterpType.BILINEAR)

                texture = Gdk.Texture.new_for_pixbuf(pixbuf)
                GLib.idle_add(callback, texture)
            except Exception:
                GLib.idle_add(callback, None)

        threading.Thread(target=_bg_task, daemon=True).start()

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
                if data[0] == "EVENT": self.on_event(data[2])
                elif data[0] == "EOSE": pass
            except: pass

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
        if self.current_sub_id != sub_id:
            try: self.ws.send(json.dumps(["CLOSE", self.current_sub_id]))
            except: pass
        self.current_sub_id = sub_id
        try: self.ws.send(json.dumps(["REQ", sub_id] + (filters if isinstance(filters, list) else [filters])))
        except: pass

    def request_once(self, sub_id, filters):
        if not self.is_connected: return
        try: self.ws.send(json.dumps(["REQ", sub_id] + (filters if isinstance(filters, list) else [filters])))
        except: pass

    def publish(self, event_json):
        if not self.is_connected: return
        try:
            msg = json.dumps(["EVENT", event_json])
            self.ws.send(msg)
        except Exception as e:
            print(f"Publish Error {self.url}: {e}")

    def close(self):
        if self.ws: self.ws.close()

class NostrClient(GObject.Object):
    __gsignals__ = {
        'event-received': (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        'profile-updated': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'contacts-updated': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'status-changed': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'relay-list-updated': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, db):
        super().__init__()
        self.active_relays = {}
        self.relay_urls = set(DEFAULT_RELAYS)
        self.seen_events = set()
        self.db = db
        self.my_pubkey = None
        self.my_privkey = None
        self.requested_profiles = set()

        self.config_dir = os.path.join(GLib.get_user_config_dir(), "gnostr")
        self.config_file = os.path.join(self.config_dir, "config.json")
        self.load_config()

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    data = json.load(f)
                    saved = data.get("relays")
                    if saved: self.relay_urls = set(saved)
            except: pass

    def save_config(self):
        if not os.path.exists(self.config_dir):
            try: os.makedirs(self.config_dir, exist_ok=True)
            except: return
        try:
            with open(self.config_file, 'w') as f: json.dump({ "relays": list(self.relay_urls) }, f)
        except: pass

    def set_keys(self, pubkey, privkey=None):
        self.my_pubkey = pubkey
        self.my_privkey = privkey

    def connect_all(self):
        if not websocket: return
        for url in list(self.relay_urls):
            self.add_relay_connection(url)

    def add_relay_connection(self, url):
        if url in self.active_relays: return
        relay = NostrRelay(url, self._handle_event, self._handle_status)
        relay.start()
        self.active_relays[url] = relay

    def add_relay(self, url, sync=True):
        if url not in self.relay_urls:
            self.relay_urls.add(url)
            self.save_config()
            self.add_relay_connection(url)
            self.emit('relay-list-updated')
            if sync and self.my_privkey: self.publish_relay_list()

    def remove_relay(self, url):
        if url in self.relay_urls:
            self.relay_urls.remove(url)
            self.save_config()
            if url in self.active_relays:
                self.active_relays[url].close()
                del self.active_relays[url]
            self.emit('relay-list-updated')
            if self.my_privkey: self.publish_relay_list()

    def fetch_user_relays(self):
        if not self.my_pubkey: return
        filter = {"kinds": [10002], "authors": [self.my_pubkey], "limit": 1}
        sub_id = f"relays_{self.my_pubkey[:8]}"
        for relay in self.active_relays.values():
            relay.request_once(sub_id, filter)

    def publish_relay_list(self):
        if not self.my_privkey or not self.my_pubkey: return
        event = {
            "pubkey": self.my_pubkey,
            "created_at": int(time.time()),
            "kind": 10002,
            "tags": [['r', url] for url in self.relay_urls],
            "content": ""
        }
        signed_event = nostr_utils.sign_event(event, self.my_privkey)
        if signed_event:
            for relay in self.active_relays.values():
                relay.publish(signed_event)

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
        elif kind == 10002:
            if self.my_pubkey and pubkey == self.my_pubkey:
                new_relays = []
                for tag in event_data.get('tags', []):
                    if tag[0] == 'r': new_relays.append(tag[1])
                if new_relays:
                    changed = False
                    for r in new_relays:
                        if r.startswith("ws") and r not in self.relay_urls:
                            self.relay_urls.add(r)
                            self.add_relay_connection(r)
                            changed = True
                    if changed:
                        self.save_config()
                        GLib.idle_add(self.emit, 'relay-list-updated')
        elif kind == 1:
            content = event_data.get('content')
            GLib.idle_add(self.emit, 'event-received', pubkey, content)

    def _handle_status(self, url, status):
        self.emit('status-changed', f"{status}")

    def subscribe(self, sub_id, filters):
        for relay in self.active_relays.values():
            relay.subscribe(sub_id, filters)

    def fetch_contacts(self):
        if self.my_pubkey:
            filter = {"kinds": [3], "authors": [self.my_pubkey], "limit": 1}
            self.subscribe("sub_contacts", filter)

    def fetch_profile(self, pubkey):
        if pubkey in self.requested_profiles: return
        self.requested_profiles.add(pubkey)
        filter = {"kinds": [0], "authors": [pubkey], "limit": 1}
        sub_id = f"meta_{pubkey[:8]}"
        for relay in self.active_relays.values():
            relay.request_once(sub_id, filter)

    def close(self):
        for relay in self.active_relays.values():
            relay.close()

class RelayPreferencesWindow(Adw.PreferencesWindow):
    def __init__(self, nostr_client, parent_window):
        super().__init__()
        self.set_transient_for(parent_window)
        self.set_modal(True)
        self.set_title("Relay Management")
        self.client = nostr_client
        self.relay_rows = []

        page = Adw.PreferencesPage()
        self.add(page)

        import_group = Adw.PreferencesGroup(title="Sync")
        import_group.set_description("Manage synchronization with your NIP-65 relay list.")
        page.add(import_group)

        import_row = Adw.ActionRow(title="Import from Profile")
        import_row.set_subtitle("Merge relays defined in your Nostr profile")
        import_btn = Gtk.Button(label="Import")
        import_btn.add_css_class("suggested-action")
        import_btn.connect("clicked", self.on_import_clicked)
        if not self.client.my_pubkey:
            import_btn.set_sensitive(False)
            import_row.set_subtitle("Login required to import relays")
        import_row.add_suffix(import_btn)
        import_group.add(import_row)

        self.relay_group = Adw.PreferencesGroup(title="Active Relays")
        self.relay_group.set_description("Changes made here are automatically saved to your profile.")
        page.add(self.relay_group)

        add_row = Adw.ActionRow(title="Add New Relay")
        self.new_relay_entry = Gtk.Entry(placeholder_text="wss://...")
        add_btn = Gtk.Button(icon_name="list-add-symbolic")
        add_btn.connect("clicked", self.on_add_clicked)
        add_row.add_suffix(self.new_relay_entry)
        add_row.add_suffix(add_btn)
        self.relay_group.add(add_row)

        self.refresh_list()
        self.client.connect('relay-list-updated', self.refresh_list)

    def on_import_clicked(self, btn):
        self.client.fetch_user_relays()
        toast = Adw.Toast(title=f"Requesting Relay List...")
        self.add_toast(toast)

    def on_add_clicked(self, btn):
        url = self.new_relay_entry.get_text().strip()
        if url.startswith("wss://") or url.startswith("ws://"):
            self.client.add_relay(url)
            self.new_relay_entry.set_text("")

    def refresh_list(self, *args):
        for row in self.relay_rows: self.relay_group.remove(row)
        self.relay_rows.clear()
        sorted_relays = sorted(list(self.client.relay_urls))
        for url in sorted_relays:
            row = Adw.ActionRow(title=url)
            status_icon = "network-offline-symbolic"
            if url in self.client.active_relays and self.client.active_relays[url].is_connected:
                status_icon = "network-wireless-signal-good-symbolic"
            icon = Gtk.Image.new_from_icon_name(status_icon)
            row.add_suffix(icon)
            del_btn = Gtk.Button(icon_name="user-trash-symbolic")
            del_btn.add_css_class("destructor")
            del_btn.connect("clicked", lambda b, u=url: self.client.remove_relay(u))
            row.add_suffix(del_btn)
            self.relay_group.add(row)
            self.relay_rows.append(row)

class LoginDialog(Adw.Window):
    def __init__(self, client, parent):
        super().__init__()
        self.client = client
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(450, 400)
        self.set_title("Login")

        content = Adw.ToolbarView()
        self.set_content(content)
        header = Adw.HeaderBar()
        content.add_top_bar(header)

        page = Adw.PreferencesPage()
        content.set_content(page)

        group = Adw.PreferencesGroup()
        group.set_title("Credentials")
        group.set_description("Enter your Nostr private key (nsec).")
        page.add(group)

        self.priv_entry = Adw.PasswordEntryRow()
        self.priv_entry.set_title("Private Key")
        group.add(self.priv_entry)

        btn_group = Adw.PreferencesGroup()
        page.add(btn_group)
        login_btn = Gtk.Button(label="Login")
        login_btn.add_css_class("pill")
        login_btn.add_css_class("suggested-action")
        login_btn.connect("clicked", self.on_login_clicked)
        btn_group.add(login_btn)

    def on_login_clicked(self, btn):
        priv_input = self.priv_entry.get_text()
        priv_hex = None
        if priv_input:
            if nostr_utils:
                if nostr_utils.is_valid_hex_key(priv_input): priv_hex = priv_input
                elif priv_input.startswith("nsec"):
                    hex_val = nostr_utils.nsec_to_hex(priv_input)
                    if hex_val: priv_hex = hex_val
            else: priv_hex = priv_input

        if priv_hex:
            pub_hex = nostr_utils.get_public_key(priv_hex)
            if pub_hex:
                KeyManager.save_key(priv_hex)
                self.client.set_keys(pub_hex, priv_hex)
                self.client.fetch_user_relays()
                self.client.fetch_contacts()
                parent = self.get_transient_for()
                if parent and hasattr(parent, 'perform_login'):
                    parent.perform_login(priv_hex)
                self.close()
            else: self.show_error("Invalid Key")
        else: self.show_error("Invalid Private Key")

    def show_error(self, msg):
        toast = Adw.Toast(title=msg)
        self.add_toast(toast)

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_title("Gnostr")
        self.set_default_size(950, 700)
        self.db = Database()
        self.client = NostrClient(self.db)
        self.client.connect("event-received", self.on_event_received)
        self.client.connect("status-changed", self.on_status_changed)
        self.client.connect("contacts-updated", lambda c: self.switch_feed("following") if self.active_feed_type == "following" else None)
        self.client.connect("profile-updated", self.on_profile_updated)
        self.priv_key = None
        self.pub_key = None
        self.active_feed_type = "global"

        self.split_view = Adw.NavigationSplitView()
        self.set_content(self.split_view)
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.new_length(Adw.BreakpointConditionLengthType.MAX_WIDTH, 800, Adw.LengthUnit.SP))
        breakpoint.add_setter(self.split_view, "collapsed", True)
        self.add_breakpoint(breakpoint)

        self.setup_sidebar()
        self.setup_content_area()

        self.main_stack = Adw.ViewStack()
        self.set_content(self.main_stack)
        self.login_page = Adw.StatusPage(title="Welcome to Gnostr", description="Secure, Native Nostr Client", icon_name="avatar-default-symbolic")
        login_btn = Gtk.Button(label="Login with Private Key", css_classes=["pill", "suggested-action"])
        login_btn.connect("clicked", self.on_login_clicked)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        box.append(login_btn)
        self.login_page.set_child(box)

        self.main_stack.add_named(self.login_page, "login")
        self.main_stack.add_named(self.split_view, "app")

        saved_key = KeyManager.load_key()
        if saved_key: self.perform_login(saved_key)
        else: self.main_stack.set_visible_child_name("login")
        GLib.idle_add(self.client.connect_all)

    def setup_sidebar(self):
        self.sidebar_page = Adw.NavigationPage(title="Menu", tag="sidebar")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        settings_btn = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Relay Settings")
        settings_btn.connect("clicked", self.on_settings_clicked)
        header.pack_end(settings_btn)
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
            self.lbl_name.set_text(name)
            self.prof_avatar.set_text(name)
            self.lbl_about.set_text(profile.get('about') or "")
            if profile.get('picture'): ImageLoader.load_avatar(profile['picture'], lambda t: self.prof_avatar.set_custom_image(t))

    def on_profile_updated(self, client, pubkey):
        if pubkey == self.pub_key: self.load_my_profile_ui()

    def on_login_clicked(self, btn):
        dialog = LoginDialog(self.client, self)
        dialog.present()

    def on_logout_clicked(self, btn):
        KeyManager.delete_key()
        self.main_stack.set_visible_child_name("login")
        self.client.set_keys(None, None)

    def on_menu_selected(self, box, row):
        if not row: return
        if row == self.row_global: self.switch_feed("global")
        elif row == self.row_following: self.switch_feed("following")
        elif row == self.row_me: self.switch_feed("me")
        elif row == self.row_profile: self.content_nav.push(self.profile_page)
        self.split_view.set_show_content(True)

    def on_settings_clicked(self, btn):
        win = RelayPreferencesWindow(self.client, self)
        win.present()

    def on_status_changed(self, client, status):
        self.status_label.set_text(status)

    def on_event_received(self, client, pubkey, content):
        if not self.db.get_profile(pubkey): self.client.fetch_profile(pubkey)
        self.render_event(pubkey, content)

    def render_event(self, pubkey, content):
        card = Adw.Bin(css_classes=["card"])

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)

        header_box = Gtk.Box(spacing=12)
        profile = self.db.get_profile(pubkey)
        name = pubkey[:8]
        if profile:
            name = profile.get('display_name') or profile.get('name') or name

        avatar = Adw.Avatar(size=40, show_initials=True, text=name)
        if profile and profile.get('picture'):
            ImageLoader.load_avatar(profile['picture'], lambda t: avatar.set_custom_image(t))
        header_box.append(avatar)

        lbl_name = Gtk.Label(label=name, css_classes=["heading"])
        header_box.append(lbl_name)
        main_box.append(header_box)

        content_widget = ContentRenderer.render(content)
        main_box.append(content_widget)

        card.set_child(main_box)
        self.posts_box.prepend(card)

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

        if feed_type == "global":
            self.client.subscribe("sub_global", {"kinds": [1], "limit": 20})
        elif feed_type == "me" and self.pub_key:
            self.client.subscribe("sub_me", {"kinds": [1], "authors": [self.pub_key], "limit": 20})
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

def main(version):
    app = GnostrApp()
    return app.run(sys.argv)

if __name__ == "__main__":
    main(None)
