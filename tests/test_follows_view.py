"""FollowersListView headless tests.

Under the conftest gi mocks, `class FollowersListView(Gtk.Box)` evaluates
against a Mock base, so the imported class is a Mock (same situation as
NostrClient in test_follow_sync.py). Logic tests therefore extract the
real methods from followers_list_view.py source via ast and bind them to
a lightweight fake self — exercising the actual code without the mocked
GTK machinery. Real rendering is verified on-device (and by the tier-2
real-GTK harness pattern).
"""
import ast
import os
from unittest.mock import MagicMock, Mock

_VIEW_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "src",
        "gnostr",
        "ui",
        "followers_list_view.py",
    )
)


def _unbound(method_name):
    """Extract the real FollowersListView.<method> function from source
    without importing the (Mock) class."""
    import gnostr.nostr_utils as _nu

    src = open(_VIEW_PATH, encoding="utf-8").read()
    tree = ast.parse(src)
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef)
        and n.name == "FollowersListView"
    )
    fn = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name
    )
    mod = ast.Module(body=[fn], type_ignores=[])
    ns = {
        "Adw": MagicMock(),
        "Gtk": MagicMock(),
        "hex_to_npub": _nu.hex_to_npub,
    }
    exec(compile(mod, _VIEW_PATH, "exec"), ns)
    return ns[method_name]


class FakeView:
    """Minimal self for FollowersListView method tests. Any real method
    not stubbed here is auto-bound from source on first access."""

    def __init__(self, is_own=False, owner="f" * 64):
        self.owner_pubkey = owner
        self.is_own = is_own
        self.list_rows = []
        self.btn_sync = Mock() if is_own else None
        self.btn_refresh = Mock()
        self.main_window = MagicMock()
        self.main_window.db.get_profile.return_value = {}
        self.title_label = MagicMock()
        self.list_box = MagicMock()

    def __getattr__(self, name):
        if name.startswith("_") and name not in (
            "_display_name",
            "_title",
            "_make_row",
            "_on_row_activated",
            "_on_contacts_updated",
        ):
            raise AttributeError(name)
        fn = _unbound(name)
        bound = fn.__get__(self)
        object.__setattr__(self, name, bound)
        return bound


def test_reload_builds_one_row_per_followed_pubkey():
    view = FakeView(is_own=True, owner="a" * 64)
    view.main_window.db.get_following_list.return_value = [
        "b" * 64,
        "c" * 64,
        "d" * 64,
    ]

    view.reload()

    # One row per followed pubkey, appended to the list box in order.
    assert len(view.list_rows) == 3
    assert view.list_box.append.call_count == 3
    view.main_window.db.get_following_list.assert_called_with("a" * 64)
    # Every row kicks a (TTL-cached) profile metadata fetch for its pubkey.
    fetched = [c.args[0] for c in view.main_window.client.fetch_profile.call_args_list]
    assert fetched == ["b" * 64, "c" * 64, "d" * 64]


def test_reload_clears_previous_rows():
    view = FakeView(is_own=True)
    view.main_window.db.get_following_list.return_value = ["b" * 64]
    view.reload()
    assert len(view.list_rows) == 1

    view.main_window.db.get_following_list.return_value = []
    view.reload()
    assert view.list_rows == []
    # The previous row was removed from the list box.
    view.list_box.remove.assert_called()


def test_contacts_updated_reloads_for_matching_owner_only():
    view = FakeView(owner="f" * 64)
    view.main_window.db.get_following_list.return_value = ["b" * 64]
    view._on_contacts_updated(MagicMock(), "f" * 64)
    assert len(view.list_rows) == 1

    # A different owner's reconciliation must NOT reload this view.
    view.main_window.db.get_following_list.return_value = []
    view._on_contacts_updated(MagicMock(), "a" * 64)
    assert len(view.list_rows) == 1


def test_refresh_uses_fetch_contacts_for_owner():
    view = FakeView(owner="f" * 64)
    view.refresh()
    view.main_window.client.fetch_contacts_for.assert_called_once_with("f" * 64)


def test_row_activation_opens_profile():
    view = FakeView(is_own=True)
    row = Mock()
    row.pubkey = "b" * 64
    view._on_row_activated(MagicMock(), row)
    view.main_window.show_profile.assert_called_once_with("b" * 64)


def test_title_uses_own_vs_foreign_wording():
    view = FakeView(is_own=True, owner="a" * 64)
    view.main_window.db.get_profile.return_value = {"name": "Alice"}
    assert view._title(5) == "You're following — 5"

    foreign = FakeView(is_own=False, owner="f" * 64)
    foreign.main_window.db.get_profile.return_value = {"name": "Alice"}
    assert foreign._title(3) == "Alice is following — 3"

    # No cached profile -> hex-prefix fallback.
    foreign.main_window.db.get_profile.return_value = None
    assert foreign._title(3) == f"{'f' * 8} is following — 3"
