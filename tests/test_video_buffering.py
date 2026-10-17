"""
VID-1: mobile video buffering — prefill gating, BUFFERING UX transitions, and
mid-playback pause-and-resume, driven through mocked Gst bus messages.

The conftest gi mocks make Gst a plain Mock, so the bus handler's numeric
message bits (GST_MESSAGE_BUFFERING = 1<<5, ASYNC_DONE = 1<<21) are the real
GStreamer values and fake messages carry int .type fields.
"""

from unittest.mock import MagicMock, Mock

import pytest

_BUFFERING = 1 << 5  # GST_MESSAGE_BUFFERING
_ASYNC_DONE = 1 << 21  # GST_MESSAGE_ASYNC_DONE


@pytest.fixture(scope="function")
def mock_container():
    # MagicMock so `for child in container` works (VideoPlayer._find_video).
    container = MagicMock()
    container.get_size_request.return_value = (-1, 200)
    container.get_parent.return_value = None
    return container


@pytest.fixture(scope="function", autouse=True)
def _reset_video_player_state():
    import sys

    from gnostr.renderer import VideoPlayer

    mock_gst = sys.modules["gi.repository.Gst"]
    mock_gst.parse_launch.reset_mock()
    mock_gst.parse_launch.side_effect = None
    with VideoPlayer._lock:
        VideoPlayer._cache.clear()


@pytest.fixture(scope="function")
def built_video(mock_container):
    """Build a player through the real code path with a mock pipeline/bus and
    return (video_widget, pipeline, bus_handler, spinner, container)."""
    import sys

    from gnostr.renderer import VideoPlayer

    mock_gst = sys.modules["gi.repository.Gst"]
    mock_gtk = sys.modules["gi.repository.Gtk"]

    mock_pipeline = Mock()
    mock_bus = Mock()
    mock_pipeline.get_bus.return_value = mock_bus
    mock_pipeline.get_by_name.return_value = Mock(get_property=lambda x: Mock())
    mock_pipeline.get_property.return_value = 0x717  # default playbin3 flags
    mock_gst.parse_launch.return_value = mock_pipeline

    mock_video = Mock()
    mock_video.get_parent.return_value = mock_container
    mock_gtk.Picture.return_value = mock_video

    mock_gst.State.PAUSED = 2
    mock_gst.State.PLAYING = 1

    spinner = Mock()
    spinner.get_parent.return_value = mock_container

    def handler(msg):
        for c in mock_bus.connect.call_args_list:
            if c[0][0] == "message":
                return c[0][1](mock_bus, msg)
        raise AssertionError("bus message handler was never connected")

    # Start from a clean widget state each build (shared Picture mock).
    for attr in (
        "_buffer_label",
        "_buffer_row",
        "_buffer_row_label",
        "_buffer_spinner",
        "_buffer_container",
    ):
        if hasattr(mock_video, attr):
            delattr(mock_video, attr)

    return mock_video, mock_pipeline, handler, spinner, mock_container


def _msg(t, pct=None, tuple_shape=False):
    m = Mock()
    m.type = t
    if pct is not None:
        # Real PyGObject gst_message_parse_buffering() returns a BARE int
        # (one out-arg; verified on GStreamer 1.26.1). tuple_shape covers
        # bindings that surface (percent, time_left).
        m.parse_buffering.return_value = (pct, None) if tuple_shape else pct
    return m


def _states(pipeline):
    return [c[0][0] for c in pipeline.set_state.call_args_list]


class TestBufferPrefill:
    def test_playbin3_buffer_props_configured(self, mock_container, built_video):
        """playbin3 gets the prefill target + the progressive-download flag."""
        import sys

        from gnostr.renderer import _BUFFER_TARGET_BYTES, _BUFFER_TARGET_NS

        _, pipeline, _, _, _ = built_video
        from gnostr.renderer import VideoPlayer

        VideoPlayer.load_and_play("http://t.test/props.mp4", mock_container, None)

        props = {c[0][0]: c[0][1] for c in pipeline.set_property.call_args_list}
        assert props["buffer-duration"] == _BUFFER_TARGET_NS
        assert props["buffer-size"] == _BUFFER_TARGET_BYTES
        # default 0x717 | GST_PLAY_FLAG_DOWNLOAD (1<<7)
        assert props["flags"] == 0x717 | (1 << 7)


class TestPrefillGate:
    def test_autoplay_video_is_gated_not_playing_immediately(
        self, mock_container, built_video
    ):
        """A tapped mp4 with autoplay waits at the gate: no PLAYING, spinner
        stays visible with a percent label."""
        from gnostr.renderer import VideoPlayer

        video, pipeline, _, spinner, container = built_video
        VideoPlayer.load_and_play(
            "http://t.test/gated.mp4", mock_container, spinner, autoplay=True
        )

        assert video._buffer_gate is True
        assert 1 not in _states(pipeline), "gated play must not set PLAYING yet"
        removed = [c[0][0] for c in container.remove.call_args_list]
        assert spinner not in removed, "spinner must stay until threshold"
        assert video._buffer_label is not None

    def test_gif_autoplay_not_gated(self, mock_container, built_video):
        """GIFs keep the old preroll+loop UX: PLAYING immediately."""
        from gnostr.renderer import VideoPlayer

        video, pipeline, _, _, _ = built_video
        VideoPlayer.load_and_play(
            "http://t.test/anim.gif", mock_container, None, autoplay=True
        )

        assert video._buffer_gate is False
        assert 1 in _states(pipeline)

    def test_buffering_below_threshold_keeps_gate(self, mock_container, built_video):
        from gnostr.renderer import VideoPlayer

        video, pipeline, handler, _, _ = built_video
        VideoPlayer.load_and_play(
            "http://t.test/low.mp4", mock_container, None, autoplay=True
        )
        pipeline.set_state.reset_mock()

        handler(_msg(_BUFFERING, 30))

        assert 1 not in _states(pipeline), "30% < threshold must not start play"
        assert video._buffer_pct == 30
        assert video._buffer_hits == 1
        video._buffer_label.set_text.assert_called_with("Buffering 30%")

    def test_buffering_threshold_releases_gate(self, mock_container, built_video):
        from gnostr.renderer import VideoPlayer

        video, pipeline, handler, spinner, container = built_video
        VideoPlayer.load_and_play(
            "http://t.test/thresh.mp4", mock_container, spinner, autoplay=True
        )
        pipeline.set_state.reset_mock()
        container.remove.reset_mock()

        handler(_msg(_BUFFERING, 80))

        assert 1 in _states(pipeline), "threshold must auto-start PLAYING"
        assert video._buffer_gate is False
        assert video._is_playing is True
        removed = [c[0][0] for c in container.remove.call_args_list]
        assert spinner in removed, "spinner removed when playback starts"

    def test_async_done_without_buffering_starts_play(
        self, mock_container, built_video
    ):
        """Local file / fast link: no BUFFERING below threshold, so preroll
        (ASYNC_DONE) releases the gate — video never gets stuck spinning."""
        from gnostr.renderer import VideoPlayer

        video, pipeline, handler, _, _ = built_video
        VideoPlayer.load_and_play(
            "http://t.test/local.mp4", mock_container, None, autoplay=True
        )
        pipeline.set_state.reset_mock()

        handler(_msg(_ASYNC_DONE))

        assert 1 in _states(pipeline)
        assert video._buffer_gate is False

    def test_buffering_tuple_shape_binding_robustness(
        self, mock_container, built_video
    ):
        """Bindings returning (percent, time_left) must gate identically."""
        from gnostr.renderer import VideoPlayer

        video, pipeline, handler, _, _ = built_video
        VideoPlayer.load_and_play(
            "http://t.test/tuple.mp4", mock_container, None, autoplay=True
        )
        pipeline.set_state.reset_mock()

        handler(_msg(_BUFFERING, 30, tuple_shape=True))

        assert 1 not in _states(pipeline), "tuple-shape 30% must not start play"
        assert video._buffer_pct == 30

        pipeline.set_state.reset_mock()
        handler(_msg(_BUFFERING, 80, tuple_shape=True))
        assert 1 in _states(pipeline), "tuple-shape threshold releases the gate"

    def test_user_tap_bypasses_gate(self, mock_container, built_video, monkeypatch):
        from gnostr.renderer import VideoPlayer

        video, pipeline, _, _, container = built_video
        VideoPlayer.load_and_play(
            "http://t.test/tap.mp4", mock_container, None, autoplay=True
        )
        pipeline.set_state.reset_mock()
        # Gtk.Picture is a Mock in this suite, so _find_video's isinstance can't
        # run — stub the lookup the same way the real feed resolves the widget.
        monkeypatch.setattr(VideoPlayer, "_find_video", staticmethod(lambda c: video))

        VideoPlayer.toggle_play(container)

        assert video._buffer_gate is False
        assert 1 in _states(pipeline), "deliberate tap plays through the gate"


class TestMidPlaybackDrain:
    def _start_playing(self, built_video, mock_container):
        from gnostr.renderer import VideoPlayer

        video, pipeline, handler, _, _ = built_video
        VideoPlayer.load_and_play(
            "http://t.test/drain.mp4", mock_container, None, autoplay=True
        )
        handler(_msg(_BUFFERING, 80))
        pipeline.set_state.reset_mock()
        return video, pipeline, handler

    def test_drain_pauses_then_resumes(self, mock_container, built_video):
        video, pipeline, handler = self._start_playing(built_video, mock_container)

        handler(_msg(_BUFFERING, 15))  # stream drains mid-playback
        states_after_drain = _states(pipeline)
        assert 2 in states_after_drain, "drain must park in PAUSED"
        assert video._auto_paused is True
        assert video._is_playing is False

        pipeline.set_state.reset_mock()
        handler(_msg(_BUFFERING, 90))  # buffer recovers
        assert 1 in _states(pipeline), "recovered buffer must resume PLAYING"
        assert video._auto_paused is False
        assert video._is_playing is True

    def test_unknown_percent_minus_one_never_drain_pauses(
        self, mock_container, built_video
    ):
        """BUFFERING pct=-1 (unknown) must not park a playing video."""
        video, pipeline, handler = self._start_playing(built_video, mock_container)
        pipeline.set_state.reset_mock()

        handler(_msg(_BUFFERING, -1))

        assert 2 not in _states(pipeline), "pct=-1 is unknown, not a drain"
        assert video._auto_paused is False
        assert video._is_playing is True

    def test_drain_shows_buffering_row(self, mock_container, built_video):
        import sys

        video, pipeline, handler = self._start_playing(built_video, mock_container)
        handler(_msg(_BUFFERING, 10))

        mock_gtk = sys.modules["gi.repository.Gtk"]
        assert video._buffer_row is mock_gtk.Box.return_value
        video._buffer_row_label.set_text.assert_called_with("Buffering 10%")
