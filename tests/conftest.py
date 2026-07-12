import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(scope="function")
def mock_db():
    """Provides a fresh instance of the DatabaseGateway."""
    mock = MagicMock()
    mock.find_events.return_value = []
    return mock


@pytest.fixture(scope="function")
def mock_kv():
    """Provides a fresh instance of the KeyValueStore."""
    mock = MagicMock()
    mock.get_key.return_value = None
    return mock
