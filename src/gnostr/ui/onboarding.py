"""Create-account wizard (Gitea #14 / docs/onboarding-key-management.md P1).

Flow: intro (generate) -> gate (un-skippable nsec type-back confirm) ->
wrap (optional NIP-49 password encryption of the backup) -> done (commit to
libsecret + login + bootstrap profile). Closing before 'done' discards the
key — nothing is persisted until the gate is passed and the wrap choice made.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from .. import nostr_utils
from ..key_manager import KeyManager


def gate_passed(expected_nsec, typed):
    """The gate rule: the user must reproduce the nsec exactly (modulo
    surrounding whitespace) before the wizard advances."""
    return typed.strip() == expected_nsec


def _page():
    box = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL,
        spacing=12,
        margin_top=24,
        margin_bottom=24,
        margin_start=24,
        margin_end=24,
    )
    return box


class CreateAccountWindow(Adw.Window):
    def __init__(self, client, main_window):
        super().__init__()
        self.client = client
        self.main_window = main_window
        self._priv_hex = None
        self._nsec = None
        self._npub = None
        self.set_title("Create Account")
        self.set_default_size(420, 560)
        self.set_transient_for(main_window)
        self.set_modal(True)

        toolbar = Adw.ToolbarView()
        self.set_content(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())

        self.view_stack = Adw.ViewStack()
        toolbar.set_content(self.view_stack)
        self._build_intro()
        self._build_gate()
        self._build_wrap()

    # ---- page: intro ----------------------------------------------------
    def _build_intro(self):
        page = _page()
        page.append(
            Gtk.Label(
                label=(
                    "A Nostr account is a secret key this device will create "
                    "for you.\n\nIf you lose this key, your account is gone "
                    "forever — there is no password reset."
                ),
                wrap=True,
                xalign=0,
            )
        )
        btn = Gtk.Button(
            label="Generate My Key", css_classes=["pill", "suggested-action"]
        )
        btn.set_halign(Gtk.Align.CENTER)
        btn.connect("clicked", self._on_generate)
        page.append(btn)
        self.view_stack.add_named(page, "intro")

    def _on_generate(self, btn):
        self._priv_hex, pub_hex = nostr_utils.generate_keypair()
        self._nsec = nostr_utils.hex_to_nsec(self._priv_hex)
        self._npub = nostr_utils.hex_to_npub(pub_hex)
        self._build_gate_widgets()
        self.view_stack.set_visible_child_name("gate")

    # ---- page: backup gate ----------------------------------------------
    def _build_gate(self):
        self._gate_box = _page()
        self.view_stack.add_named(self._gate_box, "gate")

    def _build_gate_widgets(self):
        self._gate_box.append(
            Gtk.Label(
                label="Write this down NOW. It is shown only once.", wrap=True, xalign=0
            )
        )
        self._nsec_label = Gtk.Label(
            label="•" * 24, wrap=True, selectable=True, css_classes=["monospace"]
        )
        self._gate_box.append(self._nsec_label)
        reveal = Gtk.Button(label="Reveal")
        reveal.connect("clicked", self._on_reveal)
        self._gate_box.append(reveal)
        self._gate_entry = Gtk.Entry(placeholder_text="Type your key back exactly")
        self._gate_entry.set_visibility(True)
        self._gate_box.append(self._gate_entry)
        self._gate_error = Gtk.Label(label="", css_classes=["error-label"])
        self._gate_box.append(self._gate_error)
        nxt = Gtk.Button(
            label="I saved it — continue",
            css_classes=["pill", "suggested-action"],
        )
        nxt.connect("clicked", self._on_gate_confirmed)
        self._gate_box.append(nxt)

    def _on_reveal(self, btn):
        self._nsec_label.set_text(self._nsec)

    def _on_gate_confirmed(self, btn):
        if not gate_passed(self._nsec, self._gate_entry.get_text()):
            self._gate_error.set_text(
                "Doesn't match. Check your written copy and try again."
            )
            return
        self.view_stack.set_visible_child_name("wrap")

    # ---- page: wrap (optional NIP-49) ------------------------------------
    def _build_wrap(self):
        page = _page()
        page.append(
            Gtk.Label(
                label=(
                    "Recommended: also encrypt your backup with a password "
                    "(ncryptsec). The encrypted blob is what you store "
                    "long-term."
                ),
                wrap=True,
                xalign=0,
            )
        )
        self._pw1 = Adw.PasswordEntryRow(title="Backup password")
        self._pw2 = Adw.PasswordEntryRow(title="Repeat password")
        page.append(self._pw1)
        page.append(self._pw2)
        self._wrap_error = Gtk.Label(label="", css_classes=["error-label"])
        page.append(self._wrap_error)
        encrypt_btn = Gtk.Button(
            label="Encrypt backup", css_classes=["pill", "suggested-action"]
        )
        encrypt_btn.connect("clicked", self._on_encrypt_backup)
        page.append(encrypt_btn)
        skip = Gtk.Button(
            label="Skip — my written copy is enough", css_classes=["flat"]
        )
        skip.connect("clicked", lambda b: self._finish())
        page.append(skip)
        self.view_stack.add_named(page, "wrap")

    def _on_encrypt_backup(self, btn):
        p1 = self._pw1.get_text()
        if len(p1) < 8:
            self._wrap_error.set_text("Use at least 8 characters.")
            return
        if p1 != self._pw2.get_text():
            self._wrap_error.set_text("Passwords don't match.")
            return
        from ..nip49 import nip49_encrypt

        blob = nip49_encrypt(self._priv_hex, p1)
        self._show_blob_page(blob)

    def _show_blob_page(self, blob):
        page = _page()
        page.append(
            Gtk.Label(
                label=(
                    "Store this encrypted backup somewhere safe. You will "
                    "need the password to restore it."
                ),
                wrap=True,
                xalign=0,
            )
        )
        lbl = Gtk.Label(
            label=blob, wrap=True, selectable=True, css_classes=["monospace"]
        )
        page.append(lbl)
        copy = Gtk.Button(label="Copy to clipboard")
        copy.connect("clicked", lambda b: self._copy(blob))
        page.append(copy)
        done = Gtk.Button(
            label="I stored it — finish", css_classes=["pill", "suggested-action"]
        )
        done.connect("clicked", lambda b: self._finish())
        page.append(done)
        self.view_stack.add_named(page, "blob")
        self.view_stack.set_visible_child_name("blob")

    def _copy(self, text):
        from gi.repository import Gdk

        display = Gdk.Display.get_default()
        if display:
            Gdk.Clipboard(display).set(text)

    # ---- finish: commit to keyring + login + bootstrap -------------------
    def _finish(self):
        if not KeyManager.save_key(self._priv_hex):
            # Surface the failure but keep the user on the wrap page so the
            # generated key is not lost: they can retry after unlocking.
            self.view_stack.set_visible_child_name("wrap")
            page_box = self.view_stack.get_visible_child()
            err = Gtk.Label(
                label=(
                    "Keyring save failed — key held in memory only. "
                    "Retry after unlocking the keyring."
                ),
                wrap=True,
                xalign=0,
                css_classes=["error-label"],
            )
            page_box.append(err)
            return
        self.main_window.perform_login(self._priv_hex)
        self.close()
        # Profile bootstrap: never land a new user on a blank identity.
        from ..dialogs import EditProfileDialog

        EditProfileDialog(self.client, self.main_window).present()
