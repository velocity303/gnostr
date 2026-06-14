# gnostr unit tests utility file
# This module holds reusable fixtures and mocks for the gnostr components.

import pytest
from unittest.mock import MagicMock, patch
from gnostr.gnostr.utils import bech32_encode, bech32_decode # Assuming package structure is 'gnostr.gnostr.utils'

# --- Pytest Fixtures ---

@pytest.fixture(scope="session")
def mock_database():
    """Mocks the Database class for session-level testing."""
    with patch('gnostr.gnostr.core.database.Database') as MockDB:
        instance = MockDB.return_value
        # Simulate a lightweight, mockable DB object
        instance.get_event_by_id.return_value = None

        yield instance
        MockDB.reset_mock()

@pytest.fixture(scope="session")
def mock_key_manager():
    """Mocks the KeyManager class for session-level testing."""
    with patch('gnostr.gnostr.core.key_manager.KeyManager') as MockKM:
        instance = MockKM.return_value
        instance.load_key.return_value = None # Simulate no stored key initially

        yield instance
        MockKM.reset_mock()

@pytest.fixture(scope="module")
def mock_network_client():
    """Mocks the NostrClient for module-level testing."""
    with patch('gnostr.gnostr.core.client.NostrClient') as MockNC:
        instance = MockNC.return_value
        # Add helper methods to the fixture's instance mock for convenience
        instance.connect.return_value = None

        yield instance
        MockNC.reset_mock()

@pytest.fixture(scope="module")
def mock_image_loader():
    """Mocks ImageLoader dependency."""
    with patch('gnostr.gnostr.core.renderer.ImageLoader') as MockIL:
        MockIL.MAX_WIDTH = 800 # Set a default value we expect to use
        yield MockIL

