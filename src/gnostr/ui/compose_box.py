# src/gnostr/ui/compose_box.py
"""RichComposeBox — the one composer widget for posts and DMs.

Scope: ~/Briefcase/gnostr-scopes/editor-gtk-scope.md. Gtk.TextView has no
rich text; the bar inserts markdown-lite MARKERS around the selection and
the wire content stays plain text (NIP-01). The marker set comes from
markup.SUPPORTED_FEATURES — the single source of truth shared with the
renderer — so the bar cannot offer anything the protocol spec forbids.

Context awareness = cursor + selection state, not the app page: emphasis
buttons are insensitive without a non-empty selection (locked decision:
selection required; no empty `****` insertion). The link button inserts a
bare URL at the caret (markdown [t](u) links are a spec non-goal).

Media attach (phase 2): Gtk.FileChooserNative -> uploader.upload_file on a
worker thread (network never blocks the GTK main loop); the bare URL lands
in the text and the NIP-92 imeta tag accumulates in self.media_tags for the
host to attach at publish time.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..markup import SUPPORTED_FEATURES, apply_marker, insert_at  # noqa: E402
from .icons import resolve_icon  # noqa: E402

# Candidate lists probed against the ACTIVE theme (house pattern, see
# ui/icons.py). format-text-code-symbolic is absent from the GNOME runtime
# (verified in the flatpak), utilities-terminal-symbolic is present.
_ICON_CANDIDATES = {
    "bold": ("format-text-bold-symbolic", "format-text-bold", "list-add-symbolic"),
    "italic": (
        "format-text-italic-symbolic",
        "format-text-italic",
        "list-add-symbolic",
    ),
    "code": (
        "format-text-code-symbolic",
        "utilities-terminal-symbolic",
        "list-add-symbolic",
    ),
    "link": ("insert-link-symbolic", "insert-link", "list-add-symbolic"),
    "media": ("insert-image-symbolic", "image-x-generic-symbolic", "list-add-symbolic"),
}


class RichComposeBox(Gtk.Box):
    """Multiline text view + context-aware formatting bar.

    Public API used by hosts (ComposeWindow, DMThreadView):
      get_text() / set_text(text)
      media_tags — NIP-92 imeta tags for uploads made in this box; the host
                   passes them as extra_tags on publish, then clear_media()
      sign_fn    — optional event->signed callable (client._sign) enabling
                   Blossom auth; the nostr.build backend uploads anonymously
      text_view  — the Gtk.TextView (for placeholder, focus, etc.)
    """

    def __init__(self, placeholder="", compact=False, sign_fn=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.media_tags = []
        self.sign_fn = sign_fn
        self._uploading = False

        self.text_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD, top_margin=12)
        if placeholder:
            self.text_view.set_placeholder_text(placeholder)
        scrolled = Gtk.ScrolledWindow(vexpand=not compact)
        if compact:
            # DM bottom bar: grow to ~3 lines, then scroll (OSK-safe).
            scrolled.set_max_content_height(120)
            scrolled.set_propagate_natural_height(True)
        scrolled.set_child(self.text_view)
        self.append(scrolled)

        bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
            css_classes=["linked"],
        )
        self._feature_buttons = {}
        for feature in ("bold", "italic", "code"):
            btn = Gtk.Button(icon_name=resolve_icon(_ICON_CANDIDATES[feature]))
            btn.set_tooltip_text(f"{feature.capitalize()} (wraps selection)")
            btn.set_sensitive(False)
            btn.connect("clicked", self._on_feature_clicked, feature)
            bar.append(btn)
            self._feature_buttons[feature] = btn

        # Link: MenuButton + popover entry — bare URL inserted at caret.
        self._link_btn = Gtk.MenuButton(
            icon_name=resolve_icon(_ICON_CANDIDATES["link"])
        )
        self._link_btn.set_tooltip_text("Insert link URL at cursor")
        pop = Gtk.Popover()
        pl = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=6)
        self._link_entry = Gtk.Entry(placeholder_text="https://…")
        self._link_entry.set_hexpand(True)
        self._link_entry.connect("activate", self._on_link_insert)
        ins = Gtk.Button(label="Insert", css_classes=["suggested-action"])
        ins.connect("clicked", self._on_link_insert)
        pl.append(self._link_entry)
        pl.append(ins)
        pl.set_orientation(Gtk.Orientation.HORIZONTAL)
        pop.set_child(pl)
        self._link_btn.set_popover(pop)
        bar.append(self._link_btn)

        # Media attach: image/video picker -> upload -> URL + imeta tag.
        self._media_btn = Gtk.Button(icon_name=resolve_icon(_ICON_CANDIDATES["media"]))
        self._media_btn.set_tooltip_text("Attach image or video")
        self._media_btn.connect("clicked", self._on_attach_clicked)
        bar.append(self._media_btn)

        self.append(bar)

        # Context awareness: emphasis buttons track selection state.
        buf = self.text_view.get_buffer()
        buf.connect("notify::has-selection", self._sync_button_sensitivity)
        buf.connect("changed", self._sync_button_sensitivity)
        self._sync_button_sensitivity()

    # -- public ----------------------------------------------------------

    def get_text(self):
        start, end = self.text_view.get_buffer().get_bounds()
        return self.text_view.get_buffer().get_text(start, end, False)

    def set_text(self, text):
        self.text_view.get_buffer().set_text(text)

    def grab_focus(self):
        self.text_view.grab_focus()

    def clear_media(self):
        """Called by the host after a successful publish."""
        self.media_tags = []

    # -- internals -------------------------------------------------------

    def _sync_button_sensitivity(self, *_args):
        has_sel = self.text_view.get_buffer().get_has_selection()
        for btn in self._feature_buttons.values():
            btn.set_sensitive(has_sel)

    def _apply(self, new_text, sel_start, sel_end):
        buf = self.text_view.get_buffer()
        buf.set_text(new_text)
        if sel_start != sel_end:
            buf.select_range(
                buf.get_iter_at_offset(sel_start), buf.get_iter_at_offset(sel_end)
            )
        else:
            buf.place_cursor(buf.get_iter_at_offset(sel_start))

    def _on_feature_clicked(self, _btn, feature):
        buf = self.text_view.get_buffer()
        start, end = buf.get_selection_bounds()
        text = buf.get_text(buf.get_bounds()[0], buf.get_bounds()[1], False)
        new_text, s, e = apply_marker(
            text, start.get_offset(), end.get_offset(), feature
        )
        self._apply(new_text, s, e)

    def _on_link_insert(self, *_args):
        url = self._link_entry.get_text().strip()
        if not url:
            return
        buf = self.text_view.get_buffer()
        text = buf.get_text(*buf.get_bounds(), False)
        pos = buf.get_iter_at_mark(buf.get_insert()).get_offset()
        new_text, caret = insert_at(text, pos, url)
        buf.set_text(new_text)
        buf.place_cursor(buf.get_iter_at_offset(caret))
        self._link_entry.set_text("")
        self._link_btn.get_popover().popdown()

    # -- media attach (phase 2) -------------------------------------------

    def _on_attach_clicked(self, _btn):
        if self._uploading:
            return
        parent = self.get_root()
        chooser = Gtk.FileChooserNative.new(
            "Attach media",
            parent if isinstance(parent, Gtk.Window) else None,
            Gtk.FileChooserAction.OPEN,
            "_Open",
            "_Cancel",
        )
        img = Gtk.FileFilter()
        img.set_name("Images")
        img.add_mime_type("image/png")
        img.add_mime_type("image/jpeg")
        img.add_mime_type("image/gif")
        img.add_mime_type("image/webp")
        vid = Gtk.FileFilter()
        vid.set_name("Videos")
        vid.add_mime_type("video/mp4")
        vid.add_mime_type("video/webm")
        both = Gtk.FileFilter()
        both.set_name("All media")
        for m in ("image/*", "video/mp4", "video/webm"):
            both.add_mime_type(m)
        chooser.add_filter(both)
        chooser.add_filter(img)
        chooser.add_filter(vid)
        chooser.connect("response", self._on_chooser_response)
        self._chooser = chooser  # keep alive until response
        chooser.show()

    def _on_chooser_response(self, chooser, response):
        path = None
        if response == Gtk.ResponseType.ACCEPT:
            file = chooser.get_file()
            path = file.get_path() if file else None
        chooser.destroy()
        self._chooser = None
        if not path:
            return
        self._start_upload(path)

    def _start_upload(self, path):
        from .. import uploader

        self._uploading = True
        self._media_btn.set_sensitive(False)
        self._media_btn.set_tooltip_text("Uploading…")
        sign_fn = self.sign_fn

        def worker():
            try:
                result = uploader.upload_file(path, sign_fn=sign_fn)
                GLib.idle_add(self._on_upload_done, result, None)
            except uploader.UploadError as e:
                GLib.idle_add(self._on_upload_done, None, str(e))
            except Exception as e:  # never kill the worker silently
                GLib.idle_add(self._on_upload_done, None, f"Upload failed: {e}")

        import threading

        threading.Thread(target=worker, daemon=True).start()

    def _on_upload_done(self, result, error):
        self._uploading = False
        self._media_btn.set_sensitive(True)
        self._media_btn.set_tooltip_text("Attach image or video")
        if error:
            self._notify("media-error", error)
            return False
        from ..uploader import imeta_tag

        tag = imeta_tag(result)
        if tag:
            self.media_tags.append(tag)
        buf = self.text_view.get_buffer()
        text = buf.get_text(*buf.get_bounds(), False)
        pos = buf.get_iter_at_mark(buf.get_insert()).get_offset()
        url = result["url"]
        # URL goes on its own line: newline before it unless the caret is at
        # the start of a line already.
        before = text[:pos]
        sep = "\n" if before and not before.endswith("\n") else ""
        new_text, caret = insert_at(text, pos, sep + url)
        buf.set_text(new_text)
        buf.place_cursor(buf.get_iter_at_offset(caret))
        self._notify("media-uploaded", result["url"])
        return False

    def _notify(self, detail, message):
        """Best-effort toast via the nearest Adw.ToastOverlay host."""
        win = self.get_root()
        overlay = getattr(win, "toast_overlay", None) if win else None
        if overlay is not None:
            try:
                overlay.add_toast(Adw.Toast(title=message, timeout=4))
            except Exception:
                pass
