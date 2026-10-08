# src/gnostr/ui/dm_thread_view.py
"""DM thread — chat-style conversation with one peer (spec:
docs/specs/nip44-nip17-dms.md, UI + composer contracts from
@linux-desktop-researcher).

Layout: AdwToolbarView. Top: AdwHeaderBar (peer name + short npub subtitle,
back button — Phosh gesture-exclusion: back is a header button, not only an
edge swipe). Content: vexpand GtkScrolledWindow -> GtkListBox of bubble rows
(reverse=false, scroll-on-focus=false per contract). Bottom-bar: composer —
single-line GtkEntry + suggested-action Send button (48x48 min). The
bottom-bar survives the OSK on Phosh: keyboard-resize clamps the bubble
ScrolledWindow, never truncates the composer. No modal compose reuse.

Behavior:
- Bottom-anchored: auto-scroll to bottom on row insert, suppressed when the
  user has scrolled up >1 viewport — an unread pill button appears instead.
- Optimistic send: a 'sending' bubble at opacity 0.6 keyed by the rumor event
  id; solid on publish-result ack, error-styled with an inline Retry button
  on failure (no dialogs). Send is disabled (not hidden) while the entry is
  blank — layout shift would break the OSK.
- Decryption never happens here: rows come from the DM gateway (plaintext is
  written back by DMService off-thread); live updates ride the client's
  dm-conversations-updated / dm-decrypted signals.
- Opening the thread zeroes the conversation's unread_count (mark_read) and
  refreshes the sidebar badge.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from ..nostr_utils import hex_to_npub
from .dm_inbox_view import format_relative_time

PAGE_SIZE = 50


class DMThreadView(Gtk.Box):
    def __init__(self, main_window, peer_pubkey):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.main_window = main_window
        self.peer_pubkey = peer_pubkey
        self._seen_ids = set()
        # event_id -> {"row": GtkListBoxRow, "box": Gtk.Box, "status": str}
        self._pending = {}
        self._new_since_view = 0

        prof = main_window.db.get_profile(peer_pubkey) or {}
        name = prof.get("display_name") or prof.get("name") or peer_pubkey[:8]
        npub = hex_to_npub(peer_pubkey) or peer_pubkey

        toolbar = Adw.ToolbarView()

        header = Adw.HeaderBar()
        # Back as an explicit header button (Phosh top-bar gesture exclusion).
        btn_back = Gtk.Button(icon_name="go-previous-symbolic")
        btn_back.set_tooltip_text("Back")
        btn_back.connect("clicked", lambda b: main_window.content_nav.pop())
        header.pack_start(btn_back)
        self.title = Adw.WindowTitle(title=name, subtitle=f"{npub[:16]}…")
        header.set_title_widget(self.title)
        toolbar.add_top_bar(header)

        # Bubble list: ScrolledWindow (vexpand) -> AdwClamp(720) -> ListBox.
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_vexpand(True)
        self.scroll.set_propagate_natural_height(False)
        # Contract: reverse=false, scroll-on-focus=false.
        self.scroll.set_scroll_on_focus(False)

        self.bubble_box = Gtk.ListBox()
        self.bubble_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.bubble_box.set_activate_on_single_click(False)
        self.bubble_box.add_css_class("dm-bubble-list")

        clamp = Adw.Clamp()
        clamp.set_maximum_size(720)
        clamp.set_tightening_ratio(0.7)
        clamp.set_child(self.bubble_box)
        self.scroll.set_child(clamp)

        # Unread pill overlay: shown instead of auto-scroll when the user has
        # scrolled up more than one viewport.
        self.unread_pill = Gtk.Button(label="New messages ↓")
        self.unread_pill.add_css_class("pill")
        self.unread_pill.add_css_class("suggested-action")
        self.unread_pill.set_valign(Gtk.Align.END)
        self.unread_pill.set_halign(Gtk.Align.CENTER)
        self.unread_pill.set_margin_bottom(12)
        self.unread_pill.set_no_show_all(True)
        self.unread_pill.connect("clicked", lambda b: self._scroll_to_bottom())

        overlay = Gtk.Overlay()
        overlay.set_child(self.scroll)
        overlay.add_overlay(self.unread_pill)
        toolbar.set_content(overlay)

        # Composer: bottom-bar (OSK-safe), entry + Send. Send is disabled,
        # never hidden, while the entry is blank.
        composer = Gtk.Box(spacing=8)
        composer.set_margin_start(12)
        composer.set_margin_end(12)
        composer.set_margin_top(6)
        composer.set_margin_bottom(6)

        self.entry = Gtk.Entry()  # Gtk.Entry is single-line by default
        self.entry.set_placeholder_text("Message…")
        self.entry.set_hexpand(True)
        self.entry.set_enable_ime(True)
        self.entry.connect("changed", self._on_entry_changed)
        # Send sensitive on Enter (desktop).
        self.entry.connect("activate", lambda e: self._on_send())
        composer.append(self.entry)

        self.btn_send = Gtk.Button(label="Send")
        self.btn_send.add_css_class("suggested-action")
        self.btn_send.set_valign(Gtk.Align.CENTER)
        self.btn_send.set_size_request(48, 48)
        self.btn_send.set_sensitive(False)
        self.btn_send.connect("clicked", lambda b: self._on_send())
        composer.append(self.btn_send)

        toolbar.add_bottom_bar(composer)
        self.append(toolbar)

        # Live updates.
        main_window.client.connect("dm-conversations-updated", self._on_dm_updated)
        svc = getattr(main_window.client, "dm_service", None)
        if svc is not None:
            svc.connect("dm-decrypted", self._on_decrypted)
        main_window.client.connect("publish-result", self._on_publish_result)

        # Opening the thread marks it read (contract) + refreshes the badge.
        gw = getattr(main_window.client, "dm_gateway", None)
        if gw is not None:
            try:
                gw.mark_read(peer_pubkey)
            except Exception:
                pass
        main_window.update_dm_badge()

        self.load_thread()
        # Keep the entry focused so the OSK comes up on tap without stealing
        # focus away after send (contract).
        self.entry.grab_focus()

    # ------------------------------------------------------------------
    # bubbles
    # ------------------------------------------------------------------

    def _peer_name(self):
        prof = self.main_window.db.get_profile(self.peer_pubkey) or {}
        return prof.get("display_name") or prof.get("name") or self.peer_pubkey[:8]

    def _make_bubble(self, msg, status=None):
        """One bubble row. status None/'sent' = solid; 'sending' = opacity
        0.6; 'failed' = error style + inline Retry."""
        mine = msg["sender_pk"] == self.main_window.pub_key
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        bubble = Gtk.Label(label=msg["content"] or "")
        bubble.set_wrap(True)
        bubble.set_selectable(True)
        bubble.set_xalign(0)
        # Contract: bubbles cap at ~70% width. GTK CSS can't express a
        # percentage width, so cap the label's natural width in chars —
        # ~70% of a phone-width clamp (360 logical px ≈ 40 chars).
        bubble.set_max_width_chars(40)
        bubble.add_css_class("dm-bubble")
        bubble.add_css_class("dm-out" if mine else "dm-in")
        box.append(bubble)

        meta = Gtk.Label(label=format_relative_time(msg.get("rumor_ts")))
        meta.add_css_class("caption")
        meta.add_css_class("dim-label")
        meta.set_xalign(1.0 if mine else 0.0)
        box.append(meta)

        row = Gtk.ListBoxRow()
        row.set_child(box)
        row.set_selectable(False)
        row.set_activatable(False)
        box.set_halign(Gtk.Align.END if mine else Gtk.Align.START)
        row.event_id = msg["id"]

        if status == "sending":
            box.set_opacity(0.6)
        elif status == "failed":
            box.set_opacity(1.0)
            bubble.add_css_class("dm-bubble-error")
            meta.set_text("Not sent")
            retry = Gtk.Button(label="Retry")
            retry.add_css_class("flat")
            retry.set_size_request(48, 48)  # contract: interactive nodes >=48px
            retry.set_halign(Gtk.Align.END if mine else Gtk.Align.START)
            retry.connect("clicked", lambda b, m=msg: self._retry(m))
            box.append(retry)
        return row, box

    def load_thread(self):
        """Full (re)load of the newest page, oldest-first into the box
        (bottom-anchored chat: newest at the bottom)."""
        gw = getattr(self.main_window.client, "dm_gateway", None)
        if gw is None:
            return
        msgs = gw.get_thread(self.peer_pubkey, PAGE_SIZE, None)
        msgs.reverse()  # gateway returns newest-first; chat renders oldest-first
        for r in [c for c in self.bubble_box.get_children()]:
            self.bubble_box.remove(r)
        self._seen_ids = set()
        for m in msgs:
            self._append_msg(m)
        self._scroll_to_bottom(force=True)

    def _append_msg(self, msg, status=None):
        if msg["id"] in self._seen_ids:
            return None
        self._seen_ids.add(msg["id"])
        row, box = self._make_bubble(msg, status=status)
        self.bubble_box.append(row)
        return row

    # ------------------------------------------------------------------
    # scrolling (contract: auto-scroll to bottom on insert; suppress when
    # the user scrolled up >1 viewport, show unread pill instead)
    # ------------------------------------------------------------------

    def _scrolled_up_beyond_viewport(self):
        adj = self.scroll.get_vadjustment()
        if adj is None:
            return False
        above = adj.get_upper() - (adj.get_value() + adj.get_page_size())
        return above > adj.get_page_size()

    def _scroll_to_bottom(self, force=False):
        if not force and self._scrolled_up_beyond_viewport():
            self._new_since_view += 1
            self.unread_pill.set_label(f"{self._new_since_view} new ↓")
            self.unread_pill.show()
            return
        self._new_since_view = 0
        self.unread_pill.hide()
        adj = self.scroll.get_vadjustment()
        if adj is not None:
            adj.set_value(adj.get_upper() - adj.get_page_size())

    # ------------------------------------------------------------------
    # composer
    # ------------------------------------------------------------------

    def _on_entry_changed(self, entry):
        # Disable (never hide) Send while blank — layout shift breaks the OSK.
        self.btn_send.set_sensitive(bool(entry.get_text().strip()))

    def _on_send(self):
        text = self.entry.get_text().strip()
        if not text:
            return
        self.entry.set_text("")
        self.entry.grab_focus()  # entry stays focused/empty after send (contract)
        self._send_text(text)

    def _send_text(self, text):
        signed = self.main_window.client.publish_dm(self.peer_pubkey, text)
        if signed is None:
            self._show_failed(
                {
                    "id": None,
                    "sender_pk": self.main_window.pub_key,
                    "content": text,
                    "rumor_ts": None,
                }
            )
            return
        msg = {
            "id": signed["id"],
            "sender_pk": signed["pubkey"],
            "content": text,
            "rumor_ts": signed.get("created_at"),
        }
        row = self._append_msg(msg, status="sending")
        self._pending[signed["id"]] = {"row": row, "msg": msg}
        self._scroll_to_bottom()

    def _retry(self, msg):
        row = self._pending.pop(msg["id"], {}).get("row")
        if row is not None:
            self.bubble_box.remove(row)
        self._seen_ids.discard(msg["id"])
        self._send_text(msg["content"])

    def _show_failed(self, msg):
        row, _ = self._make_bubble(msg, status="failed")
        self.bubble_box.append(row)
        if msg["id"]:
            self._pending[msg["id"]] = {"row": row, "msg": msg}
        self._scroll_to_bottom()

    # ------------------------------------------------------------------
    # signals
    # ------------------------------------------------------------------

    def _on_publish_result(self, client, event_id, accepted, message, relay_url, label):
        if label != "DM" or event_id not in self._pending:
            return
        entry = self._pending.pop(event_id)
        row = entry["row"]
        if row is None:
            return
        if accepted:
            # Solid on ack: drop the 0.6 sending opacity.
            child = row.get_child()
            if child is not None:
                child.set_opacity(1.0)
        else:
            self._seen_ids.discard(event_id)
            self.bubble_box.remove(row)
            self._show_failed(entry["msg"])

    def _on_dm_updated(self, client, peer_pk):
        if peer_pk != self.peer_pubkey:
            return
        gw = getattr(self.main_window.client, "dm_gateway", None)
        if gw is None:
            return
        # Append only unseen messages (live insert), keep scroll discipline.
        msgs = gw.get_thread(self.peer_pubkey, PAGE_SIZE, None)
        for m in reversed(msgs):
            if m["id"] not in self._seen_ids:
                self._append_msg(m)
                self._scroll_to_bottom()
        # A freshly arrived inbound DM while the thread is open is read.
        try:
            gw.mark_read(self.peer_pubkey)
        except Exception:
            pass
        self.main_window.update_dm_badge()

    def _on_decrypted(self, service, event, plaintext):
        """A message finished decrypting off-thread: if its bubble is in this
        thread, swap the raw payload for plaintext (no crypto on the UI
        thread — this signal is emitted on the main context)."""
        eid = event.get("id")
        if eid not in self._seen_ids:
            return
        for row in self.bubble_box:
            if getattr(row, "event_id", None) == eid:
                box = row.get_child()
                child = box.get_first_child() if box is not None else None
                if isinstance(child, Gtk.Label):
                    child.set_text(plaintext)
                break
