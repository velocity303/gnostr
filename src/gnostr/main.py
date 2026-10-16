#!/usr/bin/env python3
import json
import os
import sys
import time
import traceback

import gi

import gnostr

from .client import NostrClient
from .database import Database
from .dialogs import LoginDialog, RelayPreferencesWindow
from .key_manager import KeyManager
from .renderer import ContentRenderer, ImageLoader
from .ui.feed_view import FeedView
from .ui.post_widget import PostWidget
from .ui.sidebar import Sidebar

# Ensure package imports work when run as __main__
if __name__ == "__main__":
    # Add parent directory to sys.path so `import gnostr` works
    parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent not in sys.path:
        sys.path.insert(0, parent)

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_title("Gnostr")
        self.set_default_size(950, 700)

        self.db = Database()
        self.client = NostrClient(self.db)
        self.client.connect("event-received", self.on_event_received)
        self.client.connect("quote-event-received", self.on_quote_event_received)
        self.client.connect("status-changed", self.on_status_changed)
        self.client.connect("contacts-updated", self.on_contacts_updated)
        self.client.connect("profile-updated", self.on_profile_updated)
        self.client.connect("metrics-updated", self.on_metrics_updated)
        self.client.connect("publish-result", self.on_publish_result)
        self.client.connect("relay-log-updated", self.on_relay_log_updated)
        self.client.connect("dm-unreachable", self.on_dm_unreachable)

        self.priv_key = None
        self.pub_key = None
        self.active_feed_type = "following"
        self.event_widgets = {}
        self.active_profile_pubkey = None

        # 1. Root: Toast Overlay
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        # 2. Main Stack (direct child of ToastOverlay — no Gtk.Overlay wrapper)
        self.main_stack = Adw.ViewStack()
        self.toast_overlay.set_child(self.main_stack)

        # FAB Button — place on the split view's content page via its own overlay.
        # "edit-create-symbolic" is NOT in Adwaita/Phosh (verified: absent from
        # symbolic/actions/), so the FAB rendered with the wrong/fallback glyph.
        # Standard compose-new glyph in GNOME is the pencil-on-paper
        # document-edit-symbolic; document-new-symbolic is the + fallback.
        self.fab_post = Gtk.Button(
            icon_name=self._resolve_fab_icon(),
            css_classes=["suggested-action", "pill"],
        )
        self.fab_post.set_valign(Gtk.Align.END)
        self.fab_post.set_halign(Gtk.Align.END)
        self.fab_post.set_margin_bottom(20)
        self.fab_post.set_margin_end(20)
        self.fab_post.connect("clicked", self.on_fab_post_clicked)
        self.fab_post.set_visible(False)

        # 3. App View: Split View
        self.split_view = Adw.NavigationSplitView()
        bp = Adw.Breakpoint.new(
            Adw.BreakpointCondition.new_length(
                Adw.BreakpointConditionLengthType.MAX_WIDTH, 800, Adw.LengthUnit.SP
            )
        )
        bp.add_setter(self.split_view, "collapsed", True)
        self.add_breakpoint(bp)

        # UI Components
        self.sidebar = Sidebar(self)
        self.sidebar_page = Adw.NavigationPage(title="Menu", tag="sidebar")
        self.sidebar_page.set_child(self.sidebar)
        self.split_view.set_sidebar(self.sidebar_page)

        self.feed_view = FeedView(self)
        # Expose the feed's touch-activation cooldown to widgets via the window
        # ref so renderer/post_widget can consult it with a getattr guard (None
        # on non-feed pages keeps their handlers inert). Reset on feed switch.
        self.scroll_gate = None
        self.feed_view._scroll_gate.reset()
        self.scroll_gate = self.feed_view._scroll_gate
        self.content_nav = Adw.NavigationView()

        # Overlay wraps the navigation view, FAB floats on top
        self.content_overlay = Gtk.Overlay()
        self.content_overlay.set_child(self.content_nav)
        self.content_overlay.add_overlay(self.fab_post)

        # NavigationPage contains the overlay — split_view expects a NavigationPage
        self.feed_page = Adw.NavigationPage(title="Content", child=self.content_overlay)
        self.content_nav.push(self.feed_view)
        self.split_view.set_content(self.feed_page)

        # 4. Login View
        self.login_page = Adw.StatusPage(
            title="Welcome", icon_name="avatar-default-symbolic"
        )
        lb = Gtk.Button(label="Login", css_classes=["pill", "suggested-action"])
        lb.connect("clicked", self.on_login_clicked)
        bx = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        bx.append(lb)
        ca = Gtk.Button(label="Create account", css_classes=["pill"])
        ca.connect("clicked", self.on_create_account_clicked)
        bx.append(ca)
        cs = Gtk.Button(label="Connect Signer (bunker)", css_classes=["pill"])
        cs.connect("clicked", self.on_connect_signer_clicked)
        bx.append(cs)
        # P3 #16: NIP-06 is `unrecommended` by its own NIP — the entry point
        # is labelled power-user and the dialog repeats the warning.
        dv = Gtk.Button(
            label="Derive from seed phrase (power user)", css_classes=["flat"]
        )
        dv.connect("clicked", self.on_derive_key_clicked)
        bx.append(dv)
        self.login_page.set_child(bx)

        # 5. Assemble Stack
        self.main_stack.add_named(self.login_page, "login")
        self.main_stack.add_named(self.split_view, "app")

        self.detect_display_metrics()
        saved = KeyManager.load_key()
        if saved:
            self.perform_login(saved)
        else:
            self.main_stack.set_visible_child_name("login")
            self._try_restore_bunker_session()

        GLib.idle_add(self.client.connect_all)
        GLib.timeout_add_seconds(300, self.on_auto_refresh)
        # Surface publishes that never got a relay OK (~30s expiry, sweep every 15s).
        GLib.timeout_add_seconds(15, self._sweep_publishes)

        # Populate the feed from the DB right away so Back never lands on a blank
        # feed (the startup subscribe is a no-op until relays connect; on_status_changed
        # re-runs switch_feed once the first relay is live).
        self._feed_live = False
        # Startup feed-freshness state (spec Phase B): the hold window routes
        # gap-fill events to the pill instead of flushing inline; the pulled
        # flag guards "exactly one freshness REQ per startup".
        self._freshness_hold = 0.0
        self._freshness_pulled = False
        if saved:
            GLib.idle_add(lambda: self.switch_feed(self.active_feed_type))

    def detect_display_metrics(self):
        try:
            display = Gdk.Display.get_default()
            monitors = display.get_monitors()
            if monitors.get_n_items() > 0:
                monitor = monitors.get_item(0)
                geo = monitor.get_geometry()
                scale = monitor.get_scale_factor()
                if geo.width < 600:
                    target_width = geo.width * scale
                    ImageLoader.MAX_WIDTH = int(target_width)
                else:
                    ImageLoader.MAX_WIDTH = 800
        except Exception:
            ImageLoader.MAX_WIDTH = 800

    def on_settings_clicked(self):
        RelayPreferencesWindow(self.client, self).present()

    def on_menu_selected(self, r_id):
        if r_id == "global":
            self.switch_feed("global")
        elif r_id == "following":
            self.switch_feed("following")
        elif r_id == "profile":
            if self.pub_key:
                self.show_profile(self.pub_key)
        elif r_id == "search":
            self.show_search_dialog()
        elif r_id == "followers":
            if self.pub_key:
                self.show_follows(self.pub_key)
        elif r_id == "messages":
            if self.pub_key:
                self.show_dm_inbox()
        self.split_view.set_show_content(True)

    def sync_follows(self):
        """Push the DB following list to relays (kind-3) — the behavior the
        old sidebar 'Follow Sync' row had. Shared by own-profile surfaces
        (ProfileView button + FollowsListView sync button)."""
        if not self.client.sync_followers():
            self.add_toast(Adw.Toast(title="Follow Sync: not logged in"))
        else:
            self.add_toast(
                Adw.Toast(title="Follow Sync published — watch Relay Activity")
            )

    def show_follows(self, owner_pubkey):
        """Open the Follows list for any owner (own or foreign profile)."""
        try:
            from gnostr.ui.followers_list_view import FollowersListView
        except ImportError:
            from .ui.followers_list_view import FollowersListView

        is_own = owner_pubkey == self.pub_key
        view = FollowersListView(self, owner_pubkey, is_own=is_own)
        page = Adw.NavigationPage(title="Follows", child=view)
        self.content_nav.push(page)

    def show_dm_inbox(self):
        """Open the DM inbox (Messages page)."""
        try:
            from gnostr.ui.dm_inbox_view import DMInboxView
        except ImportError:
            from .ui.dm_inbox_view import DMInboxView

        view = DMInboxView(self)
        page = Adw.NavigationPage(title="Messages", child=view)
        self.content_nav.push(page)

    def show_dm_recipient(self):
        """Open the New Message recipient picker (spec Phase A2). Mirrors
        show_dm_inbox: a NavigationPage on content_nav, never a dialog —
        every DM surface here is a nav page (modals lose the shared back
        contract on Phosh)."""
        try:
            from gnostr.ui.dm_recipient_view import DmRecipientView
        except ImportError:
            from .ui.dm_recipient_view import DmRecipientView

        view = DmRecipientView(self)
        page = Adw.NavigationPage(title="New Message", child=view)
        self.content_nav.push(page)

    def show_dm_thread(self, peer_pubkey):
        """Open the DM conversation with one peer (from an inbox row)."""
        try:
            from gnostr.ui.dm_thread_view import DMThreadView
        except ImportError:
            from .ui.dm_thread_view import DMThreadView

        view = DMThreadView(self, peer_pubkey)
        page = Adw.NavigationPage(title="DM", child=view)
        self.content_nav.push(page)

    def update_dm_badge(self):
        """Refresh the sidebar unread-DM badge from the gateway."""
        gw = getattr(self.client, "dm_gateway", None)
        count = 0
        if gw is not None:
            try:
                count = gw.total_unread()
            except Exception:
                count = 0
        self.sidebar.update_dm_badge(count)

    def on_login_clicked(self, btn):
        LoginDialog(self.client, self).present()

    def on_connect_signer_clicked(self, btn):
        from .dialogs import ConnectSignerWindow

        ConnectSignerWindow(self.client, self).present()

    def _try_restore_bunker_session(self):
        """Startup restore (P2 #15): a saved bunker session reconnects without
        user interaction. Runs on a worker thread — the handshake needs live
        relays, and relays connect asynchronously. Failure is silent: the
        login view simply stays up."""
        from .key_manager import BunkerSessionManager

        session = BunkerSessionManager.load_session()
        if not session:
            return

        def _worker():
            from .signer import reconnect_from_session

            try:
                backend = reconnect_from_session(
                    self.client,
                    session,
                    on_sign_request=self.confirm_sign_request,
                    timeout=20.0,
                )
            except Exception as e:
                print(f"⚠️ bunker session restore failed: {e}")
                return
            GLib.idle_add(self.perform_bunker_login, backend)

        import threading

        threading.Thread(target=_worker, daemon=True).start()

    def perform_bunker_login(self, backend, session_saved=True):
        """Switch to the app view with a connected BunkerBackend (P2 #15)."""
        self.priv_key = None
        self.pub_key = backend.get_public_key()
        self.client.set_signer(backend)
        self.main_stack.set_visible_child_name("app")
        self.sidebar.update_status("🟢")
        self.sidebar.set_signer_status(f"bunker {self.pub_key[:8]}…")
        self.fab_post.set_visible(True)
        GLib.idle_add(self.client.connect_all)
        if not session_saved:
            self.add_toast(
                Adw.Toast(title="Signed in — session NOT saved (keyring save failed)")
            )

    def on_disconnect_signer(self):
        """Sidebar Disconnect: logout RPC + destroy session, back to login."""
        backend = getattr(self.client, "signer", None)
        from .signer import BunkerBackend

        if isinstance(backend, BunkerBackend):
            try:
                backend.disconnect()
            except Exception:
                pass
        from .key_manager import BunkerSessionManager

        BunkerSessionManager.delete_session()
        self.client.set_keys(None, None)
        self.priv_key = None
        self.pub_key = None
        self.sidebar.set_signer_status(None)
        self.main_stack.set_visible_child_name("login")

    def confirm_sign_request(self, kind, preview):
        """Per-sign-request approval (P2 #15), called from the signer worker
        thread. Shows the prompt on the UI thread and blocks the worker until
        the user answers. Default-deny: no answer within 60s rejects."""
        import threading

        answer = {"ok": False}
        done = threading.Event()

        def _show():
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=f"Sign request: kind {kind}",
                body=(preview or "(empty)")[:200] or "(empty)",
            )
            dialog.add_response("deny", "Deny")
            dialog.add_response("allow", "Allow")
            dialog.set_default_response("deny")
            dialog.set_close_response("deny")

            def _resp(d, r):
                answer["ok"] = r == "allow"
                done.set()

            dialog.connect("response", _resp)
            dialog.present()
            return False

        GLib.idle_add(_show)
        done.wait(60.0)
        return answer["ok"]

    def on_create_account_clicked(self, btn):
        from .ui.onboarding import CreateAccountWindow

        CreateAccountWindow(self.client, self).present()

    def on_logout_clicked(self):
        KeyManager.delete_key()
        self.main_stack.set_visible_child_name("login")
        self.client.set_keys(None, None)

    def on_export_key_clicked(self):
        from .dialogs import ExportKeyDialog

        ExportKeyDialog(self).present()

    def on_derive_key_clicked(self, btn):
        from .ui.derive_key import DeriveKeyDialog

        DeriveKeyDialog(self.client, self).present()

    @staticmethod
    def _resolve_fab_icon():
        """Pick the compose-FAB icon against the ACTIVE icon theme.

        edit-create-symbolic (the old hard-coded name) does not exist in
        Adwaita/Phosh, so the button fell back to the theme's broken/missing
        glyph. Preference order, first hit wins — the probe itself is the
        shared ui.icons.resolve_icon helper (A1 factoring).
          document-edit-symbolic  — pencil-on-paper, GNOME's compose glyph
          document-new-symbolic   — plus-on-paper
          list-add-symbolic       — guaranteed-present fallback
        """
        try:
            from gnostr.ui.icons import resolve_icon
        except ImportError:
            from .ui.icons import resolve_icon
        return resolve_icon(
            (
                "document-edit-symbolic",
                "document-new-symbolic",
                "list-add-symbolic",
            )
        )

    def on_fab_post_clicked(self, button):
        from gnostr.dialogs import ComposeWindow

        def handle_post(text):
            if self.client.publish_post(text):
                self.add_toast(Adw.Toast(title="Post Published"))
            else:
                self.add_toast(Adw.Toast(title="Failed to Publish Post"))

        win = ComposeWindow(self, handle_post)
        win.present()

    def add_toast(self, toast):
        self.toast_overlay.add_toast(toast)

    def on_dm_unreachable(self, client, peer_pk, event_id):
        # Spec docs/specs/nip44-nip17-dms.md 'Relay targeting' step 3: the
        # recipient has no kind-10050, the DM was NOT published. Toast only —
        # never a modal dialog (UI contract).
        self.add_toast(Adw.Toast(title="Recipient is not reachable for DMs"))

    def on_refresh_clicked(self):
        self.client.check_connections()
        page = self.content_nav.get_visible_page()
        if page == self.feed_view:
            self.switch_feed(self.active_feed_type)
        self.add_toast(Adw.Toast(title="Refreshing..."))

    def on_auto_refresh(self):
        self.client.check_connections()
        return True

    def show_profile(self, pubkey):
        try:
            from gnostr.ui.profile_view import ProfileView
        except ImportError:
            from .ui.profile_view import ProfileView
        view = ProfileView(self, pubkey)
        page = Adw.NavigationPage(title=f"Profile {pubkey[:8]}", child=view)
        self.content_nav.push(page)

    def refresh_profile(self, pubkey):
        # Manual profile refresh: re-fetch metadata, then re-push a fresh
        # ProfileView so posts reload from the DB.
        self.client.check_connections()
        self.client.fetch_profile(pubkey)
        self.content_nav.pop()
        self.show_profile(pubkey)

    def show_thread(self, event_id, pubkey, content, tags=[]):
        try:
            from gnostr.ui.thread_view import ThreadView
        except ImportError:
            from .ui.thread_view import ThreadView
        # Prefer the DB-cached event so the hero renders accurate post data even
        # when the feed copy was stale/incomplete (e.g. quote cards opened with
        # "Loading...").
        cached = self.db.get_event_by_id(event_id)
        if cached:
            pubkey = cached["pubkey"]
            content = cached["content"]
            tags = cached.get("tags", [])
        view = ThreadView(self, event_id, pubkey, content, tags)
        page = Adw.NavigationPage(title="Thread", child=view)
        self.content_nav.push(page)
        # Fetch thread data including reactions
        self.client.fetch_thread(event_id)

    def show_search_dialog(self):
        from gnostr.dialogs import SearchDialog

        SearchDialog(self).present()

    def on_event_received(self, client, eid, pubkey, content, tags_json):
        import json

        tags = json.loads(tags_json)

        # If we are on the feed and this post belongs here, add it
        if self.content_nav.get_visible_page() == self.feed_view:
            # Model-driven feed: the client persisted this event to the DB
            # before emitting (client.py saves, then emits), so hydrate the
            # full row and let FeedModel decide: buffer for the "N new
            # posts" pill, or flush inline when the user is at the top.
            if self.feed_view.feed_model is not None:
                row = self.db.get_event_by_id(eid)
                if row is not None:
                    inserted = self.feed_view.feed_model.prepend_new([row])
                    if inserted:
                        # Startup freshness hold (spec Phase B2b): gap-fill
                        # events surface as the "N new posts" pill, never an
                        # inline flush — the user chooses when the viewport
                        # moves. Live events during the hold buffer too;
                        # the pill tap flushes everything in model order.
                        if (
                            getattr(self, "_freshness_hold", 0.0) > time.monotonic()
                        ) or not self.feed_view.feed_model.at_top:
                            self.feed_view.buffered_new(inserted)
                        else:
                            self.feed_view.flush_new()
                return
            # Legacy inline feed (global / me): dedup against posts already
            # rendered: the live subscription re-delivers DB-backfilled
            # events (persisted from a prior session) that would otherwise
            # render twice.
            child = self.feed_view.posts_box.get_first_child()
            while child is not None:
                if getattr(child, "event_id", None) == eid:
                    return
                child = child.get_next_sibling()
            w = PostWidget(self, pubkey, content, eid, tags)
            # Slot into the correct time position (newest-at-top), not a blind
            # prepend — backfilled/older posts keep the feed in time order.
            PostWidget.insert_time_sorted(self.feed_view.posts_box, w)

    def on_quote_event_received(self, client, event_json):
        """Populate pending quote/naddr cards when their event arrives.
        Fires for ALL kinds (addressable events never hit event-received)."""
        try:
            ev = json.loads(event_json)
        except Exception:
            return
        eid = ev.get("id")
        kind = ev.get("kind")
        pubkey = ev.get("pubkey")
        tags = ev.get("tags", [])

        # Addressable coordinate for naddr cards: kind:pubkey:d-tag.
        d_tag = ""
        for t in tags:
            if len(t) >= 2 and t[0] == "d":
                d_tag = t[1]
                break
        coordinate = f"{kind}:{pubkey}:{d_tag}" if d_tag else None

        for w in self.event_widgets.values():
            for match_key, quote_box in getattr(w, "quote_widgets", None) or ():
                mtype, mval = match_key
                matched = False
                if mtype == "id" and mval == eid:
                    matched = True
                elif mtype == "a" and coordinate and mval == coordinate:
                    matched = True
                if not matched:
                    continue
                # Clear the "Loading Quoted Event..." label, then render content.
                child = quote_box.get_first_child()
                while child is not None:
                    quote_box.remove(child)
                    child = quote_box.get_first_child()
                ContentRenderer._build_quote_content(quote_box, ev, self)

    def on_status_changed(self, client, status):
        emoji = {"CONNECTED": "🟢", "WARNING": "🟡", "DISCONNECTED": "🔴"}.get(
            status, "⚪"
        )
        self.sidebar.update_status(emoji)
        # First relay connection -> re-run switch_feed so the feed subscribes live
        # (the startup switch_feed ran before relays connected, so its subscribe
        # was a no-op).
        if status == "CONNECTED" and not self._feed_live:
            self._feed_live = True
            GLib.idle_add(lambda: self.switch_feed(self.active_feed_type))
            # Pull reconciliation: ask relays for our newest kind-3 so the DB
            # following table matches what the relays actually have (follows
            # that only hit the DB locally get corrected here).
            if self.pub_key:
                GLib.idle_add(self.client.fetch_contacts)
            # Startup feed-freshness (spec Phase B1): exactly one bounded
            # freshness REQ for the following feed, chained AFTER the
            # switch_feed idle hop (GLib runs idle handlers in add order)
            # so the newest cached created_at is read from the freshly
            # loaded FeedModel. request_once bypasses subscribe(), so the
            # live sub_following is never CLOSEd.
            GLib.idle_add(self._pull_feed_freshness)

    def _pull_feed_freshness(self):
        """One bounded freshness REQ per startup (spec Phase B1).

        The startup feed render is the local SQLite cache; when relays come
        up this pull fills the gap between the newest cached post and now,
        bounded by a 7 d floor so it is a gap-fill, not a re-sync. The 60 s
        overlap below the newest cached post absorbs relay clock skew and
        makes the FeedModel echo-back dedup load-bearing. Events land via
        prepend_new and surface as the 'N new posts' pill while the hold
        window is active (Phase B2b) — never an inline viewport jump.
        """
        if not self.pub_key or self._freshness_pulled:
            return
        contacts = self.db.get_following_list(self.pub_key)
        if not contacts:
            return  # deferred: re-armed on own contacts-updated (Phase B3)
        self._freshness_pulled = True
        newest = 0
        m = self.feed_view.feed_model
        if m is not None and m.top_key:
            newest = m.top_key[0]  # newest cached created_at
        since = max(newest - 60, int(time.time()) - 7 * 86400)
        self._freshness_hold = time.monotonic() + 5.0  # pill-hold window (B2b)
        self.client.request_once(
            f"fresh_following_{int(time.time())}",
            {"kinds": [1], "authors": contacts[:300], "since": since, "limit": 50},
        )

    def on_contacts_updated(self, client, pubkey):
        # An owner's following list was reconciled (own or foreign kind-3).
        # Open FollowsListView widgets self-refresh via the signal; nothing
        # else needs updating here — except the freshness re-arm (Phase B3):
        # our own kind-3 arriving with no freshness pull yet means the
        # following table was empty at CONNECTED time (first-ever login),
        # so fire the one bounded pull now.
        if pubkey == self.pub_key and not self._freshness_pulled:
            self._pull_feed_freshness()

    def on_profile_updated(self, client, pubkey):
        # Profile metadata (kind 0) arrived — refresh avatars/names in existing
        # post widgets so previously-missing profile pics/names actually appear.
        prof = self.db.get_profile(pubkey)
        if not prof:
            return
        name = prof.get("display_name") or prof.get("name") or pubkey[:8]
        pic = prof.get("picture")
        for w in self.event_widgets.values():
            if getattr(w, "pubkey", None) == pubkey:
                if name:
                    w.lbl_name.set_label(name)
                if pic:
                    ImageLoader.load_avatars(
                        pic, lambda t, w=w: w.avatar.set_custom_image(t)
                    )
            # Update inline @mention labels that reference this pubkey.
            for lbl in getattr(w, "inline_mention_labels", None) or ():
                if not hasattr(lbl, "mention_fragments"):
                    continue
                new_frags = []
                changed = False
                for pk, frag in lbl.mention_fragments:
                    if pk == pubkey:
                        nm = prof.get("display_name") or prof.get("name") or pk[:8]
                        disp = (
                            f'<span weight="bold">@{GLib.markup_escape_text(nm)}</span>'
                        )
                        new_frags.append((pk, f'<a href="nostr:{pk}">{disp}</a>'))
                        changed = True
                    else:
                        new_frags.append((pk, frag))
                if changed:
                    lbl.mention_fragments = new_frags
                    lbl.set_label("".join(frag for _, frag in new_frags))

    def on_metrics_updated(self, client, eid, likes, reposts, replies):
        # Update labels on PostWidgets by event_id
        if eid in self.event_widgets:
            widget = self.event_widgets[eid]
            widget.lbl_likes.set_label(str(likes))
            widget.lbl_reposts.set_label(str(reposts))
            widget.lbl_replies.set_label(str(replies))

    def _sweep_publishes(self):
        # Callback for GLib.timeout_add_seconds — returns True to keep repeating.
        self.client.sweep_pending_publishes()
        return True

    def on_publish_result(self, client, event_id, accepted, message, relay_url, label):
        # Feedback for whether likes/follows/reposts/replies reached a relay.
        if accepted:
            self.add_toast(Adw.Toast(title=f"{label} ✓"))
        else:
            self.add_toast(Adw.Toast(title=f"{label} failed: {message}"))

    def on_relay_log_updated(self, client, line):
        # Append to the sidebar Relay Activity pane.
        self.sidebar.append_relay_log(line)

    def perform_login(self, priv_hex):
        # The keyring stores the nsec (see KeyManager.save_key) and the startup
        # path hands it straight here, while the login dialog passes hex. Accept
        # both: DMService/get_public_key need hex or they raise on "nsec…".
        if isinstance(priv_hex, str) and priv_hex.startswith("nsec"):
            priv_hex = gnostr.nostr_utils.nsec_to_hex(priv_hex)
        self.priv_key = priv_hex
        self.pub_key = gnostr.nostr_utils.get_public_key(priv_hex)
        # Push keys to the client — the fresh-login dialog path also calls
        # set_keys, so the saved-key startup path must too or publish_* (like,
        # repost, reply, follow) silently no-ops with "No private key loaded".
        self.client.set_keys(self.pub_key, self.priv_key)
        # Build the DM gateway + service and open the kind-4 subscription at
        # login (not lazily on first rumor) so the inbox/thread views can
        # connect to dm-decrypted immediately.
        self.client.ensure_dm()
        self.main_stack.set_visible_child_name("app")
        self.sidebar.update_status("🟢")
        self.fab_post.set_visible(True)
        self.update_dm_badge()
        GLib.idle_add(self.client.connect_all)

    def switch_feed(self, feed_type):
        self.active_feed_type = feed_type
        # Model-driven "following" feed: FeedModel owns the window, cursor,
        # and live buffer. Other feeds keep the legacy inline render.
        if feed_type == "following" and self.pub_key:
            contacts = self.db.get_following_list(self.pub_key)
            if contacts:
                self.client.subscribe(
                    "sub_following",
                    {"kinds": [1], "authors": contacts[:300], "limit": 50},
                )
            self.feed_view.end_feed()
            self.feed_view.start_model_feed()
            return

        self.feed_view.end_feed()
        self.feed_view.clear_posts()
        cached = []
        if feed_type == "global":
            self.client.subscribe(
                "sub_global", {"kinds": [1], "limit": 20}, snapshot=True
            )
        elif feed_type == "me" and self.pub_key:
            cached = self.db.get_feed_for_user(self.pub_key)
            self.client.subscribe(
                "sub_me", {"kinds": [1], "authors": [self.pub_key], "limit": 20}
            )
        for ev in cached:
            w = PostWidget(
                self,
                ev["pubkey"],
                ev["content"],
                ev["id"],
                ev.get("tags", []),
                created_at=ev.get("created_at"),
            )
            # cached is newest-first (ORDER BY created_at DESC) — append builds
            # newest→oldest top-to-bottom. No per-item sorted rebuild needed.
            self.feed_view.posts_box.append(w)

    def make_post_widget(self, event_id):
        """Hydrate a PostWidget for a window id from the DB (the DB is the
        source of truth — FeedModel stores ids, not content). No-op when
        the widget already exists or the row is missing (e.g. a global/me
        event evicted from a feed that doesn't persist it)."""
        if event_id in self.event_widgets:
            return
        ev = self.db.get_event_by_id(event_id)
        if ev is None:
            return
        PostWidget(
            self,
            ev["pubkey"],
            ev["content"],
            ev["id"],
            ev.get("tags", []),
            created_at=ev.get("created_at"),
        )


class GnostrApp(Adw.Application):
    def __init__(self, **kwargs):
        super().__init__(
            application_id="tech.livingonlinux.gnostr",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
            **kwargs,
        )

    def do_activate(self):
        # Initialize GStreamer for video/GIF support
        try:
            from gi.repository import Gst

            Gst.init(None)
        except Exception as e:
            print(f"GStreamer init failed: {e}")
        # Register CSS BEFORE presenting so the first window layout pass sees
        # the toast rule: narrow phones (e.g. Librem 5: 720 px panel at scale
        # 2 = 360 logical px) cannot fit libadwaita's fixed 368 px toast
        # (+margins = 384 px) and warn "AdwToastOverlay exceeds MainWindow
        # width"; min-width:0 releases it (measured on-device: overlay min
        # drops 384 -> 51). GTK CSS has no @media/max-width — plain min-width.
        css = Gtk.CssProvider()
        css.load_from_string(
            ".liked { color: @accent_color; }"
            ".liked image { color: @accent_color; }"
            ".toast { min-width: 0px; }"
            # DM chat bubbles (contract: 16px radius; 70% max width is
            # enforced in code via set_max_width_chars — GTK CSS has no
            # percentage widths, and background-alpha is not a GTK property)
            ".dm-bubble { border-radius: 16px; padding: 8px 12px; }"
            ".dm-in { background-color: alpha(@card_bg_color, 1); }"
            ".dm-out { background-color: alpha(@accent_bg_color, 1); color: @accent_fg_color; }"
            ".dm-bubble-error { background-color: alpha(@error_bg_color, 0.25); }"
            # Inbox skeleton shimmer (fixed 64px rows; removed on first
            # gateway result, never on a timer)
            ".dm-skeleton { border-radius: 8px;"
            " background-color: alpha(@text_color, 0.08); }"
            ".dm-unread-dot { color: @accent_color; }"
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        win = self.props.active_window
        if not win:
            win = MainWindow(application=self)
        win.present()


def main(version):
    app = GnostrApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    main(None)
