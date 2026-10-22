"""Relay Activity log wrap regression (Librem 5 field bug, G13).

The relay-activity label was constructed with wrap=False inside a
ScrolledWindow whose horizontal policy is NEVER, so a long relay line
(wss://relay.example-very-long-domain.org:8443 publish OK ...) forced the
label's natural width and stretched the whole sidebar/NavigationView column
on the phone instead of wrapping.

§C trim (RELSET-4): the Relay Activity pane moved from the sidebar to the
Settings → Relays page (dialogs.RelayPreferencesWindow); the wrap contract
travels with it. The sidebar keeps only a no-op append_relay_log shim.

Source-level check (the conftest gi mocks make real GTK construction
unintrospectable): the label construction contract is read off the AST of
dialogs.py, and the log-append logic is ast-extracted and bound to a fake
self (same pattern as tests/test_follows_view.py).
"""

import ast
import os
from unittest.mock import MagicMock, Mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_DIALOGS_PATH = os.path.normpath(
    os.path.join(_HERE, "..", "src", "gnostr", "dialogs.py")
)
_SIDEBAR_PATH = os.path.normpath(
    os.path.join(_HERE, "..", "src", "gnostr", "ui", "sidebar.py")
)


def _source():
    with open(_DIALOGS_PATH, encoding="utf-8") as f:
        return f.read()


def _relay_label_call():
    """Return the ast.Call for `self.relay_log_label = Gtk.Label(...)`."""
    tree = ast.parse(_source())
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "RelayPreferencesWindow"
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
    raise AssertionError("relay_log_label assignment not found in dialogs.py")


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
    wraps inside the row instead of stretching the page column."""
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


def test_relay_log_scrolled_window_never_forces_page_sideways():
    """tux Q2: the log owns its horizontal scroll region (AUTOMATIC hpolicy,
    NEVER vertical) but the WRAP above means the page itself never scrolls
    sideways; the pane keeps a bounded height."""
    src = _source()
    assert (
        "set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)" in src
    ), "relay log ScrolledWindow must own horizontal scroll, never vertical"
    assert (
        "set_max_content_height(240)" in src
    ), "relay log ScrolledWindow must keep max_content_height(240)"


def test_relay_log_lines_capped_at_200():
    """tux Q2: retained lines capped (~200)."""
    src = _source()
    assert "maxlen=200" in src, "relay log deque must cap retained lines at 200"


def test_relay_log_label_stays_selectable():
    """The log must stay selectable and complete — no truncation/elision."""
    kw = _kwargs(_relay_label_call())
    sel = kw.get("selectable")
    assert (
        isinstance(sel, ast.Constant) and sel.value is True
    ), "relay log label must be selectable"
    assert "Ellipsize" not in _source(), "relay log must never be elided"


def _log_fns():
    """Extract the real on_relay_log_line + _render_log from dialogs.py
    source (the gi mocks make the imported class a Mock)."""
    tree = ast.parse(_source())
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "RelayPreferencesWindow"
    )
    ns = {}
    for name in ("on_relay_log_line", "_render_log"):
        fn = next(
            n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name
        )
        mod = ast.Module(body=[fn], type_ignores=[])
        exec(compile(mod, _DIALOGS_PATH, "exec"), ns)
    return ns


def test_long_relay_line_appends_intact():
    line = (
        "[12:34:56] wss://relay.example-very-long-domain.org:8443 "
        "publish OK 749f00d8b1c2e3f4a5b6c7d8e9f0a1b2c3d4e5f6 accepted"
    )
    assert len(line) >= 100, "test line must be long enough to overflow a phone row"

    import collections

    label = MagicMock()
    fake = Mock()
    fake.relay_log_label = label
    fake._log_lines = collections.deque(maxlen=200)
    ns = _log_fns()
    # bind both real methods: on_relay_log_line calls self._render_log()
    fake.on_relay_log_line = lambda c, l: ns["on_relay_log_line"](fake, c, l)
    fake._render_log = lambda: ns["_render_log"](fake)
    fake.on_relay_log_line(None, line)
    text = label.set_text.call_args[0][0]
    assert line in text, "long relay line must land intact (no truncation/elision)"

    # a second line prepends and the first survives intact (newest at top)
    second = "wss://relay.example-very-long-domain.org:8443 disconnect"
    fake.on_relay_log_line(None, second)
    written2 = label.set_text.call_args[0][0]
    assert written2 == f"{second}\n{line}", "newest at top, older line intact"


# --- §C trim: the sidebar pane is gone; append_relay_log is a no-op shim ---


def test_sidebar_append_relay_log_is_noop_shim():
    """The sidebar no longer owns a log widget; the shim must accept a line
    and touch nothing (main.py keeps routing relay-log-updated to it)."""
    with open(_SIDEBAR_PATH, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
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
    fake = Mock()
    assert ns["append_relay_log"](fake, "[12:00:00] anything") is None
    assert fake.method_calls == [], "shim must not touch any sidebar state"


def test_sidebar_no_longer_ships_a_log_widget():
    with open(_SIDEBAR_PATH, encoding="utf-8") as f:
        src = f.read()
    assert "relay_log_label" not in src
    assert "relay_expander" not in src
