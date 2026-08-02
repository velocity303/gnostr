# src/gnostr/ui/post_widget.py
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Pango, Gdk
from ..renderer import ContentRenderer, ImageLoader


class PostWidget(Adw.Bin):
    def __init__(
        self, main_window, pubkey, content, event_id, tags=[], is_hero=False, created_at=None
    ):
        super().__init__(css_classes=["card"])
        self.main_window = main_window
        self.pubkey = pubkey
        self.event_id = event_id
        self.content = content

        # Resolve the post's timestamp from the DB when the caller didn't provide
        # it (e.g. live event-received posts). Unobtrusive relative-time caption.
        if created_at is None:
            ev = main_window.db.get_event_by_id(event_id)
            created_at = ev.get("created_at") if ev else None

        if is_hero:
            self.add_css_class("hero")

        # Store reference for metrics updates
        self.main_window.event_widgets[event_id] = self

        self.main_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        self.set_child(self.main_box)

        # Header: Avatar + Name
        hb = Gtk.Box(spacing=12)

        prof = self.main_window.db.get_profile(pubkey)
        name = pubkey[:8]
        if prof:
            name = prof.get("display_name") or prof.get("name") or name

        self.avatar = Adw.Avatar(
            size=48 if is_hero else 40, show_initials=True, text=name
        )
        if prof and prof.get("picture"):
            # Fix: use load_avatars (plural) not load_avatar
            ImageLoader.load_avatars(
                prof["picture"], lambda t: self.avatar.set_custom_image(t)
            )

        btn_av = Gtk.Button(css_classes=["flat"])
        btn_av.set_child(self.avatar)
        btn_av.connect("clicked", lambda b: self.main_window.show_profile(pubkey))
        hb.append(btn_av)

        nb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        nb.set_hexpand(True)
        self.lbl_name = Gtk.Label(label=name, xalign=0, css_classes=["heading"])
        nb.append(self.lbl_name)

        lbl_npub = Gtk.Label(
            label=pubkey[:12] + "...", xalign=0, css_classes=["caption", "dim-label"]
        )
        nb.append(lbl_npub)
        hb.append(nb)

        # Unobtrusive relative timestamp at the top-right of the header
        self.lbl_time = Gtk.Label(
            label=self._format_time(created_at), xalign=1, css_classes=["caption", "dim-label"]
        )
        self.lbl_time.set_halign(Gtk.Align.END)
        hb.append(self.lbl_time)
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
            ctrl.connect(
                "released",
                lambda c, n, x, y: self.main_window.show_thread(
                    event_id, pubkey, content, tags
                ),
            )
            self.add_controller(ctrl)

            # Long-press copies the whole post; group it with the click gesture so
            # a long-press doesn't also open the thread on release.
            lp = Gtk.GestureLongPress()
            lp.connect("pressed", lambda g, x, y: self._copy_post())
            ctrl.group(lp)
            self.add_controller(lp)

    def _copy_post(self):
        display = Gdk.Display.get_default()
        clipboard = display.get_clipboard() if display else None
        if clipboard is None:
            return
        provider = Gdk.ContentProvider.new_for_value(self.content)
        clipboard.set_content(provider)
        self.main_window.add_toast(Adw.Toast(title="Copied post"))

    @staticmethod
    def _format_time(ts):
        """Compact, unobtrusive relative timestamp: now / Nm / Nh / Nd, then a date."""
        if not ts:
            return ""
        try:
            import time as _time

            diff = _time.time() - float(ts)
            if diff < 60:
                return "now"
            mins = int(diff / 60)
            if mins < 60:
                return f"{mins}m"
            hours = int(diff / 3600)
            if hours < 24:
                return f"{hours}h"
            days = int(diff / 86400)
            if days < 30:
                return f"{days}d"
            import datetime as _dt

            return _dt.datetime.fromtimestamp(int(ts), _dt.timezone.utc).strftime("%b %d")
        except Exception:
            return ""
