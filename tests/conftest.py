import pytest
from unittest.mock import MagicMock


@pytest.fixture
def mock_db():
    mock = MagicMock()
    mock.find_events.return_value = []
    return mock


@pytest.fixture
def mock_kv():
    mock = MagicMock()
    mock.get_key.return_value = None
    return mock
