# src/gnostr/ui/compose_box.py
"""RichComposeBox — the one composer widget for posts and DMs.

Scope: editor-gtk-scope (rich editor spec). Gtk.TextView has no
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

Phase 3: images/videos also enter via paste (OSK clipboard button) and
drag-drop. Paste has two shapes: a Gdk.FileList/Gio.File on the clipboard
(Ctrl+V intercepted before the text view sees it) and a pasted *path* as
text — the insert-text hook fires only when the whole inserted chunk is one
existing media path, so typing a path one keystroke at a time never
triggers an attach. Drag-drop takes Gio.File directly. Every attached URL
gets a removable preview chip above the entry; removing a chip deletes its
URL line and its imeta tag.

Phase 4: chips carry a 40px thumbnail (GdkPixbuf decode for images, icon for
video); the clipboard is probed for raw image BYTES after the FileList read
fails (screenshot tools put image/png there, not a file), saved to a temp
file that is deleted once the upload has read it; and every entry point
(picker, paste, drop) takes MULTIPLE files — uploads run concurrently and
the media button stays insensitive until the last one lands.
"""

import os
import tempfile
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

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
    "video": ("video-x-generic-symbolic", "media-video-symbolic", "list-add-symbolic"),
}


_MEDIA_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".webm")
_VIDEO_EXTS = (".mp4", ".webm")

# Cap per paste/drop action — a whole folder dragged in should not open a
# dozen concurrent uploads on a phone connection.
MAX_ATTACHMENTS = 4

_CHIP_THUMB_PX = 40


def _as_media_path(text):
    """Absolute path to an existing image/video file if `text` is exactly
    one, else None. Whitespace is trimmed; anything with a newline or a
    scheme (http://…) is not a local path."""
    if not text:
        return None
    t = text.strip()
    if not t.startswith("/") or "\n" in t or "\t" in t:
        return None
    if not t.lower().endswith(_MEDIA_EXTS):
        return None
    if not os.path.isfile(t):
        return None
    return t


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
        self._chips = []
        self.sign_fn = sign_fn
        self._pending = 0  # in-flight uploads (button stays down until all land)
        self._suppress_insert_hook = False

        self.text_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD, top_margin=12)
        scrolled = Gtk.ScrolledWindow(vexpand=not compact)
        if compact:
            # DM bottom bar: grow to ~3 lines, then scroll (OSK-safe).
            scrolled.set_max_content_height(120)
            scrolled.set_propagate_natural_height(True)
        scrolled.set_child(self.text_view)

        # Placeholder: GtkTextView:placeholder-text is GTK 4.24+; the Librem 5
        # runtime is 4.22 and set_placeholder_text raises AttributeError there
        # (found by the real-GTK harness — a manual overlay label instead).
        text_area = scrolled
        self._placeholder_label = None
        if placeholder:
            overlay = Gtk.Overlay()
            overlay.set_child(scrolled)
            lbl = Gtk.Label(
                label=placeholder,
                xalign=0,
                wrap=True,
                sensitive=False,
                valign=Gtk.Align.START,
                halign=Gtk.Align.START,
                margin_top=12,
                margin_start=5,
            )
            lbl.add_css_class("dim-label")
            # 4.18+: make the label fully transparent to picking so taps
            # always reach the text view underneath.
            if hasattr(lbl, "set_can_target"):
                lbl.set_can_target(False)
            overlay.add_overlay(lbl)
            self._placeholder_label = lbl
            text_area = overlay

        # Preview chip row (phase 3): one removable chip per attached URL,
        # above the text. Hidden until the first attach.
        self.media_bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            margin_start=6,
            margin_end=6,
        )
        self.media_bar.set_visible(False)
        chip_scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        chip_scroll.set_child(self.media_bar)
        chip_scroll.set_visible(False)
        self._chip_scroll = chip_scroll
        self.append(chip_scroll)
        self.append(text_area)

        # Paste a Gio.File / FileList (file manager copy, screenshot tools):
        # intercept Ctrl+V before the text view inserts it as text.
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key_pressed)
        self.text_view.add_controller(key)

        # Paste a media PATH as text (OSK clipboard paste of a path string):
        # fires only when the whole inserted chunk is one existing media
        # path, so ordinary typing never trips it.
        self.text_view.get_buffer().connect("insert-text", self._on_insert_text)

        # Drag-drop a file onto the text view.
        drop = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.text_view.add_controller(drop)

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
        pl = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
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
        self._set_text(text)

    def grab_focus(self):
        self.text_view.grab_focus()

    def clear_media(self):
        """Called by the host after a successful publish."""
        self.media_tags = []
        for chip, _url in list(self._chips):
            chip.unparent()
        self._chips = []
        self.media_bar.set_visible(False)
        self._chip_scroll.set_visible(False)

    # -- phase 3/4: paste / drag-drop entry points --------------------------

    def _on_key_pressed(self, _ctrl, keyval, _keycode, state):
        """Ctrl+V with a FILE on the clipboard attaches it instead of pasting
        text. The read is async; if no FileList is present we've already
        returned False and the normal text paste proceeds untouched."""
        if keyval not in (Gdk.KEY_v, Gdk.KEY_V) or not (
            state & Gdk.ModifierType.CONTROL_MASK
        ):
            return False
        clip = self.text_view.get_clipboard()
        clip.read_value_async(
            Gdk.FileList, GLib.PRIORITY_DEFAULT, None, self._on_paste_files, None
        )
        return False

    def _on_paste_files(self, clip, res, _data):
        try:
            gvalue = clip.read_value_finish(res)
        except GLib.Error:
            # No file list — phase 4: screenshot tools put raw image BYTES on
            # the clipboard, not a file. Probe for a Pixbuf before giving up.
            clip.read_value_async(
                GdkPixbuf.Pixbuf, GLib.PRIORITY_DEFAULT, None, self._on_paste_image, None
            )
            return
        file_list = gvalue.get_object()
        if file_list is None:
            clip.read_value_async(
                GdkPixbuf.Pixbuf, GLib.PRIORITY_DEFAULT, None, self._on_paste_image, None
            )
            return
        paths = []
        for f in file_list.get_files():
            path = f.get_path()
            if path and _as_media_path(path):
                paths.append(path)
            if len(paths) >= MAX_ATTACHMENTS:
                break
        if paths:
            self._start_uploads(paths)

    def _on_paste_image(self, clip, res, _data):
        """Raw image bytes on the clipboard (GNOME Screenshot, GIMP copy):
        write the pixbuf to a temp PNG, upload it, delete the temp file once
        the worker has read it."""
        try:
            gvalue = clip.read_value_finish(res)
        except GLib.Error:
            return  # clipboard holds no image either — normal text paste happened
        pixbuf = gvalue.get_object()
        if pixbuf is None:
            return
        fd, tmp = tempfile.mkstemp(prefix="gnostr-paste-", suffix=".png")
        os.close(fd)
        try:
            pixbuf.savev(tmp, "png", [], [])
        except GLib.Error:
            os.unlink(tmp)
            return
        try:
            thumb = pixbuf.scale_simple(
                _CHIP_THUMB_PX, _CHIP_THUMB_PX, GdkPixbuf.InterpType.BILINEAR
            )
        except Exception:
            thumb = None
        self._start_upload(tmp, cleanup=True, thumb=thumb)

    def _on_insert_text(self, _buf, _location, text, _length):
        """A pasted *path string* (OSK clipboard paste): fires only when the
        whole inserted chunk is one existing media path, so typing a path
        one keystroke at a time never trips this. Returning True swallows
        the insertion — the path becomes an upload, not text."""
        if self._suppress_insert_hook or "\n" in text:
            return False
        path = _as_media_path(text)
        if not path:
            return False
        self._start_upload(path)
        return True

    def _on_drop(self, _target, value, _x, _y):
        if isinstance(value, Gdk.FileList):
            paths = []
            for f in value.get_files():
                path = f.get_path()
                if path and _as_media_path(path):
                    paths.append(path)
                if len(paths) >= MAX_ATTACHMENTS:
                    break
            if paths:
                self._start_uploads(paths)
                return True
            return False
        path = value.get_path() if isinstance(value, Gio.File) else None
        if path and _as_media_path(path):
            self._start_upload(path)
            return True
        return False

    # -- preview chips ------------------------------------------------------

    def _add_media_chip(self, url, local_path=None, thumb_pixbuf=None):
        """One removable chip per attached URL. Phase 4: a 40px thumbnail —
        video gets a static icon (a real first-frame grab needs a GStreamer
        pipeline per file; not worth it on the phone), images decode the
        LOCAL file async so a big photo never stalls the main loop."""
        chip = Gtk.Box(
            css_classes=["toolbar"], spacing=4, margin_top=2, margin_bottom=2
        )
        chip.add_css_class("flat")
        if url.lower().rsplit("?", 1)[0].endswith(_VIDEO_EXTS) or (
            local_path and local_path.lower().endswith(_VIDEO_EXTS)
        ):
            thumb = Gtk.Image.new_from_icon_name(resolve_icon(_ICON_CANDIDATES["video"]))
            thumb.set_pixel_size(_CHIP_THUMB_PX)
            chip.append(thumb)
        elif thumb_pixbuf is not None:
            thumb = Gtk.Image.new_from_pixbuf(thumb_pixbuf)
            chip.append(thumb)
        else:
            thumb = Gtk.Image.new_from_icon_name(
                resolve_icon(_ICON_CANDIDATES["media"])
            )
            thumb.set_pixel_size(_CHIP_THUMB_PX)
            chip.append(thumb)
            if local_path and os.path.isfile(local_path):
                self._load_chip_thumb(thumb, local_path)
        name = url.rsplit("/", 1)[-1] or url
        label = Gtk.Label(label=name if len(name) <= 24 else name[:21] + "…")
        label.set_tooltip_text(url)
        remove = Gtk.Button(icon_name="list-remove-symbolic")
        remove.add_css_class("flat")
        remove.add_css_class("dim-label")
        remove.set_valign(Gtk.Align.CENTER)
        remove.connect("clicked", self._on_chip_remove, url)
        chip.append(label)
        chip.append(remove)
        self._chips.append((chip, url))
        self.media_bar.append(chip)
        self.media_bar.set_visible(True)
        self._chip_scroll.set_visible(True)

    def _load_chip_thumb(self, image, path):
        """Decode a 40px thumbnail off the main thread (GdkPixbuf on a
        multi-MB phone photo is measurable on the A53s). The image may be
        destroyed before the decode lands — a widget detached from the tree
        has no parent (Gtk.Image's GTK3 is_valid check does not exist here)."""

        def worker():
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_size(
                    path, _CHIP_THUMB_PX, _CHIP_THUMB_PX
                )
            except Exception:
                return
            GLib.idle_add(self._set_chip_thumb, image, pb)

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _set_chip_thumb(image, pb):
        if image.get_parent() is not None:
            image.set_from_pixbuf(pb)
        return False

    def _on_chip_remove(self, _btn, url):
        # Drop the URL line from the text and the matching imeta tag.
        buf = self.text_view.get_buffer()
        text = buf.get_text(*buf.get_bounds(), False)
        kept = [ln for ln in text.split("\n") if ln.strip() != url]
        if len(kept) != len(text.split("\n")):
            self._set_text("\n".join(kept))
        self.media_tags = [t for t in self.media_tags if f"url={url}" not in t[1]]
        for chip, chip_url in list(self._chips):
            if chip_url == url:
                chip.unparent()
                self._chips.remove((chip, url))
        if not self._chips:
            self.media_bar.set_visible(False)
            self._chip_scroll.set_visible(False)

    # -- internals -------------------------------------------------------

    def _set_text(self, text):
        """Programmatic buffer write that must not trip the paste-path hook
        (set_text emits insert-text; a buffer whose whole content happens to
        be a media path would otherwise re-trigger an upload)."""
        self._suppress_insert_hook = True
        try:
            self.text_view.get_buffer().set_text(text)
        finally:
            self._suppress_insert_hook = False

    def _sync_button_sensitivity(self, *_args):
        buf = self.text_view.get_buffer()
        has_sel = buf.get_has_selection()
        for btn in self._feature_buttons.values():
            btn.set_sensitive(has_sel)
        if self._placeholder_label is not None:
            # GTK placeholder semantics: visible only when the buffer is
            # completely empty — whitespace is text and hides it.
            self._placeholder_label.set_visible(buf.get_char_count() == 0)

    def _apply(self, new_text, sel_start, sel_end):
        buf = self.text_view.get_buffer()
        self._set_text(new_text)
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
        self._set_text(new_text)
        buf.place_cursor(buf.get_iter_at_offset(caret))
        self._link_entry.set_text("")
        self._link_btn.get_popover().popdown()

    # -- media attach (phase 2) -------------------------------------------

    def _on_attach_clicked(self, _btn):
        if self._pending:
            return
        parent = self.get_root()
        chooser = Gtk.FileChooserNative.new(
            "Attach media",
            parent if isinstance(parent, Gtk.Window) else None,
            Gtk.FileChooserAction.OPEN,
            "_Open",
            "_Cancel",
        )
        chooser.set_select_multiple(True)
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
        paths = []
        if response == Gtk.ResponseType.ACCEPT:
            files = chooser.get_files()
            for f in files:
                path = f.get_path() if f else None
                if path:
                    paths.append(path)
        chooser.destroy()
        self._chooser = None
        if paths:
            self._start_uploads(paths)

    def _start_uploads(self, paths):
        """Fire every file off at once — uploads run concurrently on their own
        worker threads and the button stays down until the LAST one lands."""
        for p in paths:
            self._start_upload(p)

    def _start_upload(self, path, cleanup=False, thumb=None):
        """Spawn a worker upload. `cleanup=True` means the file is ours (a
        raw-paste temp PNG) and is deleted once the worker has read it.
        `thumb` is an already-decoded pixbuf (raw paste) for the chip."""
        from .. import uploader

        self._pending += 1
        self._media_btn.set_sensitive(False)
        self._media_btn.set_tooltip_text("Uploading…")
        sign_fn = self.sign_fn

        def worker():
            try:
                result = uploader.upload_file(path, sign_fn=sign_fn)
                GLib.idle_add(self._on_upload_done, result, None, path, thumb)
            except uploader.UploadError as e:
                GLib.idle_add(self._on_upload_done, None, str(e), path, thumb)
            except Exception as e:  # never kill the worker silently
                GLib.idle_add(
                    self._on_upload_done, None, f"Upload failed: {e}", path, thumb
                )
            finally:
                if cleanup:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

        threading.Thread(target=worker, daemon=True).start()

    def _on_upload_done(self, result, error, local_path=None, thumb=None):
        self._pending = max(0, self._pending - 1)
        if self._pending == 0:
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
        self._set_text(new_text)
        buf.place_cursor(buf.get_iter_at_offset(caret))
        self._add_media_chip(url, local_path=local_path, thumb_pixbuf=thumb)
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
