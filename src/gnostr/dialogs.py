import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Pango
from gnostr.key_manager import KeyManager
import gnostr.nostr_utils as nostr_utils


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
        g1 = Adw.PreferencesGroup(title="Sync")
        page.add(g1)
        r1 = Adw.ActionRow(title="Import from Profile")
        b1 = Gtk.Button(label="Import", css_classes=["suggested-action"])
        b1.connect("clicked", self.on_import)
        r1.add_suffix(b1)
        g1.add(r1)
        g2 = Adw.PreferencesGroup(title="Active Relays")
        page.add(g2)
        self.relay_group = g2
        r2 = Adw.ActionRow(title="Add New Relay")
        self.entry = Gtk.Entry(placeholder_text="wss://...")
        b2 = Gtk.Button(icon_name="list-add-symbolic")
        b2.connect("clicked", self.on_add)
        r2.add_suffix(self.entry)
        r2.add_suffix(b2)
        g2.add(r2)
        self.refresh()
        self.client.connect("relay-list-updated", self.refresh)

    def on_import(self, b):
        self.client.fetch_user_relays()
        self.add_toast(Adw.Toast(title="Requesting Relay List..."))

    def on_add(self, b):
        u = self.entry.get_text().strip()
        if u.startswith("ws"):
            self.client.add_relay(u)
            self.entry.set_text("")

    def refresh(self, *a):
        for r in self.relay_rows:
            self.relay_group.remove(r)
        self.relay_rows.clear()
        for u in sorted(list(self.client.relay_urls)):
            r = Adw.ActionRow(title=u)
            b = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["destructor"])
            b.connect("clicked", lambda x, url=u: self.client.remove_relay(url))
            r.add_suffix(b)
            self.relay_group.add(r)
            self.relay_rows.append(r)


def resolve_login_input(text, password):
    """Map any accepted login secret (hex | nsec | ncryptsec) to a hex key.

    Raises ValueError for ncryptsec with wrong password / corruption so the
    dialog can show it. This is the single decision point the GTK layer
    calls; unit-tested without widgets (tests/test_onboarding_keys.py).
    """
    text = text.strip()
    if text.startswith("ncryptsec"):
        from gnostr.nip49 import nip49_decrypt

        return nip49_decrypt(text, password)
    if text.startswith("nsec"):
        return nostr_utils.nsec_to_hex(text)
    return text


class LoginDialog(Adw.Window):
    def __init__(self, client, parent):
        super().__init__()
        self.client = client
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(450, 400)
        self.set_title("Login")
        c = Adw.ToolbarView()
        self.set_content(c)
        c.add_top_bar(Adw.HeaderBar())
        p = Adw.PreferencesPage()
        c.set_content(p)
        g = Adw.PreferencesGroup(title="Credentials")
        p.add(g)
        self.ent = Adw.PasswordEntryRow(title="Private Key")
        g.add(self.ent)
        self.pw_ent = Adw.PasswordEntryRow(title="Password (ncryptsec only)")
        g.add(self.pw_ent)
        bg = Adw.PreferencesGroup()
        p.add(bg)
        self.error_label = Gtk.Label(label="", css_classes=["error-label"])
        bg.add(self.error_label)
        b = Gtk.Button(label="Login", css_classes=["pill", "suggested-action"])
        b.connect("clicked", self.on_login)
        bg.add(b)

    def on_login(self, b):
        try:
            h = resolve_login_input(self.ent.get_text(), self.pw_ent.get_text())
        except ValueError:
            self.error_label.set_text(
                "Could not decrypt — wrong password or corrupt key."
            )
            return
        if h and nostr_utils.get_public_key(h):
            # A failed keyring save must NOT silently log in — the session
            # would work once and evaporate on restart (Librem 5 field bug:
            # "user interaction failed" from gnome-keyring was ignored).
            if not KeyManager.save_key(h):
                self.error_label.set_text(
                    "Keyring save failed — key not persisted."
                )
                return
            self.client.set_keys(nostr_utils.get_public_key(h), h)
            self.client.fetch_user_relays()
            self.client.fetch_contacts()
            p = self.get_transient_for()
            if p and hasattr(p, "perform_login"):
                p.perform_login(h)
            self.close()


class ExportKeyDialog(Adw.Window):
    """Produce an NIP-49 ncryptsec backup of the keyring-stored key (P1 #14).

    Closes the loop for users who logged in before the wizard existed: the
    at-rest libsecret key is wrapped with a user password and shown for
    copying. The plaintext key is never written to disk here."""

    def __init__(self, parent):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_title("Back Up Key (Encrypted)")
        self.set_default_size(450, 360)
        tb = Adw.ToolbarView()
        self.set_content(tb)
        tb.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        # Scrollable: on a portrait phone the wrapped blob + buttons exceed
        # the screen height; without this the Copy button is unreachable.
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(page)
        tb.set_content(scroll)
        g = Adw.PreferencesGroup(title="Choose a backup password")
        page.add(g)
        self.pw1 = Adw.PasswordEntryRow(title="Password")
        self.pw2 = Adw.PasswordEntryRow(title="Repeat password")
        g.add(self.pw1)
        g.add(self.pw2)
        self.err = Gtk.Label(label="", css_classes=["error-label"])
        g.add(self.err)
        g2 = Adw.PreferencesGroup()
        page.add(g2)
        b = Gtk.Button(label="Encrypt", css_classes=["pill", "suggested-action"])
        b.connect("clicked", self.on_encrypt)
        g2.add(b)
        self.copy_btn = Gtk.Button(label="Copy to clipboard", visible=False)
        self.copy_btn.connect("clicked", self.on_copy)
        g2.add(self.copy_btn)
        # WORD_CHAR: the ncryptsec blob is one long token; WORD wrap would
        # never break it and the row overflows a portrait phone screen.
        self.result_label = Gtk.Label(
            label="",
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            selectable=True,
            css_classes=["monospace"],
            xalign=0,
        )
        g2.add(self.result_label)

    def on_copy(self, btn):
        from gi.repository import Gdk

        display = Gdk.Display.get_default()
        if display:
            # Must use the display's default clipboard; constructing
            # Gdk.Clipboard(display) makes a fresh one nothing pastes from.
            display.get_clipboard().set_text(self.result_label.get_text())
        btn.set_label("Copied!")

    def on_encrypt(self, btn):
        p1 = self.pw1.get_text()
        if len(p1) < 8 or p1 != self.pw2.get_text():
            self.err.set_text("Use at least 8 characters, entered identically.")
            return
        nsec_hex = KeyManager.load_key()
        if not nsec_hex:
            self.err.set_text("No key in the keyring.")
            return
        from gnostr.nip49 import nip49_encrypt

        self.result_label.set_text(nip49_encrypt(nsec_hex, p1))
        self.copy_btn.set_visible(True)


class ComposeWindow(Adw.Window):
    def __init__(self, parent, on_post_callback):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(500, 300)
        self.set_title("New Post")

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        self.set_content(box)

        # Header bar for the window
        hb = Adw.HeaderBar()
        box.append(hb)

        # Text input
        self.text_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD, top_margin=12)
        scrolled = Gtk.ScrolledWindow(vexpand=True)
        scrolled.set_child(self.text_view)
        box.append(scrolled)

        # Footer button
        btn_post = Gtk.Button(label="Post", css_classes=["pill", "suggested-action"])
        btn_post.set_halign(Gtk.Align.END)
        btn_post.connect("clicked", self.on_post_clicked)
        box.append(btn_post)

        self.on_post_callback = on_post_callback

    def on_post_clicked(self, b):
        buffer = self.text_view.get_buffer()
        start, end = buffer.get_bounds()
        text = buffer.get_text(start, end, False)
        if text.strip():
            self.on_post_callback(text)
            self.close()


class EditProfileDialog(Adw.Window):
    """Edit + publish the user's own kind-0 profile metadata (NIP-01/NIP-24).

    Fields: name, display_name, about, picture, banner, website, nip05, lud16.
    On save, builds a metadata dict and calls client.publish_profile(), which
    signs + publishes the kind-0 event and persists locally."""

    def __init__(self, client, parent, profile=None):
        super().__init__()
        self.client = client
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(520, 620)
        self.set_title("Edit Profile")
        prof = profile or {}

        c = Adw.ToolbarView()
        self.set_content(c)
        c.add_top_bar(Adw.HeaderBar())
        p = Adw.PreferencesPage()
        c.set_content(p)

        g = Adw.PreferencesGroup(title="Identity")
        p.add(g)
        self.ent_name = self._entry_row(g, "Name", prof.get("name", ""))
        self.ent_display = self._entry_row(
            g, "Display Name", prof.get("display_name", "")
        )
        self.ent_about = self._entry_row(g, "About / Bio", prof.get("about", ""))

        g2 = Adw.PreferencesGroup(title="Media")
        p.add(g2)
        self.ent_picture = self._entry_row(
            g2, "Profile Picture URL", prof.get("picture", "")
        )
        self.ent_banner = self._entry_row(g2, "Banner URL", prof.get("banner", ""))

        g3 = Adw.PreferencesGroup(title="Links & Verification")
        p.add(g3)
        self.ent_website = self._entry_row(g3, "Website", prof.get("website", ""))
        self.ent_nip05 = self._entry_row(
            g3, "NIP-05 Identifier (user@domain)", prof.get("nip05", "")
        )
        self.ent_lud16 = self._entry_row(
            g3, "Lightning Address (user@domain)", prof.get("lud16", "")
        )

        bg = Adw.PreferencesGroup()
        p.add(bg)
        b = Gtk.Button(label="Save Profile", css_classes=["pill", "suggested-action"])
        b.connect("clicked", self.on_save)
        bg.add(b)

    def _entry_row(self, group, title, value):
        row = Adw.ActionRow(title=title)
        entry = Gtk.Entry(text=value)
        entry.set_hexpand(True)
        row.add_suffix(entry)
        group.add(row)
        return entry

    def on_save(self, b):
        metadata = {
            "name": self.ent_name.get_text().strip(),
            "display_name": self.ent_display.get_text().strip(),
            "about": self.ent_about.get_text().strip(),
            "picture": self.ent_picture.get_text().strip(),
            "banner": self.ent_banner.get_text().strip(),
            "website": self.ent_website.get_text().strip(),
            "nip05": self.ent_nip05.get_text().strip(),
            "lud16": self.ent_lud16.get_text().strip(),
        }
        # Drop empty fields so we don't publish blank metadata
        metadata = {k: v for k, v in metadata.items() if v}
        if self.client.publish_profile(metadata):
            self.close()
        else:
            self.add_toast(Adw.Toast(title="No private key loaded — cannot publish"))


class SearchDialog(Adw.Window):
    """Search for a user by pasting an npub/nprofile/nsec/hex/nostr: identifier."""

    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.set_transient_for(main_window)
        self.set_modal(True)
        self.set_default_size(480, 200)
        self.set_title("Search User")

        c = Adw.ToolbarView()
        self.set_content(c)
        c.add_top_bar(Adw.HeaderBar())

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        c.set_content(box)

        lbl = Gtk.Label(
            label="Paste an npub, nprofile, nsec, raw hex pubkey, or nostr: link",
            xalign=0,
            css_classes=["caption", "dim-label"],
        )
        box.append(lbl)

        self.entry = Gtk.Entry(placeholder_text="npub1... / nprofile1... / hex")
        self.entry.set_hexpand(True)
        self.entry.connect("activate", self.on_search)
        box.append(self.entry)

        btn = Gtk.Button(label="Search", css_classes=["pill", "suggested-action"])
        btn.set_halign(Gtk.Align.END)
        btn.connect("clicked", self.on_search)
        box.append(btn)

    def on_search(self, *a):
        from gnostr.nostr_utils import resolve_profile_identifier

        text = self.entry.get_text().strip()
        pubkey = resolve_profile_identifier(text)
        if not pubkey:
            self.add_toast(Adw.Toast(title="Not a valid Nostr identifier"))
            return
        self.main_window.show_profile(pubkey)
        self.close()
