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
gi.require_version('Pango', '1.0')
from gi.repository import Gtk, Adw, GLib, Gio, GObject, Gdk, GdkPixbuf, Pango

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
    def render(content, window_ref):
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
                if part.startswith("nostr:"):
                    ContentRenderer._add_nostr_card(box, part, window_ref)
                elif lower.endswith(ContentRenderer.IMAGE_EXTS):
                    ContentRenderer._add_image(box, part)
                elif lower.endswith(ContentRenderer.VIDEO_EXTS):
                    ContentRenderer._add_link_button(box, part, "▶ Watch Video")
                else:
                    ContentRenderer._add_link(box, part)
            else:
                current_text_buffer.append(part)

        if current_text_buffer:
            ContentRenderer._add_text(box, "".join(current_text_buffer))
        return box

    @staticmethod
    def _add_text(box, text):
        label = Gtk.Label(label=text, xalign=0, selectable=True)
        label.set_use_markup(False)
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_max_width_chars(50)
        box.append(label)

    @staticmethod
    def _add_link(box, url, label=None):
        disp = label if label else (url[:47] + "..." if len(url)>50 else url)
        markup = f'<a href="{GLib.markup_escape_text(url)}">{GLib.markup_escape_text(disp)}</a>'
        lbl = Gtk.Label(label=markup, xalign=0, wrap=True, selectable=True, use_markup=True)
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
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
    def _add_nostr_card(box, uri, window):
        try:
            parts = uri.split(":")
            if len(parts) < 2: return

            bech32_str = parts[1]
            is_event = "nevent" in uri or "note" in uri
            is_profile = "nprofile" in uri or "npub" in uri

            # Wrap in a Button to stop click propagation to the parent post
            btn = Gtk.Button(css_classes=["flat", "card"])

            row = Adw.ActionRow()
            # Disable activation on row since button handles click
            row.set_activatable(False)

            short_id = bech32_str[:10] + "..." + bech32_str[-6:]

            if is_event:
                row.set_title("Quoted Event")
                row.set_subtitle(short_id)
                row.add_prefix(Gtk.Image.new_from_icon_name("chat-bubble-symbolic"))

                def on_click_event(b):
                    hex_id = ContentRenderer._extract_hex_id(bech32_str)
                    if hex_id:
                        print(f"DEBUG: Internal Nav -> Thread {hex_id}")
                        window.show_thread(hex_id, "Unknown Author", "Loading quoted post...")
                    else:
                        launcher = Gtk.UriLauncher(uri=f"https://njump.me/{bech32_str}")
                        launcher.launch(window, None, None)

                btn.connect("clicked", on_click_event)

            elif is_profile:
                row.set_title("User Profile")
                row.set_subtitle(short_id)
                row.add_prefix(Gtk.Image.new_from_icon_name("avatar-default-symbolic"))

                def on_click_profile(b):
                    hex_pk = ContentRenderer._extract_hex_id(bech32_str)
                    if hex_pk:
                        print(f"DEBUG: Internal Nav -> Profile {hex_pk}")
                        window.show_profile(hex_pk)
                    else:
                        launcher = Gtk.UriLauncher(uri=f"https://njump.me/{bech32_str}")
                        launcher.launch(window, None, None)

                btn.connect("clicked", on_click_profile)

            btn.set_child(row)
            box.append(btn)

        except Exception as e:
            print(f"Error rendering nostr card: {e}")

    @staticmethod
    def _extract_hex_id(bech32_str):
        try:
            hrp, data = nostr_utils.bech32_decode(bech32_str)
            if not data: return None

            def to_bytes(d):
                acc = 0; bits = 0; ret = []; maxv = 255; max_acc = (1 << 12) - 1
                for value in d:
                    if value < 0 or (value >> 5): return None
                    acc = ((acc << 5) | value) & max_acc
                    bits += 5
                    while bits >= 8: bits -= 8; ret.append((acc >> bits) & maxv)
                return bytes(ret)

            raw_bytes = to_bytes(data)
            if not raw_bytes: return None

            if hrp in ["note", "npub"]: return raw_bytes.hex()

            if hrp in ["nevent", "nprofile"]:
                i = 0
                while i < len(raw_bytes):
                    if i + 2 > len(raw_bytes): break
                    t = raw_bytes[i]; l = raw_bytes[i+1]
                    if i + 2 + l > len(raw_bytes): break
                    if t == 0 and l == 32: return raw_bytes[i+2 : i+2+l].hex()
                    i += 2 + l
        except: pass
        return None

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
                if not url.startswith("http"): GLib.idle_add(callback, None); return
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
        'event-received': (GObject.SignalFlags.RUN_FIRST, None, (str, str, str, str)),
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
        self.metrics = {}
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
        tags = ev.get('tags', [])

        target = self.get_ref_id(tags)
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
                nr = [t[1] for t in tags if t[0]=='r']
                if nr:
                    ch = False
                    for r in nr:
                        if r.startswith("ws") and r not in self.relay_urls:
                            self.relay_urls.add(r); self.add_relay_connection(r); ch=True
                    if ch: self.save_config(); GLib.idle_add(self.emit, 'relay-list-updated')
        elif kind == 1:
            GLib.idle_add(self.emit, 'event-received', eid, pubkey, ev['content'], json.dumps(tags))

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
        self.set_transient_for(parent_window); self.set_modal(True)
        self.set_title("Relay Management"); self.client = nostr_client; self.relay_rows = []
        page = Adw.PreferencesPage(); self.add(page)
        g1 = Adw.PreferencesGroup(title="Sync"); page.add(g1)
        r1 = Adw.ActionRow(title="Import from Profile"); b1 = Gtk.Button(label="Import", css_classes=["suggested-action"])
        b1.connect("clicked", self.on_import); r1.add_suffix(b1); g1.add(r1)
        g2 = Adw.PreferencesGroup(title="Active Relays"); page.add(g2); self.relay_group = g2
        r2 = Adw.ActionRow(title="Add New Relay"); self.entry = Gtk.Entry(placeholder_text="wss://...")
        b2 = Gtk.Button(icon_name="list-add-symbolic"); b2.connect("clicked", self.on_add)
        r2.add_suffix(self.entry); r2.add_suffix(b2); g2.add(r2)
        self.refresh(); self.client.connect('relay-list-updated', self.refresh)
    def on_import(self, b): self.client.fetch_user_relays(); self.add_toast(Adw.Toast(title=f"Requesting Relay List..."))
    def on_add(self, b):
        u = self.entry.get_text().strip()
        if u.startswith("ws"): self.client.add_relay(u); self.entry.set_text("")
    def refresh(self, *a):
        for r in self.relay_rows: self.relay_group.remove(r)
        self.relay_rows.clear()
        for u in sorted(list(self.client.relay_urls)):
            r = Adw.ActionRow(title=u)
            b = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["destructor"])
            b.connect("clicked", lambda x,url=u: self.client.remove_relay(url))
            r.add_suffix(b); self.relay_group.add(r); self.relay_rows.append(r)

class LoginDialog(Adw.Window):
    def __init__(self, client, parent):
        super().__init__(); self.client = client; self.set_transient_for(parent); self.set_modal(True)
        self.set_default_size(450, 400); self.set_title("Login")
        c = Adw.ToolbarView(); self.set_content(c); c.add_top_bar(Adw.HeaderBar())
        p = Adw.PreferencesPage(); c.set_content(p); g = Adw.PreferencesGroup(title="Credentials"); p.add(g)
        self.ent = Adw.PasswordEntryRow(title="Private Key"); g.add(self.ent)
        bg = Adw.PreferencesGroup(); p.add(bg); b = Gtk.Button(label="Login", css_classes=["pill","suggested-action"])
        b.connect("clicked", self.on_login); bg.add(b)
    def on_login(self, b):
        k = self.ent.get_text(); h = None
        if k:
            if nostr_utils and k.startswith("nsec"): h = nostr_utils.nsec_to_hex(k)
            else: h = k
        if h and nostr_utils.get_public_key(h):
            KeyManager.save_key(h); self.client.set_keys(nostr_utils.get_public_key(h), h)
            self.client.fetch_user_relays(); self.client.fetch_contacts()
            p = self.get_transient_for()
            if p and hasattr(p, 'perform_login'): p.perform_login(h)
            self.close()

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_title("Gnostr"); self.set_default_size(950, 700)
        self.db = Database(); self.client = NostrClient(self.db)
        self.client.connect("event-received", self.on_event_received)
        self.client.connect("status-changed", self.on_status_changed)
        self.client.connect("contacts-updated", lambda c: self.switch_feed("following") if self.active_feed_type=="following" else None)
        self.client.connect("profile-updated", self.on_profile_updated)
        self.client.connect("metrics-updated", self.on_metrics_updated)
        self.priv_key = None; self.pub_key = None; self.active_feed_type = "global"; self.event_widgets = {}

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
        for r_id, title, icon in [("global","Global","network-server"), ("following","Following","system-users"), ("me","My Posts","user-info"), ("profile","Profile","avatar-default")]:
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
        p_box.set_margin_top(40)
        p_box.set_margin_bottom(20)
        p_box.set_margin_start(12)
        p_box.set_margin_end(12)
        p_box.set_halign(Gtk.Align.CENTER)

        # Header inside
        p_header_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        p_header_box.append(Adw.HeaderBar())

        # Add scroll
        p_scroll = Gtk.ScrolledWindow(vexpand=True)
        p_clamp = Adw.Clamp(maximum_size=600)
        p_clamp.set_child(p_box)
        p_scroll.set_child(p_clamp)
        p_header_box.append(p_scroll)
        self.profile_page.set_child(p_header_box)

        self.prof_avatar = Adw.Avatar(size=96, show_initials=True)
        p_box.append(self.prof_avatar)
        self.lbl_name = Gtk.Label(css_classes=["title-1"])
        p_box.append(self.lbl_name)
        self.lbl_npub = Gtk.Label(css_classes=["caption", "dim-label"], selectable=True)
        self.lbl_npub.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        p_box.append(self.lbl_npub)
        self.lbl_about = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=40)
        self.lbl_about.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        p_box.append(self.lbl_about)

    def show_thread(self, event_id, pubkey, content):
        page = Adw.NavigationPage(title="Thread")
        page.root_id = event_id
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL); b.append(Adw.HeaderBar())
        s = Gtk.ScrolledWindow(vexpand=True); c = Adw.Clamp(maximum_size=600)
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        page.thread_container = container
        c.set_child(container); s.set_child(c); b.append(s); page.set_child(b)

        self.content_nav.push(page)

        # Render Hero
        hero = self.create_post_widget(pubkey, content, event_id, is_hero=True)
        # Store hero so we can update it later
        page.hero_widget = hero

        container.append(hero)
        container.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        container.append(Gtk.Label(label="Replies", css_classes=["heading"], xalign=0))
        self.client.fetch_thread(event_id)

    def show_profile(self, pubkey):
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
        else: self.lbl_name.set_text("Loading..."); self.prof_avatar.set_text("?")

    def create_post_widget(self, pubkey, content, event_id, is_hero=False):
        card = Adw.Bin(css_classes=["card"])
        if is_hero: card.add_css_class("hero")
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        hb = Gtk.Box(spacing=12)

        # Add references to labels so we can update them later
        prof = self.db.get_profile(pubkey)
        name = pubkey[:8]
        if prof: name = prof.get('display_name') or prof.get('name') or name
        av = Adw.Avatar(size=48 if is_hero else 40, show_initials=True, text=name)
        if prof and prof.get('picture'): ImageLoader.load_avatar(prof['picture'], lambda t: av.set_custom_image(t))
        hb.append(av)
        card.avatar = av # Store ref

        nb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        lbl_name = Gtk.Label(label=name, xalign=0, css_classes=["heading"])
        card.lbl_name = lbl_name # Store ref
        nb.append(lbl_name)
        nb.append(Gtk.Label(label=pubkey[:12]+"...", xalign=0, css_classes=["caption", "dim-label"]))
        hb.append(nb)
        main_box.append(hb)

        # Content can't easily be updated in-place if we replace the widget,
        # but for 'Loading...' we assume we are creating a placeholder.
        # Actually, ContentRenderer creates a Box.
        content_area = ContentRenderer.render(content, self)
        card.content_area = content_area # Store ref? No, we might need to replace the whole box.
        # Easier: Store the main_box so we can rebuild it if needed?
        # Or just rebuild the whole card? No.
        # Let's just update the parts we know are placeholders.
        # For hero updates: We will want to replace the content label if it was "Loading..."

        main_box.append(content_area)

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
            ctrl.connect("released", lambda c, n, x, y: self.show_thread(event_id, pubkey, content))
            card.add_controller(ctrl)
        return card

    def on_event_received(self, client, eid, pubkey, content, tags_json):
        if not self.db.get_profile(pubkey): self.client.fetch_profile(pubkey)
        target = self.posts_box
        page = self.content_nav.get_visible_page()

        # Check if we are in a thread view and this event IS the root ID we are waiting for
        if hasattr(page, 'root_id') and page.root_id == eid and hasattr(page, 'hero_widget'):
             # Update Hero Widget!
             # We can't easily "edit" a GtkBox in place without clearing it.
             # Simpler: Replace the hero widget in the container.
             if hasattr(page, 'thread_container'):
                 # We need to find the index of the hero widget (usually 0)
                 # But GTK4 removed get_children list easily.
                 # Alternatively, create a NEW widget and swap properties?
                 # Or better: Just create a new one and replace the first child.
                 new_hero = self.create_post_widget(pubkey, content, eid, is_hero=True)

                 # Remove old hero (first child)
                 first = page.thread_container.get_first_child()
                 if first == page.hero_widget:
                     page.thread_container.remove(first)
                     page.thread_container.prepend(new_hero)
                     page.hero_widget = new_hero
             return

        if hasattr(page, 'root_id') and hasattr(page, 'thread_container'):
            try:
                tags = json.loads(tags_json)
                refs = [t[1] for t in tags if t[0] == 'e']
                if page.root_id in refs:
                    w = self.create_post_widget(pubkey, content, eid)
                    page.thread_container.append(w)
                    return
            except: pass
        if page == self.feed_page:
             w = self.create_post_widget(pubkey, content, eid)
             self.posts_box.prepend(w)

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

    def on_profile_updated(self, client, pubkey):
        if pubkey == self.pub_key: self.load_my_profile_ui()

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
            w = self.create_post_widget(ev['pubkey'], ev['content'], ev['id'])
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
