import collections

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, Pango

import gnostr.nostr_utils as nostr_utils
from gnostr.key_manager import KeyManager


class RelayPreferencesWindow(Adw.PreferencesWindow):
    """Settings (spec relays-and-settings-v1 §B): four pages
    Account / Relays / Media / Network (tux Q4 pick). The Relays page shows
    one AdwExpanderRow per relay entry in preference order — role switches
    and per-relay details live inside the expander, the row subtitle carries
    the role summary, and the suffixes are up/down/remove at >=44px
    (tux Q1). Relay Activity is a collapsed group at the bottom of the page
    with its own horizontal scroll region (tux Q2). The last-used page is
    persisted via client.set_settings_page (AC6 / tux change 6)."""

    PAGE_IDS = ("account", "relays", "media", "network")

    def __init__(self, nostr_client, parent_window):
        super().__init__()
        self.set_transient_for(parent_window)
        self.set_modal(True)
        self.set_title("Settings")
        self.client = nostr_client
        # url -> {"row": ExpanderRow, "switches": {...}, "details": {...}}
        self.relay_rows = {}
        self._log_lines = collections.deque(maxlen=200)

        # Four pages, kept as attributes so the AC6 page persistence can
        # map page <-> id without a lookup table.
        self.account_page = Adw.PreferencesPage(
            title="Account", icon_name="avatar-default-symbolic"
        )
        self.relays_page = Adw.PreferencesPage(
            title="Relays", icon_name="network-server-symbolic"
        )
        self.media_page = Adw.PreferencesPage(
            title="Media", icon_name="image-x-generic-symbolic"
        )
        self.network_page = Adw.PreferencesPage(
            title="Network", icon_name="network-vpn-symbolic"
        )
        self.add(self.account_page)
        self.add(self.relays_page)
        self.add(self.media_page)
        self.add(self.network_page)

        self._build_account_group()
        self._build_relay_page()
        self._build_upload_group(self.media_page)
        self._build_proxy_group(self.network_page)
        self._build_tor_group(self.network_page)
        self.refresh()
        self.client.connect("relay-list-updated", self.refresh)
        self.client.connect("relay-log-updated", self.on_relay_log_line)
        self.client.connect("status-changed", self._update_details)
        self.client.connect("publish-result", self._on_publish_result)
        # tor-v1 §A.2.4: inert-row subtitles must follow the probe result.
        self.client.connect("tor-state-changed", self.refresh)

        # AC6: reopen on the last-used page; persist every page switch.
        # Object-based API (set_visible_page / notify::visible-page): it has
        # existed since Adw 1.0, while visible-page-name is newer than the
        # phone runtime and visible-page-id does not exist at all in the
        # GNOME 50 flatpak runtime — verified against both typelibs.
        last = getattr(self.client, "settings_page", None)
        if last in self.PAGE_IDS:
            self.set_visible_page(self._page_by_id(last))
        self.connect("notify::visible-page", self._on_page_changed)

    def _page_by_id(self, page_id):
        return getattr(self, page_id + "_page")

    def _page_id(self, page):
        for page_id in self.PAGE_IDS:
            if getattr(self, page_id + "_page") is page:
                return page_id
        return None

    def _on_page_changed(self, *_a):
        page_id = self._page_id(self.get_visible_page())
        if page_id in self.PAGE_IDS:
            try:
                self.client.set_settings_page(page_id)
            except Exception:
                pass

    # -- Account page (Sync + key backup moved from the sidebar) ------------

    def _build_account_group(self):
        g1 = Adw.PreferencesGroup(title="Sync")
        self.account_page.add(g1)
        r1 = Adw.ActionRow(title="Import from Profile")
        b1 = Gtk.Button(label="Import", css_classes=["suggested-action"])
        b1.connect("clicked", self.on_import)
        r1.add_suffix(b1)
        g1.add(r1)

        gk = Adw.PreferencesGroup(
            title="Key Backup",
            description="Encrypted backup of your signing key (NIP-49).",
        )
        self.account_page.add(gk)
        self.key_health_row = Adw.ActionRow(title="Key backup")
        bb = Gtk.Button(
            label="Back Up Key",
            css_classes=["suggested-action"],
            valign=Gtk.Align.CENTER,
        )
        bb.connect("clicked", self.on_backup_key_clicked)
        self.key_health_row.add_suffix(bb)
        gk.add(self.key_health_row)
        self._refresh_key_health()

    def _refresh_key_health(self):
        """Same copy as the sidebar label had (P3 #16); the sidebar trim
        (RELSET-4) moves this surface here."""
        from . import key_health

        ts = key_health.last_backup_verified()
        if ts is None:
            self.key_health_row.set_subtitle(
                "No backup verified yet — back up your key now."
            )
        else:
            self.key_health_row.set_subtitle(
                "Backup verified: %s. Re-verify if unsure."
                % key_health.format_last_backup(ts)
            )

    def on_backup_key_clicked(self, _b):
        parent = self.get_transient_for()
        if parent is not None and hasattr(parent, "on_export_key_clicked"):
            parent.on_export_key_clicked()
        self._refresh_key_health()

    # -- Relays page (spec §B.2) ---------------------------------------------

    def _build_relay_page(self):
        self.relay_group = Adw.PreferencesGroup(title="Relays")
        self.relays_page.add(self.relay_group)
        # "Add New Relay" is kept as a member and re-appended after every
        # rebuild so it always stays at the bottom (AdwPreferencesGroup has
        # no positional insert).
        r2 = Adw.ActionRow(title="Add New Relay")
        self.add_relay_row = r2
        self.entry = Gtk.Entry(placeholder_text="wss://...")
        b2 = Gtk.Button(icon_name="list-add-symbolic")
        b2.connect("clicked", self.on_add)
        r2.add_suffix(self.entry)
        r2.add_suffix(b2)
        self.relay_group.add(r2)

        # Relay Activity (tux Q2): collapsed by default, own horizontal
        # scroll region so a long monospace line can never force the page
        # itself sideways at 360px, ~200-line cap (client.relay_log is
        # already maxlen=200; the local deque mirrors it).
        g3 = Adw.PreferencesGroup()
        self.relays_page.add(g3)
        self.activity_expander = Adw.ExpanderRow(title="Relay Activity")
        # collapsed by default; the enable-switch stays hidden (Adw default:
        # show-enable-expansion is off, and it doesn't exist on the Librem 5
        # runtime's older Adw — do not reach for it).
        # WORD_CHAR wrap + max_width_chars=1 (house rule for every monospace
        # blob label — see test_export_key_dialog): a long relay line must
        # wrap inside the page instead of forcing its natural width sideways.
        # The ScrolledWindow keeps an AUTOMATIC hpolicy per tux Q2, but the
        # wrap means the page itself never scrolls horizontally.
        self.relay_log_label = Gtk.Label(
            label="No relay activity yet",
            css_classes=["monospace", "dim-label"],
            xalign=0,
            halign=Gtk.Align.START,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            max_width_chars=1,
            hexpand=True,
        )
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        sw.set_propagate_natural_height(True)
        sw.set_max_content_height(240)
        sw.set_child(self.relay_log_label)
        self.activity_expander.add_row(sw)
        g3.add(self.activity_expander)
        for line in list(getattr(self.client, "relay_log", []) or [])[-200:]:
            self._log_lines.appendleft(line)
        self._render_log()

    def on_relay_log_line(self, _client, line):
        self._log_lines.appendleft(str(line))
        self._render_log()

    def _render_log(self):
        self.relay_log_label.set_text(
            "\n".join(self._log_lines) or "No relay activity yet"
        )

    @staticmethod
    def _role_summary(roles):
        """Glanceable role text for the row subtitle (tux Q1):
        'Read · Write', 'Read only', 'Write · DM'."""
        labels = {"read": "Read", "write": "Write", "dm": "DM"}
        names = [labels[r] for r in (roles or []) if r in labels]
        if not names:
            return "No roles"
        if names == ["Read"]:
            return "Read only"
        return " · ".join(names)

    def refresh(self, *a):
        from gnostr import proxy_settings as ps
        from gnostr.util.tor_state import TorState

        # Rebuild preserves open expanders: a role toggle re-emits
        # relay-list-updated and must not snap the expander shut mid-edit.
        open_urls = {
            u
            for u, w in self.relay_rows.items()
            if getattr(w["row"], "get_expanded", lambda: False)()
        }
        for w in self.relay_rows.values():
            self.relay_group.remove(w["row"])
        self.relay_rows.clear()
        # lift the Add row out; it goes back last
        self.relay_group.remove(self.add_relay_row)
        entries = getattr(self.client, "relay_entries", None) or []
        proxy_enabled = ps.proxy_get("proxy-enabled")
        tor_reachable = getattr(self.client, "tor_state", None) == TorState.REACHABLE
        items = entries or [
            {"url": u, "roles": [], "enabled": True}
            for u in sorted(list(self.client.relay_urls))
        ]
        for entry in items:
            url = entry["url"]
            row = self._build_relay_row(url, entry, proxy_enabled, tor_reachable)
            if url in open_urls:
                row.set_expanded(True)
            self.relay_group.add(row)
        self.relay_group.add(self.add_relay_row)

    def _build_relay_row(self, url, entry, proxy_enabled, tor_reachable):
        from gnostr.util.tor_state import host_is_onion

        row = Adw.ExpanderRow(title=url)
        subtitle = self._role_summary(entry.get("roles"))
        if not entry.get("enabled", True):
            subtitle = "Parked — " + subtitle.lower()
        row.set_subtitle(subtitle)

        widgets = {"switches": {}, "details": {}}

        # Park/unpark switch (spec §A enabled flag; the only UI for it).
        en = Adw.SwitchRow(
            title="Enabled", subtitle="Off parks the relay: kept, never connected"
        )
        en.set_active(bool(entry.get("enabled", True)))
        # AdwSwitchRow has no "toggled" (that's GtkSwitch); the house signal
        # for its state is notify::active.
        en.connect("notify::active", self._on_enabled_toggled, url)
        row.add_row(en)
        widgets["switches"]["enabled"] = en

        # Role switches inside the expander (tux Q1): full-width rows, real
        # labels, >=48px targets — no cryptic R/W/DM chips, no tooltips.
        for role, title in (("read", "Read"), ("write", "Write"), ("dm", "DM")):
            sw = Adw.SwitchRow(title=title)
            sw.set_active(role in (entry.get("roles") or []))
            sw.connect("notify::active", self._on_role_toggled, url)
            row.add_row(sw)
            widgets["switches"][role] = sw

        # Details: connection state / last error / last publish (spec §B.2).
        for key, title in (
            ("conn", "Connection"),
            ("err", "Last error"),
            ("pub", "Last publish"),
        ):
            d = Adw.ActionRow(title=title)
            d.set_sensitive(False)
            row.add_row(d)
            widgets["details"][key] = d
        self.relay_rows[url] = {**widgets, "row": row}

        # tor-v1 §A.2.4: an inert onion relay says why it is idle.
        if host_is_onion(url) and not (proxy_enabled and tor_reachable):
            widgets["details"]["conn"].set_subtitle(
                "Inactive — Tor not connected"
                if proxy_enabled
                else "Inactive — enable Tor proxy in Settings"
            )
        else:
            self._fill_details(url)

        # Suffixes: up / down / remove only (tux change 1), >=44px targets.
        up = Gtk.Button(icon_name="pan-up-symbolic", valign=Gtk.Align.CENTER)
        up.set_size_request(44, 44)
        up.connect("clicked", lambda b, u=url: self.client.move_relay_entry(u, -1))
        row.add_suffix(up)
        down = Gtk.Button(icon_name="pan-down-symbolic", valign=Gtk.Align.CENTER)
        down.set_size_request(44, 44)
        down.connect("clicked", lambda b, u=url: self.client.move_relay_entry(u, 1))
        row.add_suffix(down)
        rm = Gtk.Button(
            icon_name="user-trash-symbolic",
            css_classes=["destructor"],
            valign=Gtk.Align.CENTER,
        )
        rm.set_size_request(44, 44)
        rm.connect("clicked", lambda b, u=url: self.client.remove_relay_entry(u))
        row.add_suffix(rm)
        return row

    def _on_role_toggled(self, _switch, _pspec, url):
        w = self.relay_rows.get(url)
        if w is None:
            return
        roles = [r for r in ("read", "write", "dm") if w["switches"][r].get_active()]
        self.client.set_relay_roles(url, roles)

    def _on_enabled_toggled(self, switch, _pspec, url):
        self.client.set_relay_enabled(url, switch.get_active())

    def _on_publish_result(self, *_a):
        self._update_details()

    def _update_details(self, *_a):
        for url in list(self.relay_rows):
            self._fill_details(url)

    def _fill_details(self, url):
        w = self.relay_rows.get(url)
        if w is None:
            return
        status = getattr(self.client, "relay_status", {}).get(url)
        err = getattr(self.client, "relay_last_error", {}).get(url)
        pub = getattr(self.client, "relay_last_publish", {}).get(url)
        w["details"]["conn"].set_subtitle(
            status.details if status is not None else "Unknown"
        )
        w["details"]["err"].set_subtitle(err or "None")
        w["details"]["pub"].set_subtitle(pub or "None")

    # -- Media Upload settings (rich editor scope) ---------------------------
    # The upload backend/server are GSettings-backed so users can move to
    # another server when capacity or funding changes. Defaults live in
    # uploader.py; every read falls back so a missing schema never crashes
    # the dialog.

    _BACKENDS = [
        ("blossom", "Blossom (recommended)"),
        ("nostr.build", "nostr.build (legacy)"),
    ]

    def _build_upload_group(self, page):
        from . import uploader

        g = Adw.PreferencesGroup(
            title="Media Upload",
            description="Where attached images and videos are hosted.",
        )
        page.add(g)

        # Gtk.StringList, not Adw.StringList — the latter is libadwaita 1.6+
        # and absent from the phone runtime (verified on device).
        self.backend_row = Adw.ComboRow(
            title="Backend",
            model=Gtk.StringList(strings=[label for _, label in self._BACKENDS]),
        )
        cur_backend = uploader.settings_get("upload-backend", uploader.DEFAULT_BACKEND)
        names = [name for name, _ in self._BACKENDS]
        self.backend_row.set_selected(
            max(0, names.index(cur_backend) if cur_backend in names else 0)
        )
        g.add(self.backend_row)

        self.server_row = Adw.EntryRow(title="Server URL")
        self.server_row.set_text(
            uploader.settings_get("upload-server-url", uploader.DEFAULT_SERVER_URL)
        )
        g.add(self.server_row)

        self.maxmb_row = Adw.SpinRow.new_with_range(0, 200, 1)
        self.maxmb_row.set_title("Max file size (MB)")
        self.maxmb_row.set_value(
            float(uploader.settings_get("upload-max-mb", uploader.DEFAULT_MAX_MB))
        )
        g.add(self.maxmb_row)

        save = Adw.ActionRow(title="Save upload settings")
        btn = Gtk.Button(label="Save", css_classes=["suggested-action"])
        btn.connect("clicked", self.on_save_upload_settings)
        save.add_suffix(btn)
        g.add(save)

    # -- Tor help (tor-v1-docs) -----------------------------------------------
    # v1 tor support is "opt-in SOCKS5 proxy + docs": gnostr never starts or
    # manages the tor daemon. This group is the in-app entry point to the full
    # setup guide (docs/tor-setup.md, installed beside the app modules).

    def _build_tor_group(self, page):
        g = Adw.PreferencesGroup(
            title="Tor & Privacy",
            description="Route relays, LNURL, and media through a local tor SOCKS5 proxy.",
        )
        page.add(g)
        row = Adw.ActionRow(
            title="Tor setup guide",
            subtitle="Install tor, minimal torrc, verification, Librem 5 notes",
        )
        btn = Gtk.Button(icon_name="help-contents-symbolic", valign=Gtk.Align.CENTER)
        btn.set_tooltip_text("Open the Tor setup guide")
        btn.connect("clicked", self.on_open_tor_docs)
        row.add_suffix(btn)
        g.add(row)

    def on_open_tor_docs(self, _btn):
        import os
        from pathlib import Path

        here = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(here, "docs", "tor-setup.md"),  # installed layout
            os.path.normpath(os.path.join(here, "..", "..", "docs", "tor-setup.md")),
        ]
        for path in candidates:
            if os.path.exists(path):
                try:
                    Gtk.UriLauncher(uri=Path(path).as_uri()).launch(self, None, None)
                    return
                except Exception:
                    break
        self.add_toast(Adw.Toast(title="Tor setup guide not found"))

    def on_save_upload_settings(self, _btn):
        from . import uploader

        names = [name for name, _ in self._BACKENDS]
        backend = names[min(self.backend_row.get_selected(), len(names) - 1)]
        url = self.server_row.get_text().strip()
        if backend == "blossom" and not url.startswith("https://"):
            self.add_toast(Adw.Toast(title="Server URL must start with https://"))
            return
        ok = uploader.settings_set("upload-backend", backend)
        ok = uploader.settings_set("upload-server-url", url) and ok
        ok = (
            uploader.settings_set("upload-max-mb", int(self.maxmb_row.get_value()))
            and ok
        )
        self.add_toast(
            Adw.Toast(title="Upload settings saved")
            if ok
            else Adw.Toast(title="Could not save (settings schema unavailable)")
        )

    # -- Proxy settings (spec gnostr-tor-onion-support-v1 §4) ----------------
    # Opt-in SOCKS5 proxy (tor daemon on the user's machine). The endpoint is
    # not sensitive — no secrets here; keys live in proxy_settings.py and the
    # same GSettings hard-fallback pattern as Media Upload above.

    def _build_proxy_group(self, page):
        from . import proxy_settings as ps

        g = Adw.PreferencesGroup(
            title="Proxy",
            description="Route relays, lookups and media through a SOCKS5 proxy (e.g. tor).",
        )
        page.add(g)

        self.proxy_enabled_row = Adw.SwitchRow(
            title="Enable SOCKS5 proxy",
            subtitle="Off = direct connections (default)",
        )
        self.proxy_enabled_row.set_active(ps.proxy_get("proxy-enabled"))
        g.add(self.proxy_enabled_row)

        self.proxy_host_row = Adw.EntryRow(title="Proxy host")
        self.proxy_host_row.set_text(ps.proxy_get("proxy-host"))
        g.add(self.proxy_host_row)

        self.proxy_port_row = Adw.SpinRow.new_with_range(1, 65535, 1)
        self.proxy_port_row.set_title("Proxy port")
        self.proxy_port_row.set_value(float(ps.proxy_get("proxy-port")))
        g.add(self.proxy_port_row)

        # Keep this on with .onion endpoints: the proxy resolves hostnames
        # (socks5h), so nothing leaks to a local DNS resolver.
        self.proxy_dns_row = Adw.SwitchRow(
            title="Resolve hostnames through proxy",
            subtitle="Required for .onion relays",
        )
        self.proxy_dns_row.set_active(ps.proxy_get("proxy-remote-dns"))
        g.add(self.proxy_dns_row)

        save = Adw.ActionRow(title="Save proxy settings")
        btn = Gtk.Button(label="Save", css_classes=["suggested-action"])
        btn.connect("clicked", self.on_save_proxy_settings)
        save.add_suffix(btn)
        g.add(save)

    def on_save_proxy_settings(self, _btn):
        from . import proxy_settings as ps

        host = self.proxy_host_row.get_text().strip()
        if not host:
            self.add_toast(Adw.Toast(title="Proxy host must not be empty"))
            return
        ok = ps.proxy_set("proxy-enabled", self.proxy_enabled_row.get_active())
        ok = ps.proxy_set("proxy-host", host) and ok
        ok = ps.proxy_set("proxy-port", int(self.proxy_port_row.get_value())) and ok
        ok = ps.proxy_set("proxy-remote-dns", self.proxy_dns_row.get_active()) and ok
        self.add_toast(
            Adw.Toast(title="Proxy settings saved")
            if ok
            else Adw.Toast(title="Could not save (settings schema unavailable)")
        )

    def add_toast(self, toast):
        parent = self.get_transient_for()
        overlay = getattr(parent, "toast_overlay", None) if parent else None
        if overlay is not None:
            overlay.add_toast(toast)

    def on_import(self, b):
        self.client.fetch_user_relays()
        self.add_toast(Adw.Toast(title="Requesting Relay List..."))

    def on_add(self, b):
        u = self.entry.get_text().strip()
        if u.startswith("ws"):
            # Scheme policy (spec gnostr-tor-onion-support-v1 §3): ws:// is
            # only legal for .onion hosts behind the SOCKS proxy. Reject at
            # the dialog with a visible toast — never rewrite the URL, never
            # silently fall back to a clearnet equivalent.
            from . import proxy_settings as ps

            if not ps.url_allowed(u, ps.proxy_get("proxy-enabled")):
                self.add_toast(
                    Adw.Toast(
                        title=(
                            f"{u} refused: ws:// is only allowed for "
                            ".onion relays with the proxy enabled"
                        ),
                        timeout=5,
                    )
                )
                return
            self.client.add_relay(u)
            self.entry.set_text("")


def resolve_login_input(text, password):
    """Map any accepted login secret (hex | nsec | ncryptsec) to a hex key.

    Raises ValueError for ncryptsec with wrong password / corruption so the
    dialog can show it. This is the single decision point the GTK layer
    calls; unit-tested without widgets (tests/test_onboarding_keys.py).
    """
    text = text.strip()
    if text.startswith("ncryptsec"):
        from gnostr.nip49 import nip49_decrypt

        return nip49_decrypt(text, password)
    if text.startswith("nsec"):
        return nostr_utils.nsec_to_hex(text)
    return text


class LoginDialog(Adw.Window):
    def __init__(self, client, parent):
        super().__init__()
        self.client = client
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(450, 400)
        self.set_title("Login")
        c = Adw.ToolbarView()
        self.set_content(c)
        c.add_top_bar(Adw.HeaderBar())
        p = Adw.PreferencesPage()
        c.set_content(p)
        g = Adw.PreferencesGroup(title="Credentials")
        p.add(g)
        self.ent = Adw.PasswordEntryRow(title="Private Key")
        g.add(self.ent)
        self.pw_ent = Adw.PasswordEntryRow(title="Password (ncryptsec only)")
        g.add(self.pw_ent)
        bg = Adw.PreferencesGroup()
        p.add(bg)
        self.error_label = Gtk.Label(label="", css_classes=["error-label"])
        bg.add(self.error_label)
        b = Gtk.Button(label="Login", css_classes=["pill", "suggested-action"])
        b.connect("clicked", self.on_login)
        bg.add(b)

    def on_login(self, b):
        try:
            h = resolve_login_input(self.ent.get_text(), self.pw_ent.get_text())
        except ValueError:
            self.error_label.set_text(
                "Could not decrypt — wrong password or corrupt key."
            )
            return
        if h and nostr_utils.get_public_key(h):
            # A failed keyring save must NOT silently log in — the session
            # would work once and evaporate on restart (Librem 5 field bug:
            # "user interaction failed" from gnome-keyring was ignored).
            if not KeyManager.save_key(h):
                self.error_label.set_text("Keyring save failed — key not persisted.")
                return
            self.client.set_keys(nostr_utils.get_public_key(h), h)
            self.client.fetch_user_relays()
            self.client.fetch_contacts()
            p = self.get_transient_for()
            if p and hasattr(p, "perform_login"):
                p.perform_login(h)
            self.close()


class ExportKeyDialog(Adw.Window):
    """Produce an NIP-49 ncryptsec backup of the keyring-stored key (P1 #14).

    Closes the loop for users who logged in before the wizard existed: the
    at-rest libsecret key is wrapped with a user password and shown for
    copying. The plaintext key is never written to disk here."""

    def __init__(self, parent):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_title("Back Up Key (Encrypted)")
        self.set_default_size(450, 360)
        tb = Adw.ToolbarView()
        self.set_content(tb)
        tb.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        # Scrollable: on a portrait phone the wrapped blob + buttons exceed
        # the screen height; without this the Copy button is unreachable.
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(page)
        tb.set_content(scroll)
        g = Adw.PreferencesGroup(title="Choose a backup password")
        page.add(g)
        self.pw1 = Adw.PasswordEntryRow(title="Password")
        self.pw2 = Adw.PasswordEntryRow(title="Repeat password")
        g.add(self.pw1)
        g.add(self.pw2)
        self.err = Gtk.Label(label="", css_classes=["error-label"])
        g.add(self.err)
        g2 = Adw.PreferencesGroup()
        page.add(g2)
        b = Gtk.Button(label="Encrypt", css_classes=["pill", "suggested-action"])
        b.connect("clicked", self.on_encrypt)
        g2.add(b)
        self.copy_btn = Gtk.Button(label="Copy to clipboard", visible=False)
        self.copy_btn.connect("clicked", self.on_copy)
        g2.add(self.copy_btn)
        # WORD_CHAR: the ncryptsec blob is one long token; WORD wrap would
        # never break it and the row overflows a portrait phone screen.
        self.result_label = Gtk.Label(
            label="",
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            selectable=True,
            css_classes=["monospace"],
            xalign=0,
        )
        g2.add(self.result_label)

    def on_copy(self, btn):
        from gi.repository import Gdk

        display = Gdk.Display.get_default()
        if display:
            # Must use the display's default clipboard; constructing
            # Gdk.Clipboard(display) makes a fresh one nothing pastes from.
            # Gdk.Clipboard has NO set_text() in GTK4 (that was GTK3's
            # Gtk.Clipboard) — set_text raised AttributeError and the copy
            # silently did nothing. set_content is the GTK4 API (issue #33).
            display.get_clipboard().set_content(
                Gdk.ContentProvider.new_for_value(self.result_label.get_text())
            )
        btn.set_label("Copied!")

    def on_encrypt(self, btn):
        p1 = self.pw1.get_text()
        if len(p1) < 8 or p1 != self.pw2.get_text():
            self.err.set_text("Use at least 8 characters, entered identically.")
            return
        nsec_hex = KeyManager.load_key()
        if not nsec_hex:
            self.err.set_text("No key in the keyring.")
            return
        from gnostr.nip49 import nip49_encrypt

        self.result_label.set_text(nip49_encrypt(nsec_hex, p1))
        self.copy_btn.set_visible(True)
        # Key-health (P3 #16): producing the backup artifact counts as a
        # verified backup — Settings shows this timestamp.
        from gnostr import key_health

        key_health.mark_backup_verified()


class ComposeWindow(Adw.Window):
    def __init__(self, parent, on_post_callback):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(500, 300)
        self.set_title("New Post")

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        self.set_content(box)

        # Header bar for the window
        hb = Adw.HeaderBar()
        box.append(hb)

        # Rich composer: markdown-lite marker bar (bold/italic/code/link) +
        # media attach (imeta), scope: editor-gtk-scope (rich editor spec)
        from gnostr.ui.compose_box import RichComposeBox

        parent_window = self.get_transient_for()
        sign_fn = None
        client = getattr(parent_window, "client", None) if parent_window else None
        if client is not None and hasattr(client, "_sign"):
            sign_fn = client._sign
        self.compose = RichComposeBox(placeholder="What's happening?", sign_fn=sign_fn)
        self.text_view = self.compose.text_view
        box.append(self.compose)

        # Footer button
        btn_post = Gtk.Button(label="Post", css_classes=["pill", "suggested-action"])
        btn_post.set_halign(Gtk.Align.END)
        btn_post.connect("clicked", self.on_post_clicked)
        box.append(btn_post)

        self.on_post_callback = on_post_callback
        self.compose.grab_focus()

    def on_post_clicked(self, b):
        text = self.compose.get_text()
        if text.strip():
            self.on_post_callback(text, list(self.compose.media_tags))
            self.close()


class EditProfileDialog(Adw.Window):
    """Edit + publish the user's own kind-0 profile metadata (NIP-01/NIP-24).

    Fields: name, display_name, about, picture, banner, website, nip05, lud16.
    On save, builds a metadata dict and calls client.publish_profile(), which
    signs + publishes the kind-0 event and persists locally."""

    def __init__(self, client, parent, profile=None):
        super().__init__()
        self.client = client
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(520, 620)
        self.set_title("Edit Profile")
        prof = profile or {}

        c = Adw.ToolbarView()
        self.set_content(c)
        c.add_top_bar(Adw.HeaderBar())
        p = Adw.PreferencesPage()
        c.set_content(p)

        g = Adw.PreferencesGroup(title="Identity")
        p.add(g)
        self.ent_name = self._entry_row(g, "Name", prof.get("name", ""))
        self.ent_display = self._entry_row(
            g, "Display Name", prof.get("display_name", "")
        )
        self.ent_about = self._entry_row(g, "About / Bio", prof.get("about", ""))

        g2 = Adw.PreferencesGroup(title="Media")
        p.add(g2)
        self.ent_picture = self._entry_row(
            g2, "Profile Picture URL", prof.get("picture", "")
        )
        self.ent_banner = self._entry_row(g2, "Banner URL", prof.get("banner", ""))

        g3 = Adw.PreferencesGroup(title="Links & Verification")
        p.add(g3)
        self.ent_website = self._entry_row(g3, "Website", prof.get("website", ""))
        self.ent_nip05 = self._entry_row(
            g3, "NIP-05 Identifier (user@domain)", prof.get("nip05", "")
        )
        self.ent_lud16 = self._entry_row(
            g3, "Lightning Address (user@domain)", prof.get("lud16", "")
        )

        bg = Adw.PreferencesGroup()
        p.add(bg)
        b = Gtk.Button(label="Save Profile", css_classes=["pill", "suggested-action"])
        b.connect("clicked", self.on_save)
        bg.add(b)

    def _entry_row(self, group, title, value):
        row = Adw.ActionRow(title=title)
        entry = Gtk.Entry(text=value)
        entry.set_hexpand(True)
        row.add_suffix(entry)
        group.add(row)
        return entry

    def on_save(self, b):
        metadata = {
            "name": self.ent_name.get_text().strip(),
            "display_name": self.ent_display.get_text().strip(),
            "about": self.ent_about.get_text().strip(),
            "picture": self.ent_picture.get_text().strip(),
            "banner": self.ent_banner.get_text().strip(),
            "website": self.ent_website.get_text().strip(),
            "nip05": self.ent_nip05.get_text().strip(),
            "lud16": self.ent_lud16.get_text().strip(),
        }
        # Drop empty fields so we don't publish blank metadata
        metadata = {k: v for k, v in metadata.items() if v}
        if self.client.publish_profile(metadata):
            self.close()
        else:
            self.add_toast(Adw.Toast(title="No private key loaded — cannot publish"))


class SearchDialog(Adw.Window):
    """Search for a user by pasting an npub/nprofile/nsec/hex/nostr: identifier."""

    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.set_transient_for(main_window)
        self.set_modal(True)
        self.set_default_size(480, 200)
        self.set_title("Search User")

        c = Adw.ToolbarView()
        self.set_content(c)
        c.add_top_bar(Adw.HeaderBar())

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        c.set_content(box)

        lbl = Gtk.Label(
            label="Paste an npub, nprofile, nsec, raw hex pubkey, or nostr: link",
            xalign=0,
            css_classes=["caption", "dim-label"],
        )
        box.append(lbl)

        self.entry = Gtk.Entry(placeholder_text="npub1... / nprofile1... / hex")
        self.entry.set_hexpand(True)
        self.entry.connect("activate", self.on_search)
        box.append(self.entry)

        btn = Gtk.Button(label="Search", css_classes=["pill", "suggested-action"])
        btn.set_halign(Gtk.Align.END)
        btn.connect("clicked", self.on_search)
        box.append(btn)

    def on_search(self, *a):
        from gnostr.nostr_utils import resolve_profile_identifier

        text = self.entry.get_text().strip()
        pubkey = resolve_profile_identifier(text)
        if not pubkey:
            self.add_toast(Adw.Toast(title="Not a valid Nostr identifier"))
            return
        self.main_window.show_profile(pubkey)
        self.close()


try:  # QR rendering for the bunker connect dialog (pure-python wheel)
    import segno
except ImportError:  # pragma: no cover — flatpak ships segno; degraded UX only
    segno = None


class ConnectSignerWindow(Adw.Window):
    """NIP-46 'log in with a bunker' connect dialog (spec P2, issue #15).

    Paste a bunker:// URI (or scan the QR with the signer app — the QR encodes
    the SAME URI for signer apps that scan rather than paste). The handshake
    runs on a worker thread so the UI never blocks on the relay round-trip;
    on success the session is saved to libsecret and MainWindow switches to
    the app view through the bunker backend.
    """

    def __init__(self, client, main_window):
        super().__init__()
        self.client = client
        self.main_window = main_window
        self._busy = False
        self.set_title("Connect Signer")
        self.set_default_size(420, 560)
        self.set_transient_for(main_window)
        self.set_modal(True)

        toolbar = Adw.ToolbarView()
        self.set_content(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())

        page = Adw.PreferencesPage()
        toolbar.set_content(page)

        g = Adw.PreferencesGroup(
            title="Bunker URI",
            description="Paste the bunker:// URI from your remote signer (Amber, nsec.app, …)",
        )
        page.add(g)
        self.ent = Adw.EntryRow(title="bunker://…")
        self.ent.connect("changed", self._on_uri_changed)
        g.add(self.ent)

        # QR of the URI so a phone signer can scan it instead of typing.
        self.qr_area = Gtk.DrawingArea()
        self.qr_area.set_size_request(220, 220)
        self.qr_area.set_visible(False)
        self.qr_area.set_halign(Gtk.Align.CENTER)
        self.qr_area.connect("draw", self._draw_qr)
        self._qr_matrix = None
        qr_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        qr_box.append(self.qr_area)
        if segno is None:
            qr_box.append(
                Gtk.Label(
                    label="QR unavailable (segno not installed)",
                    css_classes=["dim-label"],
                )
            )
        g.add(qr_box)

        bg = Adw.PreferencesGroup()
        page.add(bg)
        self.error_label = Gtk.Label(label="", css_classes=["error-label"], wrap=True)
        bg.add(self.error_label)
        self.connect_btn = Gtk.Button(
            label="Connect", css_classes=["pill", "suggested-action"]
        )
        self.connect_btn.connect("clicked", self.on_connect)
        bg.add(self.connect_btn)

    def _on_uri_changed(self, *_):
        self._qr_matrix = None
        self.qr_area.set_visible(False)
        if segno is None:
            return
        try:
            qr = segno.make(self.ent.get_text().strip(), error="M")
            matrix = qr.matrix() if callable(qr.matrix) else qr.matrix
        except Exception:
            matrix = None
        if matrix:
            self._qr_matrix = matrix
            self.qr_area.set_visible(True)
            self.qr_area.queue_draw()

    def _draw_qr(self, _area, cr, width, height):
        """Render the segno boolean matrix with the cairo context GTK hands us
        (no cairo import needed — only the passed context is used)."""
        m = self._qr_matrix
        if not m:
            return False
        n = len(m)
        side = min(width, height)
        cell = max(1, side // (n + 2))  # quiet zone of 1 cell
        ox = (width - cell * n) // 2
        oy = (height - cell * n) // 2
        cr.set_source_rgb(1, 1, 1)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        cr.set_source_rgb(0, 0, 0)
        for y, row in enumerate(m):
            for x, dark in enumerate(row):
                if dark:
                    cr.rectangle(ox + x * cell, oy + y * cell, cell, cell)
        cr.fill()
        return False

    def on_connect(self, _btn):
        if self._busy:
            return
        uri = self.ent.get_text().strip()
        self._busy = True
        self.connect_btn.set_sensitive(False)
        self.error_label.set_text("")

        def _worker():
            from gnostr.signer import new_bunker_backend, session_blob

            try:
                backend = new_bunker_backend(
                    self.client,
                    uri,
                    on_sign_request=self.main_window.confirm_sign_request,
                )
            except Exception as e:
                from gi.repository import GLib

                GLib.idle_add(self._fail, str(e))
                return
            from gnostr.key_manager import BunkerSessionManager

            ok = BunkerSessionManager.save_session(session_blob(backend))
            from gi.repository import GLib

            GLib.idle_add(self._succeed, backend, ok)

        import threading

        threading.Thread(target=_worker, daemon=True).start()

    def _fail(self, msg):
        self._busy = False
        self.connect_btn.set_sensitive(True)
        self.error_label.set_text(f"Connect failed: {msg}")
        return False

    def _succeed(self, backend, saved):
        self._busy = False
        self.main_window.perform_bunker_login(backend, session_saved=saved)
        self.close()
        return False
