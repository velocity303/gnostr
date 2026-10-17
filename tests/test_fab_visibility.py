"""FAB visibility wiring: the compose FAB shows only on the root feed page.

The FAB lives in a Gtk.Overlay above content_nav, so without gating it floats
over DM threads, the inbox, profiles and thread detail — pages where composing
a new post is irrelevant. main.py connects content_nav notify::visible-page to
_update_fab_visibility; this test binds the real method (ast-extracted, same
pattern as test_dm_ui_wiring) to a fake self and asserts the truth table:
feed+signed-in -> visible; any other page or signed-out -> hidden.
"""

import ast
import os
from unittest.mock import MagicMock, Mock

_MAIN_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", "gnostr", "main.py"
    )
)


def _fab_visibility():
    src = open(_MAIN_PATH, encoding="utf-8").read()
    tree = ast.parse(src)
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MainWindow"
    )
    fn = next(
        n
        for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "_update_fab_visibility"
    )
    code = compile(ast.Module(body=[fn], type_ignores=[]), _MAIN_PATH, "exec")
    ns = {}
    exec(code, ns)
    return ns["_update_fab_visibility"]


def _fake_window(on_feed, pub_key):
    win = Mock()
    win.pub_key = pub_key
    win.feed_view = object()
    win.content_nav = Mock()
    win.content_nav.get_visible_page.return_value = (
        win.feed_view if on_feed else object()
    )
    win.fab_post = Mock()
    return win


def test_fab_visible_on_feed_signed_in():
    win = _fake_window(True, "a" * 64)
    _fab_visibility()(win)
    win.fab_post.set_visible.assert_called_once_with(True)


def test_fab_hidden_on_other_pages():
    win = _fake_window(False, "a" * 64)
    _fab_visibility()(win)
    win.fab_post.set_visible.assert_called_once_with(False)


def test_fab_hidden_when_signed_out():
    win = _fake_window(True, None)
    _fab_visibility()(win)
    win.fab_post.set_visible.assert_called_once_with(False)


def test_nav_signal_connected():
    """content_nav notify::visible-page must drive the helper (wiring)."""
    src = open(_MAIN_PATH, encoding="utf-8").read()
    assert 'connect("notify::visible-page", self._update_fab_visibility)' in src


def test_login_paths_route_through_helper():
    """Both login paths must call the helper, not set_visible(True) directly
    (a direct True would re-show the FAB on a non-feed page after login)."""
    src = open(_MAIN_PATH, encoding="utf-8").read()
    assert src.count("self.fab_post.set_visible(True)") == 0
