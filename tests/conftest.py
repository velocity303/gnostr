"""
Global test configuration and fixtures.
"""

import pytest
from unittest.mock import MagicMock, patch, Mock
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gst


@pytest.fixture(scope="module", autouse=True)
def mock_renderer_modules():
    """Mock GStreamer and GTK before renderer module is imported."""
    # Patch the src.renderer module's Gst and Gtk before any import
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
