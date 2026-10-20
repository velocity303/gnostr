# src/gnostr/ui/compose_box.py
"""RichComposeBox — the one composer widget for posts and (phase 2) DMs.

Scope: ~/Briefcase/gnostr-scopes/editor-gtk-scope.md. Gtk.TextView has no
rich text; the bar inserts markdown-lite MARKERS around the selection and
the wire content stays plain text (NIP-01). The marker set comes from
markup.SUPPORTED_FEATURES — the single source of truth shared with the
renderer — so the bar cannot offer anything the protocol spec forbids.

Context awareness = cursor + selection state, not the app page: emphasis
buttons are insensitive without a non-empty selection (locked decision:
selection required; no empty `****` insertion). The link button inserts a
bare URL at the caret (markdown [t](u) links are a spec non-goal).
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: E402

from ..markup import SUPPORTED_FEATURES, apply_marker, insert_at  # noqa: E402
from .icons import resolve_icon  # noqa: E402

# Candidate lists probed against the ACTIVE theme (house pattern, see
# ui/icons.py). format-text-code-symbolic is absent from the GNOME runtime
# (verified in the flatpak), utilities-terminal-symbolic is present.
_ICON_CANDIDATES = {
    "bold": ("format-text-bold-symbolic", "format-text-bold", "list-add-symbolic"),
    "italic": ("format-text-italic-symbolic", "format-text-italic", "list-add-symbolic"),
    "code": ("format-text-code-symbolic", "utilities-terminal-symbolic", "list-add-symbolic"),
    "link": ("insert-link-symbolic", "insert-link", "list-add-symbolic"),
}


class RichComposeBox(Gtk.Box):
    """Multiline text view + context-aware formatting bar.

    Public API used by hosts (ComposeWindow, later DM thread view):
      get_text() / set_text(text)
      text_view  — the Gtk.TextView (for placeholder, focus, etc.)
    """

    def __init__(self, placeholder=""):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self.text_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD, top_margin=12)
        if placeholder:
            self.text_view.set_placeholder_text(placeholder)
        scrolled = Gtk.ScrolledWindow(vexpand=True)
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
        self._link_btn = Gtk.MenuButton(icon_name=resolve_icon(_ICON_CANDIDATES["link"]))
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
        new_text, s, e = apply_marker(text, start.get_offset(), end.get_offset(), feature)
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
