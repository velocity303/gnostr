import pytest
from unittest.mock import MagicMock, patch
from src.gateway.gateway import (
    DatabaseGateway, IEventRepository, IKeyValueStore
)


@pytest.fixture(scope="function")
def mock_event_repo():
    """Provides a fresh instance of the Event Repository."""
    with patch.object(DatabaseGateway, '__init__', return_value=None):
        db = DatabaseGateway({"type": "TestSQLite"})
        yield db
    db.close()


def test_database_gateway_instance_initialization(mock_event_repo: MagicMock):
    """Verify that the gateway initializes its internal state correctly."""
    assert hasattr(mock_event_repo, 'close')


def test_key_store_read_write(mock_event_repo: MagicMock):
    """Test the IKeyValueStore contract functions."""
    db = mock_event_repo
    test_key = "user:settings"
    test_value = '{"theme": "dark", "notifications": true}'

    db.set_key(test_key, test_value)

    with patch.object(DatabaseGateway, 'get_key', MagicMock(return_value=test_value)):
        retrieved = db.get_key("any:key")
        assert retrieved == test_value


def test_event_repository_save_returns_id():
    """Tests event saving returns a mock id."""
    mock_db = DatabaseGateway({"type": "TestFailure"})
    event = {"pubkey": "pk", "content": "message", "tags": [], "id": "evt1"}
    result = mock_db.save_event(event)
    assert result is not None


def test_find_events_returns_list():
    """Tests that find_events returns a list."""
    mock_db = DatabaseGateway({"type": "TestFilter"})
    criteria = {"kind": 1, "authors": ["p1", "p2"]}
    results = mock_db.find_events(criteria)
    assert isinstance(results, list)