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
from .key_manager import KeyManager, load_key_async
from .renderer import ContentRenderer, ImageLoader
from . import media_resolver
from .ui.feed_view import FeedView
from .ui.post_widget import PostWidget, TrustStubWidget
from .ui.sidebar import Sidebar
from .util import collapse as collapse_util

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
        # NIP-29 groups (spec nipw-communities-v1 §4): persistence behind the
        # groups-enabled GSettings flag (default TRUE — the feature is
        # additive; flag-off leaves no groups tables touched).
        if self._groups_enabled():
            try:
                self.client.get_groups_gateway()
            except Exception:
                pass
        self.client.connect("event-received", self.on_event_received)
        self.client.connect("quote-event-received", self.on_quote_event_received)
        self.client.connect("status-changed", self.on_status_changed)
        self.client.connect("contacts-updated", self.on_contacts_updated)
        self.client.connect("profile-updated", self.on_profile_updated)
        self.client.connect("metrics-updated", self.on_metrics_updated)
        self.client.connect("zap-received", self.on_zap_received)
        self.client.connect("publish-result", self.on_publish_result)
        self.client.connect("relay-log-updated", self.on_relay_log_updated)
        self.client.connect("dm-unreachable", self.on_dm_unreachable)
        # NIPW-P1 (NIP-B7/BUD-03): read-side Blossom recovery for dead
        # content-addressed media. The resolver is fed by the client's
        # kind-10063 relay traffic and consulted from the media loaders'
        # worker threads — the UI thread is never blocked by it.
        media_resolver.configure(self.client.fetch_blossom_servers)
        self.client.connect("blossom-servers-updated", media_resolver.on_blossom_event)
        # tor-v1-status (spec §A.2): sidebar icon + one-time toasts.
        self.client.connect("tor-state-changed", self.on_tor_state_changed)
        # A.2.1 item 2: any proxy-* GSettings change re-probes immediately.
        # Keep the Gio.Settings ref on self — a dropped ref unregisters the
        # signal handler.
        from gnostr import proxy_settings as _ps

        self._proxy_settings = _ps.watch_changes(self.client.on_proxy_setting_changed)
        # trust-shield-v1 §7: trust-shield-* GSettings changes reset client
        # session state (provider switch purges other providers' ranks).
        from gnostr import trust_settings as _ts

        self._trust_settings = _ts.watch_changes(self.client.on_trust_setting_changed)
        # Toast dedup flag (A.2.4): one toast per DOWN/DISABLED episode,
        # re-armed only by a transition back to REACHABLE.
        self._tor_toast_shown = False
        # NOTE: backfill-done is connected AFTER feed_view is constructed below —
        # referencing self.feed_view here crashed MainWindow.__init__ at startup.
        # Poll-delivered DMs must move the sidebar badge even while the user
        # sits on the feed (inbox/thread views only refresh while visible).
        self.client.connect(
            "dm-conversations-updated", lambda *_: self.update_dm_badge()
        )

        self.priv_key = None
        self.pub_key = None
        self.active_feed_type = "following"
        self.event_widgets = {}
        self.active_profile_pubkey = None

        # 1. Root: Toast Overlay
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        # Key-health banner (tux Q5, spec §C follow-up): replaces the
        # sidebar key-health label the §C trim removed. Sits above the
        # stack so it is visible on every app page; hidden when the key is
        # healthy (or the signer is remote — NIP-49 backup is local-only).
        self._root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.key_banner = Adw.Banner(
            title="Your key isn't backed up",
            button_label="Back up",
        )
        self.key_banner.set_revealed(False)
        self.key_banner.connect("button-clicked", self._on_key_banner_backup)
        self._root_box.append(self.key_banner)
        self.toast_overlay.set_child(self._root_box)

        # 2. Main Stack (inside the root box, below the banner)
        # vexpand is REQUIRED: Gtk.Box gives slack space only to vexpand
        # children, so without it the stack renders at natural height and
        # the bottom of the window stays blank (footer icons stranded
        # mid-screen, feed half-height). Before the banner wrapper the
        # stack was the ToastOverlay's direct child and filled by default.
        self.main_stack = Adw.ViewStack()
        self.main_stack.set_vexpand(True)
        self._root_box.append(self.main_stack)

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
        # Must come after FeedView exists (see note above the client wiring).
        self.client.connect("backfill-done", self.feed_view.on_backfill_done)
        self.content_nav = Adw.NavigationView()

        # Overlay wraps the navigation view, FAB floats on top
        self.content_overlay = Gtk.Overlay()
        self.content_overlay.set_child(self.content_nav)
        self.content_overlay.add_overlay(self.fab_post)
        # The FAB composes a NEW post — it is noise on every other page (DM
        # thread/inbox, profile, thread detail). Drive visibility from the
        # nav stack: only the root feed page shows it. notify::visible-page
        # fires on push AND pop, so back-navigation restores it automatically.
        self.content_nav.connect("notify::visible-page", self._update_fab_visibility)

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
        # Startup keyring lookup runs OFF the main thread (startup-hang fix):
        # Secret.password_lookup_sync blocks the GTK main loop until the D-Bus
        # secret service answers — multi-second on cold boot, INDEFINITELY on a
        # locked keyring, worse through the Flatpak portal. The login page is
        # the neutral startup state; a saved key switches to the app view from
        # the completion callback (_on_startup_key).
        self.main_stack.set_visible_child_name("login")
        # Double-activation guard: a manual login started while the lookup is
        # in flight wins — the late callback must not clobber it.
        self._startup_login_superseded = False
        load_key_async(self._on_startup_key)

        GLib.idle_add(self.client.connect_all)
        # tor-v1: paint the icon at startup (no tor-state-changed fires when
        # the state never changes, e.g. proxy disabled with onion relays
        # configured — A.2.4 wants the DISABLED toast once per session).
        self._refresh_tor_icon()
        self.on_tor_state_changed(self.client, self.client.tor_state.value)
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
                    # Phone form factor (phosh): the 950x700 default gets
                    # clamped to a partial-height window, leaving a blank
                    # band under the app (footer icons appeared mid-screen).
                    # Maximize so the sidebar footer sits at the screen edge.
                    self.maximize()
                else:
                    ImageLoader.MAX_WIDTH = 800
        except Exception:
            ImageLoader.MAX_WIDTH = 800

    def on_settings_clicked(self):
        win = RelayPreferencesWindow(self.client, self)
        # Key-health banner (tux Q5): a backup made from Settings → Account
        # must clear the banner when the window closes.
        win.connect("close-request", lambda *_: (self._refresh_key_banner(), False)[0])
        win.present()

    def confirm_logout(self):
        """Logout confirmation (spec §C / tux Q3): single AdwAlertDialog,
        default focus Cancel, destructive 'Log out'. Cancel keeps the
        session; only the logout response runs the existing flow."""
        dialog = Adw.AlertDialog(
            heading="Log out of gnostr?",
            body=(
                "Your feed cache stays on this device; you'll need your "
                "key or password to sign back in."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("logout", "Log out")
        dialog.set_response_appearance("logout", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _resp(d, response):
            if response == "logout":
                self.on_logout_clicked()

        dialog.connect("response", _resp)
        dialog.present(self)

    def _refresh_key_banner(self):
        """Key-health banner (tux Q5): shown whenever a LOCAL key is loaded
        and no backup has ever been verified. Replaces the sidebar key-health
        label the §C trim removed; 'Back up' opens Settings → Account."""
        from . import key_health

        degraded = bool(self.pub_key) and key_health.last_backup_verified() is None
        self.key_banner.set_revealed(degraded)
        if not degraded:
            return

    def _on_key_banner_backup(self, _banner):
        win = RelayPreferencesWindow(self.client, self)
        win.connect("close-request", lambda *_: (self._refresh_key_banner(), False)[0])
        try:
            win.set_visible_page(win.account_page)
        except Exception:
            pass
        win.present()

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
        elif r_id == "groups":
            self.show_groups()
        elif r_id == "articles":
            self.show_articles()
        self.split_view.set_show_content(True)

    @staticmethod
    def _groups_enabled():
        try:
            from gi.repository import Gio

            return Gio.Settings.new("tech.livingonlinux.gnostr").get_boolean(
                "groups-enabled"
            )
        except Exception:
            return True

    def show_articles(self):
        """Open the opt-in Articles section (NIPW P1 longform, kind 30023).

        Opening this row IS the opt-in: the live 30023 sub opens when the
        page maps and closes when it detaches — the kind-1 feed subs are
        never broadened (agentic-flood posture).
        """
        try:
            from gnostr.ui.articles_page import ArticlesPage
        except ImportError:
            from .ui.articles_page import ArticlesPage

        view = ArticlesPage(self)
        page = Adw.NavigationPage(title="Articles", child=view)
        self.content_nav.push(page)

    def show_article_reader(self, card):
        """Push the long-form reader for one ArticleCard (kind 30023)."""
        try:
            from gnostr.ui.articles_page import ArticleReaderPage
        except ImportError:
            from .ui.articles_page import ArticleReaderPage

        view = ArticleReaderPage(self, card)
        title = getattr(card, "title", None) or "Article"
        page = Adw.NavigationPage(title=title, child=view)
        self.content_nav.push(page)

    def show_drafts(self):
        """Open the read-only drafts list (NIP-63 31234; no composer in P1)."""
        try:
            from gnostr.ui.articles_page import DraftsPage
        except ImportError:
            from .ui.articles_page import DraftsPage

        view = DraftsPage(self)
        page = Adw.NavigationPage(title="Drafts", child=view)
        self.content_nav.push(page)

    def show_groups(self):
        """Open the Communities page (NIP-29 group registry)."""
        try:
            from gnostr.ui.groups_page import GroupsPage
        except ImportError:
            from .ui.groups_page import GroupsPage

        view = GroupsPage(self)
        page = Adw.NavigationPage(title="Communities", child=view)
        self.content_nav.push(page)

    def show_group_detail(self, relay, group_id, name=None, invite=None):
        """Open one community (preview-gated join flow lives in the page)."""
        try:
            from gnostr.ui.groups_page import GroupDetailPage
        except ImportError:
            from .ui.groups_page import GroupDetailPage

        view = GroupDetailPage(self, relay, group_id, name=name, invite=invite)
        page = Adw.NavigationPage(title=name or group_id, child=view)
        self.content_nav.push(page)

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
        self._startup_login_superseded = True
        LoginDialog(self.client, self).present()

    def on_connect_signer_clicked(self, btn):
        self._startup_login_superseded = True
        from .dialogs import ConnectSignerWindow

        ConnectSignerWindow(self.client, self).present()

    def _on_startup_key(self, saved):
        """Main-thread completion of the async startup keyring lookup
        (startup-hang fix). With a saved key: log in and schedule the feed
        load. Without one: the login page stays up and a saved bunker session
        gets its chance. Double-activation guard: a manual login started while
        the lookup was in flight (or any key already loaded) wins — this late
        callback must never clobber it."""
        if self._startup_login_superseded or self.pub_key or self.priv_key:
            return
        if saved:
            self.perform_login(saved)
            # Populate the feed from the DB right away so Back never lands on a
            # blank feed (the startup subscribe is a no-op until relays
            # connect; on_status_changed re-runs switch_feed on first relay).
            GLib.idle_add(lambda: self.switch_feed(self.active_feed_type))
        else:
            self._try_restore_bunker_session()

    def _try_restore_bunker_session(self):
        """Startup restore (P2 #15): a saved bunker session reconnects without
        user interaction. The libsecret read runs INSIDE the worker thread
        (startup-hang fix: a sync lookup here would block the main loop on a
        locked keyring) — the handshake needs live relays, and relays connect
        asynchronously. Failure is silent: the login view simply stays up."""

        def _worker():
            from .key_manager import BunkerSessionManager
            from .signer import reconnect_from_session

            session = BunkerSessionManager.load_session()
            if not session:
                return
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
        self.sidebar.update_status("CONNECTED")
        # Remote signer: no local key to back up (NIP-49 is local-only).
        self._refresh_key_banner()
        self.sidebar.set_signer_status(f"bunker {self.pub_key[:8]}…")
        self._update_fab_visibility()
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
        self._refresh_key_banner()
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
        self._startup_login_superseded = True
        from .ui.onboarding import CreateAccountWindow

        CreateAccountWindow(self.client, self).present()

    def on_logout_clicked(self):
        KeyManager.delete_key()
        self.main_stack.set_visible_child_name("login")
        self.client.set_keys(None, None)
        self._refresh_key_banner()

    def on_export_key_clicked(self):
        from .dialogs import ExportKeyDialog

        ExportKeyDialog(self).present()

    def on_derive_key_clicked(self, btn):
        self._startup_login_superseded = True
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

    def _update_fab_visibility(self, *args):
        """FAB shows only on the root feed page (see content_nav connect)."""
        on_feed = self.content_nav.get_visible_page() is self.feed_view
        self.fab_post.set_visible(bool(on_feed and self.pub_key))

    def on_fab_post_clicked(self, button):
        from gnostr.dialogs import ComposeWindow

        def handle_post(text, media_tags=None):
            if self.client.publish_post(text, extra_tags=media_tags):
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
                # The subscription can be broader than the feed (global
                # sub on the 'me' feed): the source decides membership.
                # A model without a source (legacy/test fake) accepts all.
                src = getattr(self.feed_view.feed_model, "source", None)
                if row is not None and (src is None or src.accepts(row)):
                    # Backfill history (older than the window tail) is NOT
                    # a "new post" — the two-tier backfill pages it in from
                    # the DB; a pill here would bump the viewport mid-scroll.
                    # A model without is_live (test fake) is treated live.
                    is_live = getattr(self.feed_view.feed_model, "is_live", None)
                    if is_live is not None and not is_live(row):
                        return
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
            # trust-shield-v1 §3.1: live-inserted posts obey the same policy.
            if self.client.trust_shield_decide(pubkey) == "collapse":
                w = TrustStubWidget(self, pubkey, content, eid)
            else:
                w = PostWidget(
                    self, pubkey, content, eid, tags,
                    collapse_cap=collapse_util.collapse_chars(),
                )
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

        if kind == 30023:
            # NIPW P1 C2: first-article-arrival evidence. A live 30023 from
            # a FOLLOWED author reveals the opt-in Articles row (section
            # sub or quote backfill); kind-1 routing is untouched.
            # getattr guard: handlers get bound onto duck-typed windows in
            # tests (FakeWindow); partial windows must not break routing.
            reveal = getattr(self, "_reveal_articles_for_author", None)
            if callable(reveal):
                reveal(pubkey)

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
        # §C trim: the sidebar footer carries state as a themed symbolic
        # icon + CSS class (success/warning/error), not a colored emoji.
        self.sidebar.update_status(status)
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

    def _refresh_articles_row(self):
        """Opt-in Articles row gate (NIPW P1 C2).

        Revealed only when the user follows at least one author that already
        has a 30023 in the local DB — offline evidence only, never a new REQ
        (relay economy; live 30023 arrivals reveal via
        _reveal_articles_for_author). Librem5 sense: no empty-section noise.
        """
        show = False
        try:
            if self.pub_key:
                following = set(self.db.get_following_list(self.pub_key) or [])
                if following:
                    # Capped evidence scan: the newest 100 stored articles
                    # prove "follows at least one 30023 author" at P1 scale
                    # (the section itself pages at 30 per relays).
                    for row in self.db.get_feed_kind(30023, limit=100) or []:
                        if row.get("pubkey") in following:
                            show = True
                            break
        except Exception:
            pass
        try:
            self.sidebar.set_articles_visible(show)
        except Exception:
            pass

    def _reveal_articles_for_author(self, pubkey):
        """First-arrival reveal (NIPW P1 C2): a live 30023 from a FOLLOWED
        author (section sub or quote backfill) reveals the Articles row.
        Arrivals from non-followed authors never reveal it."""
        try:
            if not self.sidebar or not self.pub_key or not pubkey:
                return
            if pubkey in (self.db.get_following_list(self.pub_key) or []):
                self.sidebar.set_articles_visible(True)
        except Exception:
            pass

    def on_contacts_updated(self, client, pubkey):
        # An owner's following list was reconciled (own or foreign kind-3).
        # Open FollowsListView widgets self-refresh via the signal; nothing
        # else needs updating here — except the freshness re-arm (Phase B3):
        # our own kind-3 arriving with no freshness pull yet means the
        # following table was empty at CONNECTED time (first-ever login),
        # so fire the one bounded pull now.
        if pubkey == self.pub_key and not self._freshness_pulled:
            self._pull_feed_freshness()
        # Zap subscription follows the follow list (spec §1.4): a new follow
        # means receipts for that recipient must start arriving live.
        if pubkey == self.pub_key:
            self.client.subscribe_zaps(
                self.db.get_following_list(self.pub_key) + [self.pub_key]
            )
            # NIPW P1 C2: the follow set just changed — re-evaluate the
            # opt-in Articles row gate (DB evidence only, no new REQ).
            refresh = getattr(self, "_refresh_articles_row", None)
            if callable(refresh):
                refresh()

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
        # Update labels on PostWidgets by event_id. TrustStubWidgets have no
        # footer labels — a metrics update for a collapsed author is a no-op.
        widget = self.event_widgets.get(eid)
        if widget is None or not hasattr(widget, "lbl_likes"):
            return
        widget.lbl_likes.set_label(str(likes))
        widget.lbl_reposts.set_label(str(reposts))
        widget.lbl_replies.set_label(str(replies))

    def on_zap_received(self, client, info_json):
        # A §1.4-verified 9735 landed (client already persisted it). Update
        # the zap chip on the target card with the per-event sats total.
        try:
            info = json.loads(info_json)
        except Exception:
            return
        eid = info.get("target_e")
        if not eid or eid not in self.event_widgets:
            return
        widget = self.event_widgets[eid]
        if not hasattr(widget, "set_zap_total"):
            return  # TrustStubWidget: no zap chip
        totals = self.db.zap_totals_for_events([eid])
        widget.set_zap_total(totals.get(eid, 0) // 1000)

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
        # The Settings → Relays page owns the log now (§C trim); it
        # subscribes to this signal directly. The sidebar call is a no-op
        # shim kept for API stability.
        self.sidebar.append_relay_log(line)

    # --- tor-v1-status (spec gnostr-tor-onion-support-v1 §A.2.3/A.2.4) ---

    def _refresh_tor_icon(self):
        from gnostr import proxy_settings as ps
        from gnostr.util.tor_state import host_is_onion

        onion_count = sum(1 for u in self.client.relay_urls if host_is_onion(u))
        self.sidebar.update_tor_icon(
            self.client.tor_state.value,
            ps.proxy_get("proxy-enabled"),
            onion_count,
            reason=getattr(self.client, "_tor_probe_reason", "ok"),
            host=ps.proxy_get("proxy-host"),
            port=ps.proxy_get("proxy-port"),
        )

    def on_tor_state_changed(self, client, state):
        self._refresh_tor_icon()
        from gnostr.util.tor_state import host_is_onion

        n = sum(1 for u in client.relay_urls if host_is_onion(u))
        if state == "reachable":
            # Re-arm the skip toast for the next DOWN episode (A.2.4).
            self._tor_toast_shown = False
            if n:
                self.add_toast(
                    Adw.Toast(title="Tor connected — onion relays enabled", timeout=5)
                )
        elif state in ("down", "disabled") and n:
            # One-time toast per episode: never per-relay, never per-probe.
            if not self._tor_toast_shown:
                self._tor_toast_shown = True
                self.add_toast(
                    Adw.Toast(
                        title=(
                            f"Tor proxy not reachable — {n} onion relay(s) "
                            "inactive until Tor connects"
                        ),
                        timeout=5,
                    )
                )

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
        self.sidebar.update_status("CONNECTED")
        self._refresh_key_banner()
        self._update_fab_visibility()
        self.update_dm_badge()
        GLib.idle_add(self.client.connect_all)
        # NIPW P1 C2: warm DB evidence (follows + any 30023 persisted by an
        # earlier session) decides the Articles row at login — no REQ here;
        # a first-ever login reveals later via on_contacts_updated/arrival.
        refresh = getattr(self, "_refresh_articles_row", None)
        if callable(refresh):
            GLib.idle_add(refresh)

    def switch_feed(self, feed_type):
        self.active_feed_type = feed_type
        # Every feed is model-driven (infinite-scroll phase 1): FeedModel
        # owns the window, keyset cursors, and live buffer for following,
        # global and me alike — the FeedSource picks the DB query per kind.
        if feed_type == "following" and self.pub_key:
            contacts = self.db.get_following_list(self.pub_key)
            if contacts:
                self.client.subscribe(
                    "sub_following",
                    {"kinds": [1], "authors": contacts[:300], "limit": 50},
                )
            # Zap receipts for everyone in the feed + self (spec §1.4
            # subscription). Runs even with no contacts: own zaps still land.
            self.client.subscribe_zaps(contacts + [self.pub_key])
        elif feed_type == "global":
            # limit matches the model page size so the DB has a full
            # first page (and one backfill page) to paginate over.
            self.client.subscribe(
                "sub_global", {"kinds": [1], "limit": 50}, snapshot=True
            )
        elif feed_type == "me" and self.pub_key:
            self.client.subscribe(
                "sub_me", {"kinds": [1], "authors": [self.pub_key], "limit": 50}
            )
        else:
            return
        self.feed_view.end_feed()
        self.feed_view.start_model_feed(feed_type)

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
        # trust-shield-v1 §3.1: low-rank authors render as a collapsed stub.
        # Flag off (default) => decide() is always 'normal' => untouched path.
        if self.client.trust_shield_decide(ev["pubkey"]) == "collapse":
            TrustStubWidget(
                self,
                ev["pubkey"],
                ev["content"],
                ev["id"],
                created_at=ev.get("created_at"),
            )
            return
        PostWidget(
            self,
            ev["pubkey"],
            ev["content"],
            ev["id"],
            ev.get("tags", []),
            created_at=ev.get("created_at"),
            collapse_cap=collapse_util.collapse_chars(),
        )

    def trust_reveal(self, author_pk):
        """trust-shield-v1 §3.1 'Show post': reveal instantly (content is
        already hydrated — no refetch) and swap every rendered stub of this
        author for the full card. Session-only: a relaunch re-collapses."""
        self.client.trust_shield_reveal(author_pk)
        for eid, w in list(self.event_widgets.items()):
            if not getattr(w, "is_trust_stub", False) or w.pubkey != author_pk:
                continue
            parent = w.get_parent()
            if parent is None:
                continue
            idx = parent.get_first_child()
            pos = 0
            while idx is not None and idx is not w:
                pos += 1
                idx = idx.get_next_sibling()
            ev = self.db.get_event_by_id(eid)
            if ev is None:
                continue
            # (cap computed via a local so the FEED-TRUNC source-pin in
            # test_feed_collapse keeps counting exactly its two feed sites)
            _cap = collapse_util.collapse_chars()
            PostWidget(
                self,
                ev["pubkey"],
                ev["content"],
                ev["id"],
                ev.get("tags", []),
                created_at=ev.get("created_at"),
                collapse_cap=_cap,
            )  # re-registers eid in event_widgets
            parent.insert(self.event_widgets[eid], pos)
            parent.remove(w)


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
        from . import debug_resize
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
            # trust-shield-v1 §3.1: the 'Low rank' pill on a collapsed stub
            ".pill-low-trust { border-radius: 999px; padding: 2px 10px;"
            " color: @warning_color;"
            " background-color: alpha(@warning_bg_color, 0.18); }"
            # Inbox skeleton shimmer (fixed 64px rows; removed on first
            # gateway result, never on a timer)
            ".dm-skeleton { border-radius: 8px;"
            " background-color: alpha(@text_color, 0.08); }"
            ".dm-unread-dot { color: @accent_color; }"
            # Status-icon state colors (sidebar footer network icon + tor
            # icon carry these classes; the emoji they replaced were colored
            # by the glyph itself, a symbolic icon needs explicit color).
            ".success { color: @success_color; }"
            ".warning { color: @warning_color; }"
            ".error { color: @error_color; }"
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        win = self.props.active_window
        if not win:
            win = MainWindow(application=self)
        debug_resize.install(win)
        win.present()


def main(version):
    app = GnostrApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    main(None)
