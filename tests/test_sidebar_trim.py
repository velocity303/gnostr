"""RELSET-4 sidebar trim: logout dialog wiring + status-row icon states.

Spec §C + tux verdict (docs/specs/relays-and-settings-v1-usability.md):
- Q3: logout is a footer icon button (>=44px, separated from the status
  glyphs); the click opens an Adw.AlertDialog whose default is Cancel and
  whose 'Log out' response is DESTRUCTIVE; only the logout response runs
  the existing on_logout_clicked flow.
- §C.2: the network status emoji is replaced by a themed symbolic icon
  whose state rides the success/warning/error CSS classes.

conftest mocks gi, so MainWindow/Sidebar are unconstructable; the real
methods are ast-extracted and bound to fakes (house pattern from
test_follows_view.py), and the widget-construction contracts are read off
the AST of sidebar.py.
"""

import ast
import os
from unittest.mock import MagicMock, Mock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MAIN_PATH = os.path.normpath(os.path.join(_HERE, "..", "src", "gnostr", "main.py"))
_SIDEBAR_PATH = os.path.normpath(
    os.path.join(_HERE, "..", "src", "gnostr", "ui", "sidebar.py")
)


@pytest.fixture
def health_store(tmp_path, monkeypatch):
    """Point key_health's state file at a temp dir (GLib is mocked in conftest)."""
    from gnostr import key_health

    monkeypatch.setattr(key_health.GLib, "get_user_data_dir", lambda: str(tmp_path))
    return tmp_path


def _extract(path, cls_name, *names, extra_ns=None):
    """Compile the named FunctionDefs out of one class (gi is mocked)."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name
    )
    ns = {"__package__": "gnostr"}
    if extra_ns:
        ns.update(extra_ns)
    for name in names:
        fn = next(
            n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name
        )
        mod = ast.Module(body=[fn], type_ignores=[])
        exec(compile(mod, path, "exec"), ns)
    return ns


# --- confirm_logout dialog wiring (cancel vs confirm) ---


class _FakeAdw:
    """Stand-in for the Adw module inside the ast-extracted method."""

    class ResponseAppearance:
        DESTRUCTIVE = "destructive"

    def __init__(self):
        self.made = []

    class _Dialog:
        def __init__(self, **kw):
            self.kw = kw
            self.appearances = {}
            self.responses = {}
            self.default = None
            self.close_resp = None
            self.cb = None

        def add_response(self, rid, label):
            self.responses[rid] = label

        def set_response_appearance(self, rid, app):
            self.appearances[rid] = app

        def set_default_response(self, rid):
            self.default = rid

        def set_close_response(self, rid):
            self.close_resp = rid

        def connect(self, sig, cb):
            assert sig == "response"
            self.cb = cb

        def present(self, parent):
            self.presented_to = parent

    def AlertDialog(self, **kw):
        d = _FakeAdw._Dialog(**kw)
        self.made.append(d)
        return d


def _confirm_logout_with(monkeypatch, response):
    """Run the real MainWindow.confirm_logout against a fake Adw and return
    (dialog, fake_mainwindow)."""
    fake_adw = _FakeAdw()
    fake = Mock()
    ns = _extract(
        _MAIN_PATH, "MainWindow", "confirm_logout", extra_ns={"Adw": fake_adw}
    )
    ns["confirm_logout"](fake)
    d = fake_adw.made[0]
    if d.cb:
        d.cb(d, response)
    return d, fake


def test_confirm_logout_confirm_runs_logout_flow(monkeypatch):
    _, fake = _confirm_logout_with(monkeypatch, "logout")
    fake.on_logout_clicked.assert_called_once()


def test_confirm_logout_cancel_keeps_session(monkeypatch):
    _, fake = _confirm_logout_with(monkeypatch, "cancel")
    fake.on_logout_clicked.assert_not_called()


def test_confirm_logout_dialog_contract(monkeypatch):
    """tux Q3: default response is Cancel, Log out is DESTRUCTIVE, Escape
    closes as cancel."""
    d, _ = _confirm_logout_with(monkeypatch, "cancel")
    assert d.default == "cancel", "dialog must default to Cancel (tux Q3)"
    assert d.close_resp == "cancel", "Escape must not log out"
    assert d.appearances.get("logout") == "destructive"
    assert set(d.responses) == {"cancel", "logout"}


# --- status-row icon states (sidebar.update_status) ---


def _update_status(state):
    ns = _extract(_SIDEBAR_PATH, "Sidebar", "update_status")
    icon = MagicMock()
    fake = Mock()
    fake.status_icon = icon
    ns["update_status"](fake, state)
    return icon


def test_update_status_css_state_classes():
    for state, css in (
        ("CONNECTED", "success"),
        ("WARNING", "warning"),
        ("DISCONNECTED", "error"),
        ("WEIRD", "error"),
    ):
        icon = _update_status(state)
        icon.add_css_class.assert_called_once_with(css)
        removed = {c[0][0] for c in icon.remove_css_class.call_args_list}
        assert {
            "success",
            "warning",
            "error",
        } <= removed, "all three state classes must be cleared before adding one"
        tip = icon.set_tooltip_text.call_args[0][0]
        assert state.lower() in tip


def test_status_row_is_centered_three_icons_plus_logout():
    """§C.2 + spec update 8def2ab: footer is a CENTERED cluster of same-size
    symbolic icons; logout is a >=44px button separated from the status
    glyphs; no reserved slots (no invisible placeholder widgets)."""
    with open(_SIDEBAR_PATH, encoding="utf-8") as f:
        src = f.read()
    assert "self.status_row.set_halign(Gtk.Align.CENTER)" in src
    assert 'icon_name="system-log-out-symbolic"' in src
    assert 'set_tooltip_text("Log out")' in src
    assert "set_size_request(44, 44)" in src, "logout must keep a 44px target"
    assert "Gtk.Separator" in src, "logout must be visually separated"
    # the removed widgets must be gone from the sidebar entirely
    for gone in ("key_health_label", "back_up_key", "relay_expander"):
        assert gone not in src, f"{gone} must not remain in sidebar.py"


def test_logout_click_delegates_to_confirm_dialog():
    """The sidebar button never logs out directly — it opens the dialog."""
    ns = _extract(_SIDEBAR_PATH, "Sidebar", "on_logout_clicked")
    fake = Mock()
    ns["on_logout_clicked"](fake, Mock())
    fake.main_window.confirm_logout.assert_called_once()
    fake.main_window.on_logout_clicked.assert_not_called()


# --- key-health banner (tux Q5) ---


def test_key_banner_hidden_for_remote_signer_and_shown_for_unbacked_local(
    health_store,
):
    """Banner shows only when a LOCAL key is loaded and no backup was ever
    verified; a bunker (remote) session never shows it (NIP-49 is local)."""
    from gnostr import key_health

    ns = _extract(_MAIN_PATH, "MainWindow", "_refresh_key_banner")
    fake = Mock()
    fake.pub_key = "ab" * 32
    ns["_refresh_key_banner"](fake)
    assert (
        fake.key_banner.set_revealed.call_args[0][0] is True
    ), "unbacked local key must show the banner"
    key_health.mark_backup_verified()
    fake.key_banner.reset_mock()
    ns["_refresh_key_banner"](fake)
    assert fake.key_banner.set_revealed.call_args[0][0] is False

    fake2 = Mock()
    fake2.pub_key = None  # bunker session: pub_key set only for local flow
    ns["_refresh_key_banner"](fake2)
    assert fake2.key_banner.set_revealed.call_args[0][0] is False
