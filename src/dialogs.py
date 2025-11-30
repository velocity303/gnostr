import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw
from key_manager import KeyManager
import nostr_utils

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
    def on_import(self, b): 
        self.client.fetch_user_relays()
        self.add_toast(Adw.Toast(title=f"Requesting Relay List..."))
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
