"""
Global test configuration and fixtures.
"""

import sys
from unittest.mock import Mock
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gst


# Pre-import patching: ensure src.renderer imports with mocked Gst/Gtk
def _setup_renderer_mocks():
    """Mock GStreamer and GTK before renderer module is imported."""
    mock_gst = Mock()
    mock_gst.parse_launch.return_value = Mock()
    mock_gst.State = Mock(PLAYING=1)
    mock_gst.Format = Mock(TIME=1)
    mock_gst.SeekFlags = Mock(FLUSH=1)
    mock_gst.MessageType = Mock(EOS=1, ERROR=2)
    mock_gst.parse_error.return_value = (Mock(message="test error"), "debug")

    mock_gtk = Mock()
    mock_box_instance = Mock()
    mock_box_instance.get_children.return_value = []
    mock_box_instance.append.return_value = None
    mock_gtk.Box.return_value = mock_box_instance
    mock_gtk.Picture.return_value = Mock()
    mock_gtk.Spinner.return_value = Mock()

    sys.modules['src.renderer.Gst'] = mock_gst
    sys.modules['src.renderer.Gtk'] = mock_gtk

    return mock_gst, mock_gtk


# Run setup immediately
mock_gst, mock_gtk = _setup_renderer_mocks()


def pytest_configure():
    """Register the mock modules with pytest."""
    pass
