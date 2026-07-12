"""
Global test configuration and fixtures.
"""

import pytest
from unittest.mock import Mock, MagicMock


@pytest.fixture(scope="module", autouse=True)
def mock_renderer_modules(monkeypatch):
    """Mock GStreamer and GTK before renderer module is imported."""
    # Create mock for Gst
    mock_gst = Mock()
    mock_gst.parse_launch.return_value = Mock()
    mock_gst.State = Mock(PLAYING=1)
    mock_gst.Format = Mock(TIME=1)
    mock_gst.SeekFlags = Mock(FLUSH=1)
    mock_gst.MessageType = Mock(EOS=1, ERROR=2)
    mock_gst.parse_error.return_value = (Mock(message="test error"), "debug")

    # Create mock for Gtk
    mock_gtk = Mock()
    mock_box_instance = Mock()
    mock_box_instance.get_children.return_value = []
    mock_box_instance.append.return_value = None
    mock_gtk.Box.return_value = mock_box_instance
    mock_gtk.Picture.return_value = Mock()
    mock_gtk.Spinner.return_value = Mock()

    # Patch sys.modules
    mock_gi = Mock()
    mock_gi.require_version = lambda x, y: None
    mock_gi.Gst = mock_gst
    mock_gi.Gtk = mock_gtk

    import sys
    sys.modules["gi.repository"] = mock_gi
    sys.modules["gi"] = mock_gi

    # Patch src.renderer
    sys.modules["src.renderer.Gst"] = mock_gst
    sys.modules["src.renderer.Gtk"] = mock_gtk

    yield mock_gst, mock_gtk


@pytest.fixture
def mock_db():
    """Provides a mock database repository."""
    return MagicMock()


@pytest.fixture
def mock_kv():
    """Provides a mock key-value store."""
    return MagicMock()
