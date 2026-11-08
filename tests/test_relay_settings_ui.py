"""RELSET-3: Settings restructure (spec relays-and-settings-v1 §B, card
RELSET-3).

Tier-1: the gi mocks make RelayPreferencesWindow unconstructable, so the
page structure and relay-row wiring are read off the AST of dialogs.py
(house pattern, same as test_proxy_settings.py), and the client-side
additions (settings_page persistence, per-relay status tracking, reorder
re-publish) are ast-extracted onto a FakeClient (same harness as
test_relay_roles_client.py).

Covers the acceptance items owned by this card:
- four pages Account / Relays / Media / Network (tux Q4 pick)
- role switches live INSIDE the per-relay expander; row suffixes are
  up/down/remove only (tux Q1)
- reorder buttons call client.move_relay_entry with -1/+1
- role toggle handler persists via client.set_relay_roles
- Relay Activity: collapsed expander at the bottom of the Relays page,
  200-line cap, fed by relay-log-updated
- key backup section (key-health row + Back Up Key) on the Account page
- last-used page persists to config.json and reloads (AC6)
- move_relay_entry re-publishes kind-10002 (spec §B: every mutation)
- per-relay details state (relay_status / relay_last_error /
  relay_last_publish) is maintained by _handle_status / _handle_ok
"""

import ast
import collections
import json
import os
import tempfile
import types
from unittest.mock import MagicMock

import gnostr  # real package; carries the relay_entries attribute

_SRC = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)
_DIALOGS = os.path.join(_SRC, "gnostr", "dialogs.py")
_CLIENT_PATH = os.path.join(_SRC, "gnostr", "client.py")


# ---------------------------------------------------------------------------
# dialogs.py AST helpers
# ---------------------------------------------------------------------------


def _class_node(name="RelayPreferencesWindow"):
    tree = ast.parse(open(_DIALOGS, encoding="utf-8").read())
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)


def _method(name, cls=None):
    cls = cls or _class_node()
    return next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name
    )


# -- page structure (tux Q4: Account / Relays / Media / Network) -------------


def test_four_pages_in_order():
    src = ast.unparse(_method("__init__"))
    # ast.unparse normalizes string quotes to single
    for title in ("Account", "Relays", "Media", "Network"):
        assert f"title='{title}'" in src, f"missing page: {title}"
    order = [
        src.index(f"title='{t}'") for t in ("Account", "Relays", "Media", "Network")
    ]
    assert order == sorted(order), "pages must be added in Account..Network order"


def test_page_ids_match_contract():
    cls = _class_node()
    ids = next(
        n
        for n in cls.body
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "PAGE_IDS"
    )
    got = tuple(e.value for e in ids.value.elts)
    # zaps-payments-v1 §3.2 LOCKS a dedicated Wallet page between Media and
    # Network (spec line: "Wallet UI surface: LOCKED").
    assert got == ("account", "relays", "media", "wallet", "network")


def test_groups_live_on_the_right_pages():
    src = open(_DIALOGS).read()
    assert "self._build_upload_group(self.media_page)" in src
    assert "self._build_wallet_group(self.wallet_page)" in src
    assert "self._build_proxy_group(self.network_page)" in src
    assert "self._build_tor_group(self.network_page)" in src


def test_account_page_has_sync_and_key_backup():
    body = ast.dump(_method("_build_account_group"))
    assert "Import from Profile" in body
    assert "Key Backup" in body
    assert "Back Up Key" in body
    assert "_refresh_key_health" in body


# -- relay rows (tux Q1: switches in expander, suffixes up/down/remove) ------


def test_relay_row_is_expander_with_role_switch_rows():
    body = ast.dump(_method("_build_relay_row"))
    assert "ExpanderRow" in body, "per-relay row must be an AdwExpanderRow"
    assert "SwitchRow" in body, "role editing must use AdwSwitchRow"
    for role in ("read", "write", "dm"):
        assert f"'{role}'" in body, f"role {role} must get a switch"
    # tux Q1: no R/W/DM toggle chips
    assert "ToggleButton" not in body


def test_relay_row_suffixes_are_up_down_remove_only():
    body = ast.unparse(_method("_build_relay_row"))
    assert "pan-up-symbolic" in body
    assert "pan-down-symbolic" in body
    assert "user-trash-symbolic" in body
    # the ONLY suffixes are the three buttons (role switches live inside)
    assert body.count("row.add_suffix") == 3


def test_reorder_wiring_calls_move_relay_entry():
    body = ast.dump(_method("_build_relay_row"))
    assert "move_relay_entry" in body
    # up = -1, down = +1
    assert "move_relay_entry(u, -1)" in ast.unparse(_method("_build_relay_row"))
    assert "move_relay_entry(u, 1)" in ast.unparse(_method("_build_relay_row"))


def test_role_toggle_persists_via_set_relay_roles():
    body = ast.unparse(_method("_on_role_toggled"))
    assert "set_relay_roles" in body
    for role in ("read", "write", "dm"):
        assert f"'{role}'" in body


def test_enabled_toggle_persists_via_set_relay_enabled():
    body = ast.unparse(_method("_on_enabled_toggled"))
    assert "set_relay_enabled" in body


def test_remove_wiring_uses_entry_api():
    body = ast.unparse(_method("_build_relay_row"))
    assert "remove_relay_entry" in body


# -- Relay Activity (tux Q2) --------------------------------------------------


def test_activity_log_collapsed_own_scroll_capped():
    body = ast.unparse(_method("_build_relay_page"))
    assert "Relay Activity" in body
    # no enable-switch on the expander (and no API drift on the phone's
    # older Adw: show-enable-expansion must not be touched)
    assert "show_enable" not in body
    assert "enable_switch" not in body
    # 200-line cap: the local deque mirrors client.relay_log (maxlen=200)
    init = ast.unparse(_method("__init__"))
    assert "maxlen=200" in init
    # own horizontal scroll region: a ScrolledWindow with AUTOMATIC hpolicy
    assert "ScrolledWindow" in body
    assert "AUTOMATIC" in body
    # fed by relay-log-updated
    assert "relay-log-updated" in init


def test_activity_log_prefills_from_client_relay_log():
    body = ast.unparse(_method("_build_relay_page"))
    assert "relay_log" in body, "log must prefill from client.relay_log"


# -- last-used page (AC6 / tux change 6) -------------------------------------


def test_page_switch_persists_and_reloads():
    init = ast.unparse(_method("__init__"))
    # Object-based page API: set_visible_page exists in every Adw the app
    # targets (phone 4.22 runtime and GNOME 50 flatpak); the -id variant
    # does not exist at all — verified against both typelibs.
    assert "set_visible_page(" in init
    assert "notify::visible-page" in init
    assert "settings_page" in init
    handler = ast.unparse(_method("_on_page_changed"))
    assert "set_settings_page" in handler


# ---------------------------------------------------------------------------
# client.py additions on a FakeClient (house ast-extraction harness)
# ---------------------------------------------------------------------------


def _client_globals():
    src = open(_CLIENT_PATH, encoding="utf-8").read()
    ns = {"__name__": "gnostr.client", "__package__": "gnostr"}
    exec(compile(ast.parse(src), _CLIENT_PATH, "exec"), ns)
    ns["GLib"] = types.SimpleNamespace(
        idle_add=lambda fn, *a: fn(*a),
        timeout_add=lambda ms, fn, *a: None,
        timeout_add_seconds=lambda s, fn, *a: 1,
        source_remove=lambda t: None,
        get_user_config_dir=lambda: "/tmp/gnostr-test",
    )
    return ns


_NS = _client_globals()


def _unbound_client(method_name):
    src = open(_CLIENT_PATH, encoding="utf-8").read()
    cls = next(
        n
        for n in ast.parse(src).body
        if isinstance(n, ast.ClassDef) and n.name == "NostrClient"
    )
    fns = [
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name
    ]
    if not fns:
        raise StopIteration(method_name)
    ns = dict(_NS)
    exec(compile(ast.Module(body=list(fns), type_ignores=[]), _CLIENT_PATH, "exec"), ns)
    return ns[method_name]


class FakeClient:
    """Minimal self for the RELSET-3 client additions."""

    def __init__(self, entries=None):
        self.db = MagicMock()
        self.config_file = os.path.join(
            tempfile.mkdtemp(prefix="gnostr-relset3-"), "config.json"
        )
        self.emitted = []
        self.log = []
        self.active_relays = {}
        self.relay_entries = entries if entries is not None else []
        self.relay_status = {}
        self.relay_last_error = {}
        self.relay_last_publish = {}
        self.settings_page = None
        self.scheduled = []
        self.published_lists = 0

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        try:
            fn = _unbound_client(name)
        except StopIteration:
            raise AttributeError(name)
        bound = fn.__get__(self)
        object.__setattr__(self, name, bound)
        return bound

    def emit(self, *a):
        self.emitted.append(a)

    def _log_relay(self, line):
        self.log.append(line)

    def add_relay_connection(self, url):
        pass

    def probe_tor(self):
        pass

    def _schedule_relay_list_publish(self):
        self.scheduled.append(True)
        return False

    def publish_relay_list(self):
        self.published_lists += 1


# -- settings_page persistence (AC6) ------------------------------------------


def test_set_settings_page_persists_and_reloads():
    c = FakeClient()
    c.set_settings_page("relays")
    data = json.load(open(c.config_file))
    assert data["settings_page"] == "relays"
    # reload path: load_config picks it back up
    c2 = FakeClient()
    c2.config_file = c.config_file
    c2.load_config()
    assert c2.settings_page == "relays"


def test_set_settings_page_noop_when_unchanged():
    c = FakeClient()
    c.set_settings_page("media")
    before = os.path.getmtime(c.config_file)
    c.set_settings_page("media")  # unchanged -> no rewrite
    c.set_settings_page(None)  # empty -> ignored
    assert c.settings_page == "media"
    assert before == os.path.getmtime(c.config_file)


def test_load_config_ignores_bad_page_value():
    c = FakeClient()
    with open(c.config_file, "w") as f:
        json.dump({"settings_page": 42}, f)
    c.load_config()
    assert c.settings_page is None


# -- reorder re-publishes 10002 (spec §B) -------------------------------------


def test_move_relay_entry_republishes_10002():
    entries = [
        {"url": "wss://a.example", "roles": ["read", "write"], "enabled": True},
        {"url": "wss://b.example", "roles": ["read", "write"], "enabled": True},
    ]
    c = FakeClient(entries)
    assert c.move_relay_entry("wss://b.example", -1) is True
    assert c.relay_entries[0]["url"] == "wss://b.example"
    assert c.scheduled, "reorder must schedule a debounced 10002 re-publish"
    assert ("relay-list-updated",) in c.emitted


def test_move_relay_entry_clamped_is_noop():
    entries = [
        {"url": "wss://a.example", "roles": ["read", "write"], "enabled": True},
    ]
    c = FakeClient(entries)
    assert c.move_relay_entry("wss://a.example", -1) is False
    assert not c.scheduled


# -- per-relay details state ---------------------------------------------------


def test_handle_status_tracks_state_and_errors():
    c = FakeClient()
    st = _NS["ConnectionState"]
    c._handle_status("wss://a.example", st.CONNECTED)
    assert c.relay_status["wss://a.example"].status == "CONNECTED"
    c._handle_status("wss://a.example", st.WARNING)
    assert "Warning" in c.relay_last_error["wss://a.example"]
    c._handle_status("wss://a.example", st.CONNECTED)
    assert "wss://a.example" not in c.relay_last_error
    assert ("status-changed", "CONNECTED") in c.emitted


def test_handle_status_without_dicts_keeps_old_behaviour():
    # Legacy ast-fakes predate the dicts: the emit must still fire and no
    # AttributeError may escape.
    class Legacy:
        def _log_relay(self, line):
            pass

        def _role_tag(self, url):
            return ""

        def emit(self, *a):
            self.emitted = a

    legacy = Legacy()
    st = _NS["ConnectionState"]
    _unbound_client("_handle_status").__get__(legacy)("wss://a.example", st.CONNECTED)
    assert legacy.emitted == ("status-changed", "CONNECTED")


def test_handle_ok_records_last_publish():
    c = FakeClient()
    ev = {"id": "e" * 64}
    c.pending_publishes = {ev["id"]: {"label": "Post", "expires": 1e12, "tried": set()}}
    c._handle_ok(ev["id"], True, "", "wss://a.example")
    assert c.relay_last_publish["wss://a.example"].startswith("Post OK")


# -- role summary copy (tux Q1 subtitle) --------------------------------------


def _role_summary():
    src = open(_DIALOGS, encoding="utf-8").read()
    cls = next(
        n
        for n in ast.parse(src).body
        if isinstance(n, ast.ClassDef) and n.name == "RelayPreferencesWindow"
    )
    fn = next(
        n
        for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "_role_summary"
    )
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), _DIALOGS, "exec"), ns)
    return ns["_role_summary"].__func__


def test_role_summary_copy():
    rs = _role_summary()
    assert rs(["read", "write"]) == "Read · Write"
    assert rs(["read"]) == "Read only"
    assert rs(["write", "dm"]) == "Write · DM"
    assert rs([]) == "No roles"


# --- regression: settings window crashed on open (FuriPhone 2026-11-08) -----
# `self.nwc_uri_row.set_visibility(False)` — Adw.EntryRow has no
# set_visibility(); the AttributeError killed RelayPreferencesWindow.__init__
# the moment Settings was clicked. The row must be an Adw.PasswordEntryRow
# (masked spending credential) and no widget in dialogs.py may call the
# non-existent set_visibility() (only Gtk.Entry-style widgets have it).


def _walk_calls(tree):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
        ):
            yield node.func.value.attr, node.func.attr


def test_no_set_visibility_calls_in_dialogs():
    tree = ast.parse(open(_DIALOGS).read())
    bad = [
        f"{obj}.{meth}"
        for obj, meth in _walk_calls(tree)
        if meth == "set_visibility"
    ]
    assert bad == [], f"widgets have no set_visibility(): {bad}"


def test_nwc_uri_row_is_password_entry_row():
    tree = ast.parse(open(_DIALOGS).read())
    kinds = [
        node.value.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and isinstance(node.targets[0], ast.Attribute)
        and node.targets[0].attr == "nwc_uri_row"
    ]
    assert kinds == ["PasswordEntryRow"]
