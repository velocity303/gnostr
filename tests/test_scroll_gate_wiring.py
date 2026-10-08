"""Wiring tests for the ScrollGate (issue #22), headless.

Exercises the REAL handler code — FeedView stamping, PostWidget._activation_gated
and its footer buttons, ContentRenderer._activation_gated / _on_quote_clicked —
against a real ScrollGate. Under the conftest gi mocks the widget classes are
Mocks, so the real methods are extracted from source via ast and bound to
lightweight fakes (same pattern as test_follows_view.py).

Regression guards for the two inert-gate bugs found in the PR #23 review:
  * bug 1: is_touch_source must compare against Gdk.InputSource.TOUCHSCREEN
    (Gdk.EventType.TOUCHSCREEN / Gdk.SourceType do not exist in GTK4 and the
    except-swallow made the gate never engage on the phone).
  * bug 2: the renderer gate must read the device class off the GATE, not off
    a window attribute nobody sets.
"""

import ast
import os
import time
from unittest.mock import MagicMock

from gnostr.util.scroll_gate import ScrollGate

_SRC = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "gnostr")
)


def _unbound(rel_path, cls_name, method_name):
    """Extract the real <cls>.<method> function from source without importing
    the (Mock-based) class."""
    path = os.path.join(_SRC, rel_path)
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name
    )
    fn = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name
    )
    mod = ast.Module(body=[fn], type_ignores=[])
    ns = {
        "Gtk": MagicMock(),
        "Adw": MagicMock(),
        "Gdk": MagicMock(),
        "Pango": MagicMock(),
        "ScrollGate": ScrollGate,
    }
    exec(compile(mod, path, "exec"), ns)
    return ns[method_name]


class FakeSource:
    def __init__(self, input_source):
        self._s = input_source

    def get_input_source(self):
        return self._s


class FakeDevice:
    def __init__(self, source):
        self._source = source

    def get_source_device(self):
        return self._source


class FakeGesture:
    def __init__(self, device):
        self._device = device

    def get_current_event_device(self):
        return self._device


def _touch_gate():
    gate = ScrollGate(clock=time.monotonic, grace_ms=600)
    gate.mark_scroll(is_touch=True)
    return gate


# ---------------------------------------------------------------------------
# bug-1 regression: the enum used by is_touch_source
# ---------------------------------------------------------------------------


def test_scroll_gate_references_the_real_gdk_enum():
    """Source guard: GTK4 has Gdk.InputSource.TOUCHSCREEN only. The wrong enum
    names raise AttributeError, the except swallows it, and the gate silently
    never engages on the phone — a headless suite can't catch that by behavior,
    so guard the source."""
    src = open(os.path.join(_SRC, "util", "scroll_gate.py"), encoding="utf-8").read()
    assert "Gdk.InputSource.TOUCHSCREEN" in src
    assert "Gdk.EventType.TOUCHSCREEN" not in src
    assert "Gdk.SourceType" not in src


def test_is_touch_source_classifies_with_mocked_gdk():
    """With gi.repository.Gdk mocked, is_touch_source must return True for a
    source whose get_input_source() is Gdk.InputSource.TOUCHSCREEN and False
    for a mouse source. NOTE: conftest patches sys.modules['gi.repository'] to
    a Mock, so `from gi.repository import Gdk` resolves as an ATTRIBUTE on that
    mock — patch it there, not on the submodule entry."""
    import sys

    repo = sys.modules["gi.repository"]
    gdk = MagicMock()
    gdk.InputSource.TOUCHSCREEN = 3
    gdk.InputSource.MOUSE = 2
    saved = repo.Gdk
    repo.Gdk = gdk
    try:
        assert ScrollGate.is_touch_source(FakeSource(3)) is True
        assert ScrollGate.is_touch_source(FakeSource(2)) is False
        assert ScrollGate.is_touch_source(None) is False
    finally:
        repo.Gdk = saved


# ---------------------------------------------------------------------------
# bug-2 regression: renderer gate reads the device class off the GATE
# ---------------------------------------------------------------------------


def test_renderer_gated_after_touch_scroll_reads_gate():
    from gnostr.renderer import ContentRenderer

    win = type("Win", (), {})()
    win.scroll_gate = _touch_gate()
    # Deliberately do NOT set win.last_scroll_was_touch — the flag lives on the
    # gate. The old code read the window attribute and was inert.
    assert ContentRenderer._activation_gated(win) is True


def test_renderer_not_gated_after_wheel_scroll():
    from gnostr.renderer import ContentRenderer

    win = type("Win", (), {})()
    win.scroll_gate = ScrollGate(clock=time.monotonic, grace_ms=600)
    win.scroll_gate.mark_scroll(is_touch=False)
    # Even with a stale True on the window, the gate's class wins.
    win.last_scroll_was_touch = True
    assert ContentRenderer._activation_gated(win) is False


def test_renderer_not_gated_without_gate():
    from gnostr.renderer import ContentRenderer

    win = type("Win", (), {})()
    assert ContentRenderer._activation_gated(win) is False


# ---------------------------------------------------------------------------
# FeedView stamping (real _mark_scroll_stamp / _last_scroll_was_touch)
# ---------------------------------------------------------------------------


class FakeFeedView:
    def __init__(self, last_event):
        self._syncing = False
        self._scroll_gate = ScrollGate(clock=time.monotonic, grace_ms=600)
        self._scrolled = MagicMock()
        self._scrolled.get_last_event.return_value = last_event

    def __getattr__(self, name):
        if name.startswith("_") or name in ("on_scroll_changed",):
            fn = _unbound("ui/feed_view.py", "FeedView", name)

            def bound(*a, _s=self, _f=fn):
                return _f(_s, *a)

            object.__setattr__(self, name, bound)
            return bound
        raise AttributeError(name)


def _event_with_source(input_source):
    ev = MagicMock()
    ev.get_device.return_value = FakeDevice(FakeSource(input_source))
    return ev


def test_feed_view_stamps_touch_scroll():
    view = FakeFeedView(_event_with_source(3))  # InputSource.TOUCHSCREEN
    view._mark_scroll_stamp(MagicMock())
    assert view._scroll_gate.in_cooldown() is True
    assert view._scroll_gate.last_scroll_was_touch is True


def test_feed_view_records_wheel_scroll_as_non_touch():
    view = FakeFeedView(_event_with_source(2))  # InputSource.MOUSE
    view._mark_scroll_stamp(MagicMock())
    assert view._scroll_gate.in_cooldown() is True
    assert view._scroll_gate.last_scroll_was_touch is False


def test_feed_view_skips_stamp_while_syncing():
    view = FakeFeedView(_event_with_source(3))
    view._syncing = True
    view._mark_scroll_stamp(MagicMock())
    assert view._scroll_gate.in_cooldown() is False


def test_feed_view_unknown_event_defaults_to_touch():
    view = FakeFeedView(None)
    view._mark_scroll_stamp(MagicMock())
    assert view._scroll_gate.last_scroll_was_touch is True


# ---------------------------------------------------------------------------
# PostWidget gate (real _activation_gated + footer buttons, slice 2)
# ---------------------------------------------------------------------------


class FakePostWidget:
    def __init__(self, gate):
        self.main_window = type("Win", (), {})()
        self.main_window.scroll_gate = gate
        self.main_window.client = MagicMock()
        self.main_window.pub_key = "a" * 64
        self.event_id = "e" * 64
        self.pubkey = "b" * 64
        self._liked = False
        self._root_id = None
        self._root_pk = None
        self.btn_like = MagicMock()
        self.lbl_likes = MagicMock()
        self.lbl_likes.get_label.return_value = "0"

    def __getattr__(self, name):
        fn = _unbound("ui/post_widget.py", "PostWidget", name)

        def bound(*a, _s=self, _f=fn):
            return _f(_s, *a)

        object.__setattr__(self, name, bound)
        return bound


def test_card_gate_uses_real_touch_device():
    w = FakePostWidget(_touch_gate())
    gn = FakeGesture(FakeDevice(FakeSource(3)))
    assert w._activation_gated(gn) is True


def test_card_gate_not_applied_for_mouse_click():
    w = FakePostWidget(_touch_gate())
    gn = FakeGesture(FakeDevice(FakeSource(2)))
    # A mouse click during a touch-scroll cooldown must still activate.
    assert w._activation_gated(gn) is False


def test_card_gate_falls_back_to_scroll_stamp_without_device():
    w = FakePostWidget(_touch_gate())
    gn = FakeGesture(None)
    assert w._activation_gated(gn) is True


def test_like_button_swallowed_during_touch_cooldown():
    w = FakePostWidget(_touch_gate())
    w._on_like(w.btn_like)
    w.main_window.client.publish_reaction.assert_not_called()


def test_like_button_works_after_cooldown():
    gate = ScrollGate(clock=time.monotonic, grace_ms=600)
    gate.mark_scroll(is_touch=True)
    gate.last_stamp = time.monotonic() - 1.0  # glide long over
    w = FakePostWidget(gate)
    w.main_window.client.publish_reaction.return_value = True
    w._on_like(w.btn_like)
    w.main_window.client.publish_reaction.assert_called_once()


def test_repost_button_swallowed_during_touch_cooldown():
    w = FakePostWidget(_touch_gate())
    w._on_repost(MagicMock())
    w.main_window.client.publish_repost.assert_not_called()


# ---------------------------------------------------------------------------
# Quote-card gate (slice 2)
# ---------------------------------------------------------------------------


def test_quote_click_swallowed_during_touch_cooldown():
    from gnostr.renderer import ContentRenderer

    win = MagicMock()
    win.scroll_gate = _touch_gate()
    ContentRenderer._on_quote_clicked(win, ("thread", "x" * 64), "x" * 64)
    win.show_thread.assert_not_called()


def test_quote_click_activates_without_gate():
    from gnostr.renderer import ContentRenderer

    calls = []

    class Win:  # plain object: NO scroll_gate attribute -> ungated
        def show_thread(self, *a):
            calls.append(a)

    ContentRenderer._on_quote_clicked(Win(), ("thread", "x" * 64), "x" * 64)
    assert len(calls) == 1
