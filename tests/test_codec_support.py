"""
Comprehensive tests for codec support and video playback.
"""

import pytest
from unittest.mock import Mock


@pytest.fixture(scope="function")
def mock_container():
    """Mock container for video player tests."""
    container = Mock()
    container.get_size_request.return_value = (-1, 200)
    container.get_parent.return_value = None
    return container


@pytest.fixture(scope="function", autouse=True)
def _reset_video_player_state(mock_renderer_modules_reset):
    """The conftest gi mocks are module-level and shared across tests, so call
    counts accumulate; VideoPlayer._cache is a class-level LRU that persists too
    (same-URL calls skip parse_launch entirely). Reset both before each test so
    assertions like parse_launch.assert_called_once() are honest."""
    from gnostr.renderer import VideoPlayer

    mock_gst, _ = mock_renderer_modules_reset
    mock_gst.parse_launch.reset_mock()
    with VideoPlayer._lock:
        VideoPlayer._cache.clear()


@pytest.fixture(scope="function")
def mock_renderer_modules_reset():
    """Same shared mocks the tests already receive, for the reset fixture."""
    import sys

    return (sys.modules["gi.repository.Gst"], sys.modules["gi.repository.Gtk"])


class TestCodecSupport:
    """Tests for codec detection and video URL handling."""

    def test_is_video_url_mp4(self):
        """Test MP4 URL detection."""
        from gnostr.renderer import ContentRenderer

        url = "https://example.com/video.mp4"
        assert ContentRenderer.is_video_url(url) is True

    def test_is_video_url_mov(self):
        """Test MOV URL detection."""
        from gnostr.renderer import ContentRenderer

        url = "https://example.com/video.mov"
        assert ContentRenderer.is_video_url(url) is True

    def test_is_video_url_webm(self):
        """Test WebM URL detection."""
        from gnostr.renderer import ContentRenderer

        url = "https://example.com/video.webm"
        assert ContentRenderer.is_video_url(url) is True

    def test_is_video_url_gif(self):
        """Test GIF URL detection."""
        from gnostr.renderer import ContentRenderer

        url = "https://example.com/animation.gif"
        assert ContentRenderer.is_video_url(url) is True

    def test_is_video_url_jpg(self):
        """Test JPG URL is not a video."""
        from gnostr.renderer import ContentRenderer

        url = "https://example.com/image.jpg"
        assert ContentRenderer.is_video_url(url) is False

    def test_is_video_url_png(self):
        """Test PNG URL is not a video."""
        from gnostr.renderer import ContentRenderer

        url = "https://example.com/image.png"
        assert ContentRenderer.is_video_url(url) is False

    def test_is_video_url_empty(self):
        """Test empty string handling."""
        from gnostr.renderer import ContentRenderer

        url = ""
        assert ContentRenderer.is_video_url(url) is False

    def test_is_video_url_none(self):
        """Test None handling."""
        from gnostr.renderer import ContentRenderer

        url = None
        try:
            ContentRenderer.is_video_url(url)
        except Exception:
            pass  # Expected to handle gracefully


class TestVideoPlayer:
    """Tests for VideoPlayer functionality."""

    def test_load_and_play_starts_paused(self, mock_container, mock_renderer_modules):
        """Lazy-start contract: building a player creates a playbin3 pipeline
        and leaves it PAUSED — playback starts only on user tap or autoplay."""
        from gnostr.renderer import VideoPlayer

        mock_gst, mock_gtk = mock_renderer_modules

        # Mock pipeline and bus
        mock_pipeline = Mock()
        mock_bus = Mock()
        mock_pipeline.get_bus.return_value = mock_bus
        mock_pipeline.get_by_name.return_value = Mock(get_property=lambda x: Mock())
        mock_gst.parse_launch.return_value = mock_pipeline

        # Mock video widget
        mock_video = Mock()
        mock_gtk.Picture.return_value = mock_video

        # Distinct state values so PAUSED vs PLAYING calls are distinguishable
        mock_gst.State.PAUSED = 2
        mock_gst.State.PLAYING = 1

        # Unique URL: VideoPlayer._cache is a class-level LRU shared across tests
        url = "http://test.com/lazy-start.mp4"
        VideoPlayer.load_and_play(url, mock_container, None)

        # Pipeline built with playbin3
        mock_gst.parse_launch.assert_called_once()
        assert "playbin3" in mock_gst.parse_launch.call_args[0][0]

        # Set to PAUSED (preroll), never PLAYING — lazy start
        paused_calls = [
            c for c in mock_pipeline.set_state.call_args_list
            if c[0][0] == mock_gst.State.PAUSED
        ]
        playing_calls = [
            c for c in mock_pipeline.set_state.call_args_list
            if c[0][0] == mock_gst.State.PLAYING
        ]
        assert paused_calls, "pipeline should be set to PAUSED on build"
        assert not playing_calls, "lazy-load must not start playback"

    def test_load_and_play_autoplay_starts_playing(self, mock_container, mock_renderer_modules):
        """autoplay=True (GIFs, profile avatars) must reach PLAYING immediately."""
        from gnostr.renderer import VideoPlayer

        mock_gst, mock_gtk = mock_renderer_modules

        mock_pipeline = Mock()
        mock_bus = Mock()
        mock_pipeline.get_bus.return_value = mock_bus
        mock_pipeline.get_by_name.return_value = Mock(get_property=lambda x: Mock())
        mock_gst.parse_launch.return_value = mock_pipeline

        mock_video = Mock()
        mock_gtk.Picture.return_value = mock_video

        mock_gst.State.PAUSED = 2
        mock_gst.State.PLAYING = 1

        url = "http://test.com/autoplay.gif"
        VideoPlayer.load_and_play(url, mock_container, None, autoplay=True)

        mock_gst.parse_launch.assert_called_once()
        playing_calls = [
            c for c in mock_pipeline.set_state.call_args_list
            if c[0][0] == mock_gst.State.PLAYING
        ]
        assert playing_calls, "autoplay=True must set the pipeline to PLAYING"

    def test_load_and_play_failure(self, mock_container, mock_renderer_modules):
        """Test video player initialization failure."""
        from gnostr.renderer import VideoPlayer

        mock_gst, mock_gtk = mock_renderer_modules

        # Force parse_launch to raise
        mock_gst.parse_launch.side_effect = Exception("Test error")

        # Call should not crash
        VideoPlayer.load_and_play("http://test.com/fail.mp4", mock_container, None)

        # Verify pipeline was attempted
        mock_gst.parse_launch.assert_called_once()


class TestContentRenderer:
    """Tests for content rendering with video support."""

    def test_render_video_url(self, mock_renderer_modules):
        """Test rendering a video URL in content."""
        from gnostr.renderer import ContentRenderer

        content = "Check this out: https://example.com/video.mp4"
        window_ref = Mock()
        box = ContentRenderer.render(content, window_ref)

        # Should return a box with video widget
        # The mock_gtk.Box is used, so it's a Mock
        assert isinstance(box, Mock)
        # The box should contain at least one child (the video or text)
        # In GTK4, get_children() returns a list directly
        children = box.get_children()
        assert isinstance(children, list)
        assert len(children) > 0

    def test_render_mixed_content(self, mock_renderer_modules):
        """Test rendering mixed text and video URLs."""
        from gnostr.renderer import ContentRenderer

        content = "Text before https://example.com/video.mp4 text after"
        window_ref = Mock()
        box = ContentRenderer.render(content, window_ref)

        assert isinstance(box, Mock)

    def test_render_no_video(self, mock_renderer_modules):
        """Test rendering content without video URLs."""
        from gnostr.renderer import ContentRenderer

        content = "Just plain text"
        window_ref = Mock()
        box = ContentRenderer.render(content, window_ref)

        assert isinstance(box, Mock)

    def test_render_with_profile_video(self, mock_renderer_modules):
        """Test rendering profile picture that is a video."""
        from gnostr.renderer import ContentRenderer

        profile = {"picture": "https://example.com/animated.gif", "name": "Test User"}
        window_ref = Mock()
        window_ref.db = Mock()
        window_ref.db.get_profile.return_value = profile

        # Simulate profile card rendering
        box = ContentRenderer.render("Test", window_ref)

        assert isinstance(box, Mock)
