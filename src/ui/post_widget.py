# src/ui/post_widget.py
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Pango
from gnostr.renderer import ContentRenderer, ImageLoader

class PostWidget(Adw.Bin):
    def __init__(self, main_window, pubkey, content, event_id, tags=[], is_hero=False):
        super().__init__(css_classes=["card"])
        self.main_window = main_window
        self.pubkey = pubkey
        self.event_id = event_id
        
        if is_hero:
            self.add_css_class("hero")

        # Store reference for metrics updates
        self.main_window.event_widgets[event_id] = self

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                               margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.set_child(self.main_box)

        # Header: Avatar + Name
        hb = Gtk.Box(spacing=12)

        prof = self.main_window.db.get_profile(pubkey)
        name = pubkey[:8]
        if prof:
            name = prof.get('display_name') or prof.get('name') or name

        self.avatar = Adw.Avatar(size=48 if is_hero else 40, show_initials=True, text=name)
        if prof and prof.get('picture'):
            ImageLoader.load_avatar(prof['picture'], lambda t: self.avatar.set_custom_image(t))

        btn_av = Gtk.Button(css_classes=["flat"])
        btn_av.set_child(self.avatar)
        btn_av.connect("clicked", lambda b: self.main_window.show_profile(pubkey))
        hb.append(btn_av)

        nb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.lbl_name = Gtk.Label(label=name, xalign=0, css_classes=["heading"])
        nb.append(self.lbl_name)

        lbl_npub = Gtk.Label(label=pubkey[:12]+"...", xalign=0, css_classes=["caption", "dim-label"])
        nb.append(lbl_npub)
        hb.append(nb)
        self.main_box.append(hb)

        # Content
        try:
            rendered_content = ContentRenderer.render(content, self.main_window, self)
            self.main_box.append(rendered_content)
        except Exception:
            self.main_box.append(Gtk.Label(label="[Content Error]"))

        # Footer: Metrics
        footer = Gtk.Box(spacing=20, margin_top=8)

        def mk_met(icon, label):
            b = Gtk.Box(spacing=6)
            b.append(Gtk.Image.new_from_icon_name(icon))
            l = Gtk.Label(label=label, css_classes=["caption", "dim-label"])
            b.append(l)
            return l, b  # return label first

        self.lbl_replies, r_box = mk_met("chat-bubble-symbolic", "0")
        self.lbl_reposts, rt_box = mk_met("media-playlist-repeat-symbolic", "0")
        self.lbl_likes, l_box = mk_met("starred-symbolic", "0")

        footer.append(r_box)
        footer.append(rt_box)
        footer.append(l_box)

        self.main_box.append(footer)

        # Interaction
        if not is_hero:
            ctrl = Gtk.GestureClick()
            ctrl.connect("released", lambda c, n, x, y: self.main_window.show_thread(event_id, pubkey, content, tags))
            self.add_controller(ctrl)

    def _setup_metric(self, icon, container):
        b = Gtk.Box(spacing=6)
        b.append(Gtk.Image.new_from_icon_name(icon))
        l = Gtk.Label(label="0", css_classes=["caption", "dim-label"])
        b.append(l)
        container.append(b)
        return l, b
