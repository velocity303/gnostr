#!/usr/bin/env python3
import sys
import json
import threading
import time
import urllib.request
import os
import re
import html
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

DEFAULT_RELAYS = [
    "wss://relay.damus.io",
    "wss://relay.nostr.band",
    "wss://nos.lol",
    "wss://relay.primal.net"
]

class ContentRenderer:
    LINK_REGEX = re.compile(r'((?:https?://|nostr:)[^\s]+)')
    IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.webp')
    VIDEO_EXTS = ('.mp4', '.mov', '.webm')

    @staticmethod
    def render(content):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        if not content: return box

        clean_content = html.unescape(content)
        parts = ContentRenderer.LINK_REGEX.split(clean_content)
        current_text_buffer = []

        for part in parts:
            if not part: continue
            if ContentRenderer.LINK_REGEX.match(part):
                if current_text_buffer:
                    ContentRenderer._add_text(box, "".join(current_text_buffer))
                    current_text_buffer = []
                lower = part.lower()
                if part.startswith("nostr:"): ContentRenderer._add_nostr_link(box, part)
                elif lower.endswith(ContentRenderer.IMAGE_EXTS): ContentRenderer._add_image(box, part)
                elif lower.endswith(ContentRenderer.VIDEO_EXTS): ContentRenderer._add_link_button(box, part, "▶ Watch Video")
                else: ContentRenderer._add_link(box, part)
            else: current_text_buffer.append(part)

        if current_text_buffer: ContentRenderer._add_text(box, "".join(current_text_buffer))
        return box

    @staticmethod
    def _add_text(box, text):
        label = Gtk.Label(label=GLib.markup_escape_text(text), xalign=0, wrap=True, selectable=True)
        label.set_use_markup(False)
        box.append(label)

    @staticmethod
    def _add_link(box, url, label=None):
        disp = label if label else (url[:47] + "..." if len(url)>50 else url)
        markup = f'<a href="{GLib.markup_escape_text(url)}">{GLib.markup_escape_text(disp)}</a>'
        lbl = Gtk.Label(label=markup, xalign=0, wrap=True, selectable=True, use_markup=True)
        box.append(lbl)

    @staticmethod
    def _add_link_button(box, url, label):
        btn = Gtk.LinkButton(uri=url, label=label, halign=Gtk.Align.START)
        box.append(btn)

    @staticmethod
    def _add_image(box, url):
        img_box = Gtk.Box(halign=Gtk.Align.START, margin_top=6, margin_bottom=6)
        spinner = Gtk.Spinner(); spinner.start(); img_box.append(spinner); box.append(img_box)
        ImageLoader.load_image_into_widget(url, img_box, spinner)

    @staticmethod
    def _add_nostr_link(box, uri):
        try:
            parts = uri.split(":")
            if len(parts) > 1:
                bech32_id = parts[1]
                short = bech32_id[:10] + "..."
                btn = Gtk.LinkButton(uri=f"https://njump.me/{bech32_id}", label=f"Ref: {short}", halign=Gtk.Align.START)
                box.append(btn)
        except: pass

class ImageLoader:
    @staticmethod
    def load_avatar(url, callback): ImageLoader._fetch(url, callback, size=(64,64))
    @staticmethod
    def load_image_into_widget(url, container, spinner):
        def on_ready(texture):
            if spinner: container.remove(spinner)
            if texture:
                p = Gtk.Picture.new_for_paintable(texture)
                p.set_can_shrink(True); p.set_content_fit(Gtk.ContentFit.SCALE_DOWN); p.set_halign(Gtk.Align.START)
                container.append(p)
            else: container.append(Gtk.Image.new_from_icon_name("image-missing-symbolic"))
        ImageLoader._fetch(url, on_ready)
    @staticmethod
    def _fetch(url, callback, size=None):
        def _bg():
            try:
                if not url or not url.startswith("http"): GLib.idle_add(callback, None); return
                req = urllib.request.Request(url, headers={'User-Agent': 'Gnostr/1.0'})
                with urllib.request.urlopen(req, timeout=10) as r: data = r.read()
                loader = GdkPixbuf.PixbufLoader(); loader.write(data); loader.close()
                pix = loader.get_pixbuf()
                if not pix: GLib.idle_add(callback, None); return
                if size: pix = pix.scale_simple(size[0], size[1], GdkPixbuf.InterpType.BILINEAR)
                GLib.idle_add(callback, Gdk.Texture.new_for_pixbuf(pix))
            except: GLib.idle_add(callback, None)
        threading.Thread(target=_bg, daemon=True).start()

class NostrRelay(GObject.Object):
    def __init__(self, url, on_event, on_status):
        super().__init__()
        self.url = url
        self.on_event = on_event
        self.on_status = on_status
        self.ws = None
        self.is_connected = False
        self.sub_id = None

    def start(self):
        def on_msg(ws, m):
            try:
                d = json.loads(m)
                if d[0] == "EVENT": self.on_event(d[2])
            except: pass
        def on_open(ws): self.is_connected=True; GLib.idle_add(self.on_status, self.url, "Connected")
        def on_err(ws, e): self.is_connected=False; GLib.idle_add(self.on_status, self.url, "Error")
        def on_close(ws, c, m): self.is_connected=False; GLib.idle_add(self.on_status, self.url, "Disconnected")
        self.ws = websocket.WebSocketApp(self.url, on_open=on_open, on_message=on_msg, on_error=on_err, on_close=on_close)
        threading.Thread(target=self.ws.run_forever, daemon=True).start()

    def subscribe(self, sub_id, filters):
        if not self.is_connected: return
        if self.sub_id != sub_id:
            try: self.ws.send(json.dumps(["CLOSE", self.sub_id]))
            except: pass
        self.sub_id = sub_id
        try: self.ws.send(json.dumps(["REQ", sub_id] + (filters if isinstance(filters, list) else [filters])))
        except: pass

    def request_once(self, sub_id, filters):
        # FIX: Corrected syntax "if not self.is_connected"
        if not self.is_connected: return
        try: self.ws.send(json.dumps(["REQ", sub_id] + (filters if isinstance(filters, list) else [filters])))
        except: pass

    def publish(self, event_json):
        if not self.is_connected: return
        try: self.ws.send(json.dumps(["EVENT", event_json]))
        except: pass

    def close(self):
        if self.ws: self.ws.close()

class NostrClient(GObject.Object):
    __gsignals__ = {
        'event-received': (GObject.SignalFlags.RUN_FIRST, None, (str, str, str)), # id, pubkey, content
        'profile-updated': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'contacts-updated': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'status-changed': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'relay-list-updated': (GObject.SignalFlags.RUN_FIRST, None, ()),
        'metrics-updated': (GObject.SignalFlags.RUN_FIRST, None, (str, int, int, int)),
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
        self.metrics = {} # eid -> {likes, reposts, replies}
        self.config_file = os.path.join(GLib.get_user_config_dir(), "gnostr", "config.json")
        self.load_config()

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    data = json.load(f)
                    if data.get("relays"): self.relay_urls = set(data["relays"])
            except: pass

    def save_config(self):
        d = os.path.dirname(self.config_file)
        if not os.path.exists(d): os.makedirs(d, exist_ok=True)
        try:
            with open(self.config_file, 'w') as f:
                json.dump({"relays": list(self.relay_urls)}, f)
        except: pass

    def set_keys(self, pub, priv): self.my_pubkey = pub; self.my_privkey = priv

    def connect_all(self):
        for url in list(self.relay_urls): self.add_relay_connection(url)

    def add_relay_connection(self, url):
        if url in self.active_relays: return
        r = NostrRelay(url, self._handle_event, self._handle_status)
        r.start()
        self.active_relays[url] = r

    def add_relay(self, url):
        if url not in self.relay_urls:
            self.relay_urls.add(url); self.save_config()
            self.add_relay_connection(url); self.emit('relay-list-updated')
            self.publish_relay_list()

    def remove_relay(self, url):
        if url in self.relay_urls:
            self.relay_urls.remove(url); self.save_config()
            if url in self.active_relays: self.active_relays[url].close(); del self.active_relays[url]
            self.emit('relay-list-updated'); self.publish_relay_list()

    def fetch_user_relays(self):
        if not self.my_pubkey: return
        filter = {"kinds": [10002], "authors": [self.my_pubkey], "limit": 1}
        for r in self.active_relays.values(): r.request_once(f"relays_{self.my_pubkey[:8]}", filter)

    def publish_relay_list(self):
        if not self.my_privkey: return
        event = {"pubkey": self.my_pubkey, "created_at": int(time.time()), "kind": 10002,
                 "tags": [['r', u] for u in self.relay_urls], "content": ""}
        signed = nostr_utils.sign_event(event, self.my_privkey)
        if signed:
            for r in self.active_relays.values(): r.publish(signed)

    def get_ref_id(self, tags):
        for t in tags:
            if t[0] == 'e': return t[1]
        return None

    def _handle_event(self, ev):
        eid = ev.get('id'); kind = ev['kind']; pubkey = ev['pubkey']

        target = self.get_ref_id(ev.get('tags', []))
        if target:
            if target not in self.metrics: self.metrics[target] = {'likes':0,'reposts':0,'replies':0}
            updated = False
            if kind == 7: self.metrics[target]['likes']+=1; updated=True
            elif kind == 6: self.metrics[target]['reposts']+=1; updated=True
            elif kind == 1: self.metrics[target]['replies']+=1; updated=True
            if updated:
                m = self.metrics[target]
                GLib.idle_add(self.emit, 'metrics-updated', target, m['likes'], m['reposts'], m['replies'])

        if eid in self.seen_events: return
        self.seen_events.add(eid)
        self.db.save_event(ev)

        if kind == 0:
            self.db.save_profile(pubkey, ev['content'], ev['created_at'])
            GLib.idle_add(self.emit, 'profile-updated', pubkey)
        elif kind == 3:
            if pubkey == self.my_pubkey:
                c = nostr_utils.extract_followed_pubkeys(ev)
                self.db.save_contacts(self.my_pubkey, c)
                GLib.idle_add(self.emit, 'contacts-updated')
        elif kind == 10002:
            if pubkey == self.my_pubkey:
                nr = [t[1] for t in ev.get('tags', []) if t[0]=='r']
                if nr:
                    ch = False
                    for r in nr:
                        if r.startswith("ws") and r not in self.relay_urls:
                            self.relay_urls.add(r); self.add_relay_connection(r); ch=True
                    if ch: self.save_config(); GLib.idle_add(self.emit, 'relay-list-updated')
        elif kind == 1:
            GLib.idle_add(self.emit, 'event-received', eid, pubkey, ev['content'])

    def _handle_status(self, url, status): self.emit('status-changed', status)

    def subscribe(self, sub_id, filters):
        for r in self.active_relays.values(): r.subscribe(sub_id, filters)

    def fetch_contacts(self):
        if self.my_pubkey: self.subscribe("sub_contacts", {"kinds": [3], "authors": [self.my_pubkey], "limit": 1})

    def fetch_profile(self, pubkey):
        if pubkey in self.requested_profiles: return
        self.requested_profiles.add(pubkey)
        for r in self.active_relays.values(): r.request_once(f"meta_{pubkey[:8]}", {"kinds": [0], "authors": [pubkey], "limit": 1})

    def fetch_thread(self, root_id):
        f1 = {"ids": [root_id]}
        f2 = {"kinds": [1], "#e": [root_id], "limit": 50}
        f3 = {"kinds": [6, 7], "#e": [root_id], "limit": 100}
        self.subscribe(f"thread_{root_id}", [f1, f2, f3])

    def close(self):
        for r in self.active_relays.values(): r.close()

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
        page.add(import_group)
        import_row = Adw.ActionRow(title="Import from Profile")
        import_btn = Gtk.Button(label="Import")
        import_btn.add_css_class("suggested-action")
        import_btn.connect("clicked", self.on_import_clicked)
        if not self.client.my_pubkey: import_btn.set_sensitive(False)
        import_row.add_suffix(import_btn)
        import_group.add(import_row)
        self.relay_group = Adw.PreferencesGroup(title="Active Relays")
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
        self.add_toast(Adw.Toast(title=f"Requesting Relay List..."))

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
        page.add(group)
        self.priv_entry = Adw.PasswordEntryRow()
        self.priv_entry.set_title("Private Key")
        # FIX: Removed call to set_placeholder_text which caused crash
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
            else: self.add_toast(Adw.Toast(title="Invalid Key"))
        else: self.add_toast(Adw.Toast(title="Invalid Private Key"))

    def show_error(self, msg):
        toast = Adw.Toast(title=msg)
        self.add_toast(toast)

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_title("Gnostr"); self.set_default_size(950, 700)
        self.db = Database()
        self.client = NostrClient(self.db)
        self.client.connect("event-received", self.on_event_received)
        self.client.connect("status-changed", self.on_status_changed)
        self.client.connect("contacts-updated", lambda c: self.switch_feed("following") if self.active_feed_type=="following" else None)
        self.client.connect("profile-updated", self.on_profile_updated)
        self.client.connect("metrics-updated", self.on_metrics_updated)

        self.priv_key = None; self.pub_key = None; self.active_feed_type = "global"
        self.event_widgets = {}

        self.split_view = Adw.NavigationSplitView()
        self.set_content(self.split_view)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.new_length(Adw.BreakpointConditionLengthType.MAX_WIDTH, 800, Adw.LengthUnit.SP))
        bp.add_setter(self.split_view, "collapsed", True)
        self.add_breakpoint(bp)

        self.setup_sidebar(); self.setup_content_area()
        self.main_stack = Adw.ViewStack(); self.set_content(self.main_stack)

        self.login_page = Adw.StatusPage(title="Welcome to Gnostr", description="Secure, Native Nostr Client", icon_name="avatar-default-symbolic")
        login_btn = Gtk.Button(label="Login with Private Key", css_classes=["pill", "suggested-action"])
        login_btn.connect("clicked", self.on_login_clicked)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        box.append(login_btn)
        self.login_page.set_child(box)

        self.main_stack.add_named(self.login_page, "login")
        self.main_stack.add_named(self.split_view, "app")

        saved = KeyManager.load_key()
        if saved: self.perform_login(saved)
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
        wrapper = Adw.NavigationPage(title="Content", tag="wrapper")
        wrapper.set_child(self.content_nav)
        self.split_view.set_content(wrapper)

        self.feed_page = Adw.NavigationPage(title="Feed", tag="feed")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(Adw.HeaderBar())
        scroll = Gtk.ScrolledWindow(vexpand=True)
        clamp = Adw.Clamp(maximum_size=600)
        self.posts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        clamp.set_child(self.posts_box)
        scroll.set_child(clamp)
        box.append(scroll)
        self.feed_page.set_child(box)
        self.content_nav.add(self.feed_page)

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
        p_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20, margin_top=40)
        p_box.set_halign(Gtk.Align.CENTER)
        p_box.append(Adw.HeaderBar())
        self.prof_avatar = Adw.Avatar(size=96, show_initials=True)
        p_box.append(self.prof_avatar)
        self.lbl_name = Gtk.Label(css_classes=["title-1"])
        p_box.append(self.lbl_name)
        self.lbl_npub = Gtk.Label(css_classes=["caption", "dim-label"], selectable=True)
        p_box.append(self.lbl_npub)
        self.lbl_about = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=40)
        p_box.append(self.lbl_about)
        self.profile_page.set_child(p_box)

    def show_thread(self, event_id, pubkey, content):
        self.content_nav.push(self.thread_page)
        c = self.thread_container.get_first_child()
        while c: self.thread_container.remove(c); c = self.thread_container.get_first_child()
        hero = self.create_post_widget(pubkey, content, event_id, is_hero=True)
        self.thread_container.append(hero)
        self.thread_container.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        self.thread_container.append(Gtk.Label(label="Replies", css_classes=["heading"], xalign=0))
        self.client.fetch_thread(event_id)

    def create_post_widget(self, pubkey, content, event_id, is_hero=False):
        card = Adw.Bin(css_classes=["card"])
        if is_hero: card.add_css_class("hero")
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        hb = Gtk.Box(spacing=12)
        prof = self.db.get_profile(pubkey)
        name = pubkey[:8]
        if prof: name = prof.get('display_name') or prof.get('name') or name
        av = Adw.Avatar(size=48 if is_hero else 40, show_initials=True, text=name)
        if prof and prof.get('picture'): ImageLoader.load_avatar(prof['picture'], lambda t: av.set_custom_image(t))
        hb.append(av)
        nb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        nb.append(Gtk.Label(label=name, xalign=0, css_classes=["heading"]))
        nb.append(Gtk.Label(label=pubkey[:12]+"...", xalign=0, css_classes=["caption", "dim-label"]))
        hb.append(nb)
        main_box.append(hb)
        main_box.append(ContentRenderer.render(content))
        footer = Gtk.Box(spacing=20, margin_top=8)
        def mk_met(icon, count):
            b = Gtk.Box(spacing=6)
            b.append(Gtk.Image.new_from_icon_name(icon))
            l = Gtk.Label(label=str(count), css_classes=["caption", "dim-label"])
            b.append(l)
            return b, l
        r_box, l_rep = mk_met("chat-bubble-symbolic", 0)
        rt_box, l_ret = mk_met("media-playlist-repeat-symbolic", 0)
        l_box, l_like = mk_met("starred-symbolic", 0)
        footer.append(r_box); footer.append(rt_box); footer.append(l_box)
        main_box.append(footer)
        card.set_child(main_box)
        card.lbl_replies = l_rep; card.lbl_reposts = l_ret; card.lbl_likes = l_like
        self.event_widgets[event_id] = card
        if not is_hero:
            ctrl = Gtk.GestureClick()
            ctrl.connect("released", lambda c, n, x, y: self.show_thread(event_id, pubkey, content))
            card.add_controller(ctrl)
        return card

    def on_event_received(self, client, eid, pubkey, content):
        if not self.db.get_profile(pubkey): self.client.fetch_profile(pubkey)
        target = self.posts_box
        if self.content_nav.get_visible_page() == self.thread_page: target = self.thread_container
        w = self.create_post_widget(pubkey, content, eid)
        if target == self.posts_box: target.prepend(w)
        else: target.append(w)

    def on_metrics_updated(self, client, eid, likes, reposts, replies):
        if eid in self.event_widgets:
            w = self.event_widgets[eid]
            if hasattr(w, 'lbl_likes'): w.lbl_likes.set_label(str(likes))
            if hasattr(w, 'lbl_reposts'): w.lbl_reposts.set_label(str(reposts))
            if hasattr(w, 'lbl_replies'): w.lbl_replies.set_label(str(replies))

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
            w = self.create_post_widget(ev['pubkey'], ev['content'], ev['id'])
            self.posts_box.prepend(w)

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
