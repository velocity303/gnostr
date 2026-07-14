"""
Root conftest.py - MUST load before any tests import gnostr modules.
"""

import sys
import os
from unittest.mock import Mock

# CRITICAL: Mock gi BEFORE any other imports that might trigger renderer.py
mock_gi = Mock()
mock_gi.require_version = lambda x, y: None
mock_gi.Gst = Mock()
mock_gi.Gst.State = Mock(PLAYING=1)
mock_gi.Gst.Format = Mock(TIME=1)
mock_gi.Gst.SeekFlags = Mock(FLUSH=1)
mock_gi.Gst.MessageType = Mock(EOS=1, ERROR=2)
mock_gi.Gtk = Mock()
mock_gi.Adw = Mock()
mock_gi.GLib = Mock()
mock_gi.Gdk = Mock()
mock_gi.GdkPixbuf = Mock()
mock_gi.Pango = Mock()

# Patch the actual import path used by renderer.py
sys.modules["gi"] = mock_gi
sys.modules["gi.repository"] = mock_gi
sys.modules["gi.repository.Gst"] = mock_gi.Gst
sys.modules["gi.repository.Gtk"] = mock_gi.Gtk
sys.modules["gi.repository.Adw"] = mock_gi.Adw
sys.modules["gi.repository.GLib"] = mock_gi.GLib
sys.modules["gi.repository.Gdk"] = mock_gi.Gdk
sys.modules["gi.repository.GdkPixbuf"] = mock_gi.GdkPixbuf
sys.modules["gi.repository.Pango"] = mock_gi.Pango

# Set up mocks for Gst and Gtk
mock_gst = mock_gi.Gst
mock_gtk = mock_gi.Gtk

mock_box_instance = Mock()
mock_box_instance.get_children.return_value = [Mock()]
mock_box_instance.append.return_value = None
mock_gtk.Box.return_value = mock_box_instance
mock_gtk.Picture.return_value = Mock()
mock_gtk.Spinner.return_value = Mock()

mock_gst.parse_launch.return_value = Mock()
mock_gst.State = Mock(PLAYING=1)
mock_gst.Format = Mock(TIME=1)
mock_gst.SeekFlags = Mock(FLUSH=1)
mock_gst.MessageType = Mock(EOS=1, ERROR=2)
mock_gst.parse_error.return_value = (Mock(message="test error"), "debug")

# Add src to PYTHONPATH so gnostr package is importable
project_root = os.path.dirname(os.path.abspath(__file__))
src_path = os.path.join(project_root, "src")
if src_path not in sys.path:
    sys.path.insert(0, src_path)

# Ensure the gnostr package is importable
# This must come AFTER mocking gi to avoid import errors
try:
    import gnostr
except Exception as e:
    print(f"Warning: Could not import gnostr during conftest setup: {e}")

# Fixture to ensure gi is mocked
import pytest


@pytest.fixture(autouse=True)
def ensure_gi_mocked():
    """Ensure gi is mocked for all tests."""
    pass


@pytest.fixture(scope="function")
def mock_renderer_modules():
    """Provides mock renderer modules for video player tests."""
    return (mock_gi.Gst, mock_gi.Gtk)


@pytest.fixture(scope="function")
def mock_db():
    """Provides a mock database repository."""
    return Mock()


@pytest.fixture(scope="function")
def mock_kv():
    """Provides a mock key-value store."""
    return Mock()
