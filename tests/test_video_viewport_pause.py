"""VID-2: viewport-aware video pause/resume, headless.

Direct tests against the real VideoPlayer.scan_viewport / pause_all_playing
(renderer.py imports fine under the conftest gi mocks — same as
test_codec_support.py), plus ast-extracted FeedView wiring methods (same
pattern as test_scroll_gate_wiring.py) asserting the debounce plumbing calls
the sweep with the scrolledwindow and pauses on unmap.

Acceptance mapping:
  * pause-on-out-of-view  -> test_scan_pauses_offscreen_player
  * resume-on-return      -> test_scan_resumes_only_viewport_paused_player
  * user pause respected  -> test_scan_never_resumes_user_paused_player
  * fast flick immunity   -> debounce lives in FeedView._schedule_viewport_scan
                             (timer restart per tick; asserted in
                             test_schedule_restarts_timer_each_tick)
"""

import ast
import os
from unittest.mock import MagicMock

from gnostr.renderer import VideoPlayer

_SRC = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "gnostr")
)


class FakePipeline:
    def __init__(self):
        self.states = []

    def set_state(self, state):
        self.states.append(state)


class FakeVideo:
    """Minimal stand-in for a live VideoPlayer widget in the cache."""

    def __init__(self, y, height=200, playing=True):
        self._pipeline = FakePipeline()
        self._is_playing = playing
        self._y = y
        self._height = height
        self._unmapped = False

    def translate_coordinates(self, widget, x, y):
        if self._unmapped:
            return None
        return (0, self._y)

    def get_height(self):
        return self._height


class FakeScrolled:
    def __init__(self, height=600):
        self._h = height

    def get_height(self):
        return self._h


def _install(video, url):
    VideoPlayer._cache[url] = video
    return video


def _clear():
    VideoPlayer._cache.clear()


def setup_function(_):
    _clear()


def teardown_function(_):
    _clear()


# ----------------------------------------------------------------------
# scan_viewport geometry
# ----------------------------------------------------------------------


def test_scan_pauses_offscreen_player():
    """Playing player below the 600px viewport -> PAUSED + remembered."""
    v = _install(FakeVideo(y=1500), "u1")
    VideoPlayer.scan_viewport(FakeScrolled(600))
    assert v._is_playing is False
    assert v._offscreen_paused is True
    assert v._pipeline.states  # set_state was called


def test_scan_leaves_visible_player_alone():
    """Player intersecting the visible rect keeps playing, untouched."""
    v = _install(FakeVideo(y=400), "u1")  # 400..600 intersects 0..600
    VideoPlayer.scan_viewport(FakeScrolled(600))
    assert v._is_playing is True
    assert not getattr(v, "_offscreen_paused", False)
    assert v._pipeline.states == []


def test_scan_resumes_only_viewport_paused_player():
    """Return-to-view resumes ONLY what the sweep paused, nothing else."""
    v = _install(FakeVideo(y=1500), "u1")
    VideoPlayer.scan_viewport(FakeScrolled(600))  # pause it
    assert v._is_playing is False
    v._y = 100  # scrolled back into view
    VideoPlayer.scan_viewport(FakeScrolled(600))
    assert v._is_playing is True
    assert v._offscreen_paused is False


def test_scan_never_resumes_user_paused_player():
    """A player the user paused (no _offscreen_paused flag) stays paused."""
    v = _install(FakeVideo(y=1500, playing=False), "u1")
    v._y = 100  # in view
    VideoPlayer.scan_viewport(FakeScrolled(600))
    assert v._is_playing is False
    assert v._pipeline.states == []


def test_unmapped_widget_counts_as_not_visible():
    """translate_coordinates -> None (unmapped/detached) must pause."""
    v = _install(FakeVideo(y=100), "u1")
    v._unmapped = True
    VideoPlayer.scan_viewport(FakeScrolled(600))
    assert v._is_playing is False
    assert v._offscreen_paused is True


def test_partial_intersection_at_top_edge():
    """A video straddling the top edge (y<0, bottom>0) is still visible."""
    v = _install(FakeVideo(y=-50), "u1")  # -50..150 intersects 0..600
    VideoPlayer.scan_viewport(FakeScrolled(600))
    assert v._is_playing is True


def test_scan_tolerates_none_scrolled_and_dead_players():
    """None scrolledwindow is a no-op; dead (pipeline-less) widgets skipped."""
    VideoPlayer.scan_viewport(None)  # must not raise
    dead = _install(FakeVideo(y=1500), "u1")
    dead._pipeline = None
    VideoPlayer.scan_viewport(FakeScrolled(600))  # must not raise


# ----------------------------------------------------------------------
# pause_all_playing (unmap path)
# ----------------------------------------------------------------------


def test_pause_all_playing_pauses_every_playing_player():
    a = _install(FakeVideo(y=100), "u1")
    b = _install(FakeVideo(y=900, playing=False), "u2")  # already paused
    VideoPlayer.pause_all_playing()
    assert a._is_playing is False and a._offscreen_paused is True
    assert b._pipeline.states == []  # user-paused untouched


# ----------------------------------------------------------------------
# FeedView wiring (ast-extracted; FeedView itself can't be instantiated
# under the Mock-based gi — same harness as test_scroll_gate_wiring.py)
# ----------------------------------------------------------------------


def _feedview_method(name):
    path = os.path.join(_SRC, "ui", "feed_view.py")
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FeedView"
    )
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    mod = ast.Module(body=[fn], type_ignores=[])
    ns = {
        "GLib": MagicMock(),
        "SCROLL_SETTLE_MS": 200,
        # so `from ..renderer import ...` inside the method resolves
        "__package__": "gnostr.ui",
        "__name__": "gnostr.ui.feed_view",
    }
    exec(compile(mod, path, "exec"), ns)
    return ns[name], ns


def test_schedule_restarts_timer_each_tick():
    """Every scroll tick removes the pending source and re-arms — the
    debounce that makes a fast flick past a video a no-op."""
    method, ns = _feedview_method("_schedule_viewport_scan")
    glib = ns["GLib"]
    glib.timeout_add.return_value = 42

    class FakeView:
        _viewport_scan_id = None

        def _run_viewport_scan(self):
            pass

    view = FakeView()
    method(view)
    assert view._viewport_scan_id == 42
    method(view)  # second tick cancels the first
    glib.source_remove.assert_called_once_with(42)


def test_run_viewport_scan_sweeps_scrolled_and_disarms():
    method, _ = _feedview_method("_run_viewport_scan")

    calls = []

    class FakeVideoPlayer:
        @staticmethod
        def scan_viewport(scrolled):
            calls.append(scrolled)

    import sys

    saved = sys.modules.get("gnostr.renderer")
    fake_mod = MagicMock()
    fake_mod.VideoPlayer = FakeVideoPlayer
    sys.modules["gnostr.renderer"] = fake_mod
    try:

        class FakeView:
            _viewport_scan_id = 7
            _scrolled = object()

        view = FakeView()
        result = method(view)
        assert result is False  # one-shot timer
        assert view._viewport_scan_id is None
        assert calls == [view._scrolled]
    finally:
        if saved is not None:
            sys.modules["gnostr.renderer"] = saved


def test_unmap_handler_pauses_all_playing():
    method, _ = _feedview_method("_on_unmap_pause_videos")
    calls = []

    class FakeVideoPlayer:
        @staticmethod
        def pause_all_playing():
            calls.append(True)

    import sys

    saved = sys.modules.get("gnostr.renderer")
    fake_mod = MagicMock()
    fake_mod.VideoPlayer = FakeVideoPlayer
    sys.modules["gnostr.renderer"] = fake_mod
    try:
        method(object())  # bound loosely; takes *_args
        assert calls == [True]
    finally:
        if saved is not None:
            sys.modules["gnostr.renderer"] = saved


def test_feed_view_source_wires_debounce_and_unmap():
    """Source-level guard: value-changed path schedules the scan, and the
    unmap signal is connected at construction."""
    src = open(os.path.join(_SRC, "ui", "feed_view.py"), encoding="utf-8").read()
    assert 'self.connect("unmap", self._on_unmap_pause_videos)' in src
    # _mark_scroll_stamp (the value-changed handler) must arm the debounce
    tree = ast.parse(src)
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FeedView"
    )
    stamp = next(
        n
        for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "_mark_scroll_stamp"
    )
    body_src = ast.unparse(stamp)
    assert "_schedule_viewport_scan()" in body_src
    assert "if self._syncing" in body_src  # programmatic moves must not sweep
