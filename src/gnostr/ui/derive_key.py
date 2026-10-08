"""NIP-06 seed-phrase derivation dialog — POWER-USER ONLY (issue #16, P3).

NIP-06 itself is flagged `unrecommended` ("prefer a single nsec"): the
warning below is the NIP's own copy rule and must stay visible, not buried.
Derives at m/44'/1237'/<account>'/0/0 (SLIP-0044 Nostr entry) and stores the
resulting key in the keyring like any other login.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from .. import nip06, nostr_utils
from ..key_manager import KeyManager

WARNING_TEXT = (
    "NIP-06 seed-phrase derivation is unrecommended for normal users — "
    "prefer a single nsec. Wallet-tree derivation confuses backup and "
    "recovery. Only continue if you know why you need it."
)


class DeriveKeyDialog(Adw.Window):
    def __init__(self, client, parent):
        super().__init__()
        self.client = client
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_title("Derive Key from Seed Phrase")
        self.set_default_size(460, 460)
        tb = Adw.ToolbarView()
        self.set_content(tb)
        tb.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        tb.set_content(page)

        gw = Adw.PreferencesGroup()
        page.add(gw)
        warn = Gtk.Label(label=WARNING_TEXT, wrap=True, xalign=0)
        warn.add_css_class("warning")
        gw.add(warn)

        g = Adw.PreferencesGroup(title="BIP-39 mnemonic")
        page.add(g)
        self.ent = Gtk.Entry(placeholder_text="12 or 24 word mnemonic, space separated")
        self.ent.set_visibility(True)
        g.add(self.ent)
        self.acct = Adw.SpinRow.new_with_range(0, 100, 1)
        self.acct.set_title("Account index")
        g.add(self.acct)

        bg = Adw.PreferencesGroup()
        page.add(bg)
        self.error_label = Gtk.Label(label="", css_classes=["error-label"], wrap=True)
        bg.add(self.error_label)
        b = Gtk.Button(
            label="Derive and log in", css_classes=["pill", "suggested-action"]
        )
        b.connect("clicked", self.on_derive)
        bg.add(b)

    def on_derive(self, btn):
        try:
            priv_hex, pub_hex = nip06.derive_keypair(
                self.ent.get_text(), account=int(self.acct.get_value())
            )
        except ValueError as e:
            self.error_label.set_text(str(e))
            return
        if not KeyManager.save_key(priv_hex):
            self.error_label.set_text("Keyring save failed — key not persisted.")
            return
        self.client.set_keys(pub_hex, priv_hex)
        self.client.fetch_user_relays()
        self.client.fetch_contacts()
        p = self.get_transient_for()
        if p and hasattr(p, "perform_login"):
            p.perform_login(priv_hex)
        self.close()


def derive_keypair_for_test(mnemonic, account=0):
    """Thin indirection kept for tests that want the dialog's exact call shape."""
    return nip06.derive_keypair(mnemonic, account=account)
