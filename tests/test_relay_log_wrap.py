"""Relay Activity log wrap regression (Librem 5 field bug, G13).

The relay-activity label was constructed with wrap=False inside a
ScrolledWindow whose horizontal policy is NEVER, so a long relay line
(wss://relay.example-very-long-domain.org:8443 publish OK ...) forced the
label's natural width and stretched the whole sidebar/NavigationView column
on the phone instead of wrapping.

Source-level check (the conftest gi mocks make real GTK construction
unintrospectable): the label construction contract is read off the AST of
sidebar.py, and append_relay_log is ast-extracted and bound to a fake self
(same pattern as tests/test_follows_view.py).
"""

import ast
import os
from unittest.mock import MagicMock, Mock

_SIDEBAR_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "src",
        "gnostr",
        "ui",
        "sidebar.py",
    )
)


def _source():
    with open(_SIDEBAR_PATH, encoding="utf-8") as f:
        return f.read()


def _relay_label_call():
    """Return the ast.Call for `self.relay_log_label = Gtk.Label(...)`."""
    tree = ast.parse(_source())
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Sidebar"
    )
    for node in ast.walk(cls):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (
                    isinstance(tgt, ast.Attribute)
                    and tgt.attr == "relay_log_label"
                    and isinstance(node.value, ast.Call)
                ):
                    return node.value
    raise AssertionError("relay_log_label assignment not found in sidebar.py")


def _kwargs(call):
    return {kw.arg: kw.value for kw in call.keywords if kw.arg}


def test_relay_log_label_wraps_word_char():
    kw = _kwargs(_relay_label_call())
    wrap = kw.get("wrap")
    assert (
        isinstance(wrap, ast.Constant) and wrap.value is True
    ), "relay_log_label must be constructed with wrap=True"
    wm = kw.get("wrap_mode")
    rendered = ast.dump(wm) if wm is not None else ""
    assert "WORD_CHAR" in rendered and "Pango" in rendered, (
        "relay_log_label must set wrap_mode=Pango.WrapMode.WORD_CHAR so long "
        "URLs break only when a single word exceeds the row width"
    )


def test_relay_log_label_requests_narrow_natural_width():
    """wrap=True alone still reports the full line as natural width; the label
    must request a narrow natural width (max_width_chars) + hexpand so it
    wraps inside the row instead of stretching the sidebar column."""
    kw = _kwargs(_relay_label_call())
    mw = kw.get("max_width_chars")
    assert (
        isinstance(mw, ast.Constant) and isinstance(mw.value, int) and mw.value > 0
    ), (
        "relay_log_label must set max_width_chars=<small int> so its natural "
        "width stays bounded and the line wraps"
    )
    hx = kw.get("hexpand")
    assert (
        isinstance(hx, ast.Constant) and hx.value is True
    ), "relay_log_label must set hexpand=True to fill the row width"


def test_relay_log_scrolled_window_stays_no_hscroll():
    """Horizontal scrolling must stay off on mobile (policy NEVER) and the
    pane keeps its bounded height."""
    src = _source()
    assert (
        "set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)" in src
    ), "relay log ScrolledWindow must keep horizontal policy NEVER"
    assert (
        "set_max_content_height(180)" in src
    ), "relay log ScrolledWindow must keep max_content_height(180)"


def test_relay_log_label_stays_selectable():
    """The log must stay selectable and complete — no truncation/elision."""
    src = _source()
    assert "self.relay_log_label.set_selectable(True)" in src
    assert "Ellipsize" not in src, "relay log must never be elided"


def _append_relay_log():
    """Extract the real Sidebar.append_relay_log from source (the gi mocks
    make the imported class a Mock, same as test_follows_view.py)."""
    tree = ast.parse(_source())
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Sidebar"
    )
    fn = next(
        n
        for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "append_relay_log"
    )
    mod = ast.Module(body=[fn], type_ignores=[])
    ns = {}
    exec(compile(mod, _SIDEBAR_PATH, "exec"), ns)
    return ns["append_relay_log"]


def test_long_relay_line_appends_intact():
    line = (
        "[12:34:56] wss://relay.example-very-long-domain.org:8443 "
        "publish OK 749f00d8b1c2e3f4a5b6c7d8e9f0a1b2c3d4e5f6 accepted"
    )
    assert len(line) >= 100, "test line must be long enough to overflow a phone row"

    label = MagicMock()
    label.get_text.return_value = "No relay activity yet"
    fake = Mock()
    fake.relay_log_label = label

    _append_relay_log()(fake, line)

    label.set_text.assert_called_once()
    written = label.set_text.call_args[0][0]
    assert line in written, "relay line must be stored complete (no truncation)"
    assert written == f"{line}\n", "placeholder text is replaced, newest on top"

    # a second line prepends and the first survives intact
    label.get_text.return_value = written
    second = "wss://relay.example-very-long-domain.org:8443 disconnect"
    _append_relay_log()(fake, second)
    written2 = label.set_text.call_args[0][0]
    assert written2 == f"{second}\n{written}"
