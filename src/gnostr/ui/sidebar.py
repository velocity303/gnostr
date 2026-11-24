# src/ui/sidebar.py
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk


class Sidebar(Gtk.Box):
    """Sidebar (spec relays-and-settings-v1 §C, trimmed): navigation menu +
    signer row + a compact footer status row of three same-size symbolic
    icons [tor] [network] [logout], horizontally centered (spec update
    8def2ab: the FLX1 rounded bottom bezel clips a left-aligned cluster).

    The Relay Activity expander, key-health label, Back Up Key button and
    Logout text button were REMOVED here: relay activity and key backup now
    live in Settings (RELSET-3), key-health warnings ride an AdwBanner on
    the main window (tux Q5), and logout is the footer icon + an
    Adw.AlertDialog confirmation (tux Q3)."""

    def __init__(self, main_window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.main_window = main_window

        # Header Bar
        h = Adw.HeaderBar()
        h.set_show_end_title_buttons(False)
        sb = Gtk.Button(icon_name="emblem-system-symbolic")
        sb.connect("clicked", self.on_settings_clicked)
        h.pack_end(sb)
        self.append(h)

        # Navigation Menu
        self.menu_list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.menu_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.menu_list.set_activate_on_single_click(True)
        self.menu_list.connect("row-activated", self.on_menu_selected)

        self.rows = {}
        items = [
            ("following", "My Feed", "system-users"),
            ("global", "Global Feed", "network-server"),
            ("profile", "Profile", "avatar-default"),
            ("search", "Search User", "system-search"),
            ("followers", "Follows", "network-transmit-receive"),
            ("messages", "Messages", "mail-unread"),
            ("groups", "Communities", "network-workgroup"),
            # NIPW P1 longform: opt-in Articles section (kind 30023).
            # Opening it opens the 30023 live sub; closing detaches it —
            # the kind-1 feeds are never affected (agentic-flood posture).
            ("articles", "Articles", "text-x-generic"),
        ]

        for r_id, title, icon in items:
            r = Adw.ActionRow(title=title, icon_name=f"{icon}-symbolic")
            r.set_activatable(True)
            self.menu_list.append(r)
            self.rows[r_id] = r

        # NIPW P1 C2 opt-in gate: the Articles row starts HIDDEN (Librem5
        # sense: no empty-section noise). MainWindow reveals it via
        # set_articles_visible() once the user follows at least one author
        # that has a 30023 in the local DB, or a live 30023 from a followed
        # author arrives.
        self.rows["articles"].set_visible(False)

        # Unread-DM badge on the Messages row (hidden at 0). MainWindow
        # refreshes it via update_dm_badge() off gateway queries.
        self.dm_badge = Gtk.Label(label="0")
        self.dm_badge.add_css_class("badge")
        self.dm_badge.add_css_class("accent")
        self.dm_badge.hide()
        self.rows["messages"].add_suffix(self.dm_badge)

        self.append(self.menu_list)
        self.append(Gtk.Box(vexpand=True))

        # Connected remote signer (NIP-46, P2 #15) — hidden unless a bunker
        # backend is active. Shows which signer holds the key + disconnect.
        self.signer_row = Adw.ActionRow(title="Signer", subtitle="remote signer")
        self.signer_row.set_visible(False)
        disc = Gtk.Button(label="Disconnect", valign=Gtk.Align.CENTER)
        disc.connect("clicked", self.on_disconnect_signer_clicked)
        self.signer_row.add_suffix(disc)
        self.menu_list.append(self.signer_row)

        # Footer status row: [tor icon] [network status icon] [logout icon].
        # All same-size symbolic icons; the cluster is CENTERED (spec update
        # 8def2ab) and does not reserve slots — with the tor icon hidden the
        # remaining two icons re-center rather than leaving a gap.
        self.status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.status_row.set_halign(Gtk.Align.CENTER)
        self.tor_icon = Gtk.Image.new_from_icon_name(self._tor_icon_name())
        self.tor_icon.set_visible(False)
        self.status_row.append(self.tor_icon)
        # Network status: a themed symbolic icon (was a colored emoji). State
        # is carried by CSS classes, same pattern as the tor icon — the emoji
        # glyphs rendered identically in B/W themes and could not be themed.
        self.status_icon = Gtk.Image.new_from_icon_name(
            self._net_icon_name("DISCONNECTED")
        )
        self.status_icon.add_css_class("warning")
        self.status_icon.set_tooltip_text("Relays: disconnected")
        self.status_row.append(self.status_icon)
        # Logout (tux Q3): a real button (not a bare icon), visually
        # separated from the informational status glyphs by the group
        # spacing, tooltip "Log out". Click opens the confirm dialog on
        # MainWindow — never logs out directly.
        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep.set_margin_top(6)
        sep.set_margin_bottom(6)
        self.status_row.append(sep)
        self.logout_button = Gtk.Button(icon_name="system-log-out-symbolic")
        self.logout_button.set_tooltip_text("Log out")
        self.logout_button.add_css_class("flat")
        self.logout_button.set_size_request(44, 44)
        self.logout_button.connect("clicked", self.on_logout_clicked)
        self.status_row.append(self.logout_button)
        self.append(self.status_row)

    @staticmethod
    def _tor_icon_name():
        # A.0/C2 resolve_icon pattern; last candidate is the guaranteed
        # fallback. Single resolved name across states — state is carried
        # by CSS class + tooltip (no new icon assets shipped).
        TOR_ICON_CANDIDATES = (
            "network-vpn-symbolic",
            "network-server-secure-symbolic",
            "network-server-symbolic",
        )
        try:
            from .icons import resolve_icon

            return resolve_icon(TOR_ICON_CANDIDATES)
        except Exception:
            return TOR_ICON_CANDIDATES[-1]

    @staticmethod
    def _net_icon_name(state):
        # Same resolve_icon pattern as the tor icon / FAB icon (house rule:
        # never hard-code a glyph that can be absent on Phosh). State is
        # carried by CSS class + tooltip, so one resolved name serves all.
        NET_ICON_CANDIDATES = (
            "network-transmit-receive-symbolic",
            "network-wireless-symbolic",
            "network-server-symbolic",
        )
        try:
            from .icons import resolve_icon

            return resolve_icon(NET_ICON_CANDIDATES)
        except Exception:
            return NET_ICON_CANDIDATES[-1]

    def update_tor_icon(
        self, state, proxy_enabled, onion_count, reason="ok", host="", port=0
    ):
        """Tor-state indicator (spec §A.2.3). state is the TorState value
        string; called from MainWindow on tor-state-changed and at startup."""
        from gnostr.util.tor_state import TorState

        visible = proxy_enabled or onion_count > 0
        self.tor_icon.set_visible(visible)
        if not visible:
            return
        self.tor_icon.remove_css_class("dim-label")
        self.tor_icon.remove_css_class("warning")
        if state == TorState.REACHABLE.value:
            self.tor_icon.set_tooltip_text(f"Tor proxy reachable ({host}:{port})")
        elif state == TorState.DOWN.value:
            self.tor_icon.add_css_class("warning")
            if reason == "auth":
                tip = "SOCKS proxy requires authentication (unsupported)"
            else:
                tip = "Tor proxy not reachable — onion relays are inactive"
            self.tor_icon.set_tooltip_text(tip)
        else:  # DISABLED
            self.tor_icon.add_css_class("dim-label")
            self.tor_icon.set_tooltip_text(
                "Tor proxy is off — onion relays are inactive"
            )

    def append_relay_log(self, line):
        """No-op shim (spec §C): the Relay Activity log moved to the
        Settings → Relays page (RELSET-3), which subscribes to
        `relay-log-updated` directly. main.py keeps routing the signal here
        so the sidebar class API stays stable for existing callers/tests."""
        return None

    def on_settings_clicked(self, b):
        self.main_window.on_settings_clicked()

    def on_menu_selected(self, listbox, row):
        for r_id, row_obj in self.rows.items():
            if row == row_obj:
                self.main_window.on_menu_selected(r_id)
                break

    def on_logout_clicked(self, b):
        # Confirmation lives on MainWindow (it owns the transient parent for
        # the Adw.AlertDialog); the sidebar never logs out directly.
        self.main_window.confirm_logout()

    def on_disconnect_signer_clicked(self, b):
        self.main_window.on_disconnect_signer()

    def set_signer_status(self, label):
        """Show/hide the connected-signer row (P2 #15). label=None hides."""
        if label is None:
            self.signer_row.set_visible(False)
        else:
            self.signer_row.set_subtitle(label)
            self.signer_row.set_visible(True)

    def update_status(self, state):
        """Network status icon state. `state` is the connection status string
        (CONNECTED / WARNING / DISCONNECTED / unknown). Color comes from CSS
        classes (registered by the GnostrApp CSS provider), tooltip from the
        plain-text state — the emoji this replaced could not be themed."""
        css = {"CONNECTED": "success", "WARNING": "warning"}.get(state, "error")
        self.status_icon.remove_css_class("success")
        self.status_icon.remove_css_class("warning")
        self.status_icon.remove_css_class("error")
        self.status_icon.add_css_class(css)
        self.status_icon.set_tooltip_text(f"Relays: {state.lower()}")

    def update_dm_badge(self, count):
        """Show the unread-DM count on the Messages row; hidden at 0."""
        if count > 0:
            self.dm_badge.set_text(str(count))
            self.dm_badge.show()
        else:
            self.dm_badge.hide()

    def set_articles_visible(self, visible):
        """Reveal/hide the opt-in Articles row (NIPW P1 C2 gate). Hidden
        until the user follows at least one 30023 author — MainWindow
        computes the evidence in _refresh_articles_row / live arrivals."""
        row = self.rows.get("articles")
        if row is not None:
            row.set_visible(bool(visible))
