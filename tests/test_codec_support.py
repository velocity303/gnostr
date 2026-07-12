"""
Comprehensive tests for codec support and video playback.
"""

import pytest
from unittest.mock import MagicMock, patch, Mock

"""
Comprehensive tests for codec support and video playback.
"""

import pytest
from unittest.mock import MagicMock, patch, Mock
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gst

# Import the modules under test AFTER setting up mocks
# This ensures the renderer module imports with mocked GStreamer/ GTK


@pytest.fixture(scope="module", autouse=True)
def mock_renderer_modules():
    """Mock GStreamer and GTK before renderer module is imported."""
    with patch("src.renderer.Gst") as mock_gst, patch("src.renderer.Gtk") as mock_gtk:
        mock_gst.parse_launch.return_value = Mock()
        mock_gst.State = Mock(PLAYING=1)
        mock_gst.Format = Mock(TIME=1)
        mock_gst.SeekFlags = Mock(FLUSH=1)
        mock_gst.MessageType = Mock(EOS=1, ERROR=2)
        mock_gst.parse_error.return_value = (Mock(message="test error"), "debug")

        # Mock Gtk.Box to have get_children() method for GTK4 compatibility
        mock_box_instance = Mock()
        mock_box_instance.get_children.return_value = []
        mock_box_instance.append.return_value = None
        mock_gtk.Box.return_value = mock_box_instance

        mock_gtk.Picture.return_value = Mock()
        mock_gtk.Spinner.return_value = Mock()
        yield mock_gst, mock_gtk


# Import the modules under test AFTER mocks are in place
from src.renderer import ContentRenderer, VideoPlayer


@pytest.fixture(scope="function")
def mock_container():
    """Mock container for video player tests."""
    container = Mock()
    container.get_size_request.return_value = (-1, 200)
    container.get_parent.return_value = None
    return container


class TestCodecSupport:
    """Tests for codec detection and video URL handling."""

    def test_is_video_url_mp4(self):
        """Test MP4 URL detection."""
        url = "https://example.com/video.mp4"
        assert ContentRenderer.is_video_url(url) is True

    def test_is_video_url_mov(self):
        """Test MOV URL detection."""
        url = "https://example.com/video.mov"
        assert ContentRenderer.is_video_url(url) is True

    def test_is_video_url_webm(self):
        """Test WebM URL detection."""
        url = "https://example.com/video.webm"
        assert ContentRenderer.is_video_url(url) is True

    def test_is_video_url_gif(self):
        """Test GIF URL detection."""
        url = "https://example.com/animation.gif"
        assert ContentRenderer.is_video_url(url) is True

    def test_is_video_url_jpg(self):
        """Test JPG URL is not a video."""
        url = "https://example.com/image.jpg"
        assert ContentRenderer.is_video_url(url) is False

    def test_is_video_url_png(self):
        """Test PNG URL is not a video."""
        url = "https://example.com/image.png"
        assert ContentRenderer.is_video_url(url) is False

    def test_is_video_url_empty(self):
        """Test empty string handling."""
        url = ""
        assert ContentRenderer.is_video_url(url) is False

    def test_is_video_url_none(self):
        """Test None handling."""
        url = None
        try:
            ContentRenderer.is_video_url(url)
        except Exception:
            pass  # Expected to handle gracefully


class TestVideoPlayer:
    """Tests for VideoPlayer functionality."""

    def test_load_and_play_success(self, mock_gst, mock_gtk, mock_container):
        """Test successful video player initialization."""
        # Mock pipeline and bus
        mock_pipeline = Mock()
        mock_bus = Mock()
        mock_pipeline.get_bus.return_value = mock_bus
        mock_pipeline.get_by_name.return_value = Mock(get_property=lambda x: Mock())
        mock_gst.parse_launch.return_value = mock_pipeline

        # Mock video widget
        mock_video = Mock()
        mock_gtk.Picture.return_value = mock_video

        # Mock Gst.State.PLAYING
        mock_gst.State.PLAYING = 1

        # Call load_and_play
        VideoPlayer.load_and_play("http://test.com/video.mp4", mock_container, None)

        # Verify pipeline was created
        mock_gst.parse_launch.assert_called_once()

        # Verify pipeline state was set to PLAYING
        mock_pipeline.set_state.assert_called_with(1)

    def test_load_and_play_failure(self, mock_gst, mock_gtk, mock_container):
        """Test video player initialization failure."""
        # Force parse_launch to raise
        mock_gst.parse_launch.side_effect = Exception("Test error")

        # Call should not crash
        VideoPlayer.load_and_play("http://test.com/video.mp4", mock_container, None)

        # Verify pipeline was attempted
        mock_gst.parse_launch.assert_called_once()


class TestContentRenderer:
    """Tests for content rendering with video support."""

    def test_render_video_url(self):
        """Test rendering a video URL in content."""
        content = "Check this out: https://example.com/video.mp4"
        window_ref = Mock()
        box = ContentRenderer.render(content, window_ref)

        # Should return a box with video widget
        assert isinstance(box, Gtk.Box)
        # The box should contain at least one child (the video or text)
        # GTK4 uses append() but children are accessed via get_children()
        assert len(box.get_children()) > 0

    def test_render_mixed_content(self):
        """Test rendering mixed text and video URLs."""
        content = "Text before https://example.com/video.mp4 text after"
        window_ref = Mock()
        box = ContentRenderer.render(content, window_ref)

        assert isinstance(box, Gtk.Box)

    def test_render_no_video(self):
        """Test rendering content without video URLs."""
        content = "Just plain text"
        window_ref = Mock()
        box = ContentRenderer.render(content, window_ref)

        assert isinstance(box, Gtk.Box)

    def test_render_with_profile_video(self):
        """Test rendering profile picture that is a video."""
        profile = {"picture": "https://example.com/animated.gif", "name": "Test User"}
        window_ref = Mock()
        window_ref.db = Mock()
        window_ref.db.get_profile.return_value = profile

        # Simulate profile card rendering
        from src.renderer import ContentRenderer

        # This would trigger the video detection in profile card
        box = ContentRenderer.render("Test", window_ref)

        assert isinstance(box, Gtk.Box)
