
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gdk
from gnostr.ui.post_widget import PostWidget
from gnostr.renderer import ImageLoader
from gnostr.nostr_utils import hex_to_npub

class ProfileView(Adw.Bin):
    def __init__(self, main_window, pubkey):
        super().__init__(css_classes=["card"])
        self.main_window = main_window
        self.pubkey = pubkey

        self.layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                             margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.set_child(self.layout)

        # Header with back button, avatar, npub
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        header.set_halign(Gtk.Align.CENTER)

        # Back button row
        back_row = Gtk.Box(spacing=12, halign=Gtk.Align.START)
        btn_back = Gtk.Button(icon_name="go-previous-symbolic", css_classes=["flat"])
        btn_back.set_tooltip_text("Back")
        btn_back.connect("clicked", lambda b: self.main_window.content_nav.pop())
        back_row.append(btn_back)
        header.append(back_row)

        # Avatar
        prof = self.main_window.db.get_profile(pubkey)
        name = prof.get('display_name') or prof.get('name') or pubkey[:8] if prof else pubkey[:8]
        self.avatar = Adw.Avatar(size=120, show_initials=True, text=name)
        if prof and prof.get('picture'):
            ImageLoader.load_avatar(prof['picture'], lambda t: self.avatar.set_custom_image(t))
        header.append(self.avatar)

        # Username (big bold)
        lbl_name = Gtk.Label(label=name, xalign=0.5, css_classes=["heading", "large"])
        header.append(lbl_name)

        # Npub with copy button
        npub = hex_to_npub(pubkey) or pubkey
        npub_box = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        lbl_npub = Gtk.Label(label=npub, xalign=0, css_classes=["caption", "dim-label"])
        npub_box.append(lbl_npub)

        btn_copy = Gtk.Button(icon_name="edit-copy-symbolic", css_classes=["flat"])
        btn_copy.set_tooltip_text("Copy npub")
        btn_copy.connect("clicked", lambda b: self.copy_to_clipboard(npub))
        npub_box.append(btn_copy)

        header.append(npub_box)
        self.layout.append(header)

        # Posts List with scrolling
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        c = Adw.Clamp(maximum_size=600)
        self.posts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        c.set_child(self.posts_box)
        scroll.set_child(c)
        scroll.set_vexpand(True)
        self.layout.append(scroll)

        # Load posts from DB
        posts = self.main_window.db.get_feed_for_user(pubkey)
        for ev in posts:
            w = PostWidget(self.main_window, ev['pubkey'], ev['content'], ev['id'], ev.get('tags', []), is_hero=False)
            self.posts_box.append(w)

    def copy_to_clipboard(self, text):
        clipboard = Gtk.Clipboard.get_default()
        clipboard.set_text(text)
        toast = Adw.Toast(title="Copied npub")
        self.main_window.toast_overlay.add_toast(toast)
