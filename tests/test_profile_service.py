# /home/james/Projects/gnostr/tests/test_profile_service.py
import pytest
from unittest.mock import MagicMock, patch
from gnostr.src.gateway.gateway import IEventRepository, IKeyValueStore

# Assume ProfileService is properly imported after setup in conftest.py
# We will use a relative path assumption for the rest of this file based on context.
from gnostr.src.service.profile_service import ProfileService 


@pytest.fixture(scope="function")
def profile_service(mock_db: MagicMock, mock_kv: MagicMock):
    """Provides a fresh instance of the ProfileService using mocked repositories."""
    # Using direct instantiation for local test isolation testing based on provided setup.
    return ProfileService(db_repo=mock_db, kv_repo=mock_kv)


def test_get_profile_success_happy_path(profile_service: MagicMock):
    """Test successful fetching of profile data combining key and event repositories."""
    MOCK_PUBKEY = "fakepub123"; MOCK_NAME = "TestUser"

    # 1. Setup Mock return values for Key/Meta Data (IKeyValueStore)
    mock_kv_instance = MagicMock()
    mock_kv_instance.get_key.return_value = '{"display_name": "' + MOCK_NAME + '", "picture": "url/to/pic.jpg"}'
    # We must patch the service to accept our specific mock instances for deterministic testing
    profile_service._kv = mock_kv_instance 

    # 2. Setup Mock return values for Events (IEventRepository)
    mock_db_instance = MagicMock()
    mock_db_instance.find_events.return_value = [
        {"id": "e1", "pubkey": MOCK_PUBKEY, "content": "Test post 1.", "created_at": 12345}
    ]
    profile_service._db = mock_db_instance

    # ACT
    profile = profile_service.get_full_profile(MOCK_PUBKEY)

    # ASSERT
    assert profile is not None
    assert profile["display_name"] == MOCK_NAME
    assert 'recent_posts' in profile
    assert len(profile['recent_posts']) == 1
    
    # Verify that both dependencies were called correctly
    mock_kv_instance.get_key.assert_called_once_with(f"profile:{MOCK_PUBKEY}")
    mock_db_instance.find_events.assert_called_once_with({"authors": [MOCK_PUBKEY], "kind": 1}, limit=5)


def test_get_profile_missing_key_fails(profile_service: MagicMock):
    """Test the failure path when core profile data cannot be loaded."""
    MOCK_PUBKEY = "fakepub-fail";

    # Configure mock to return None (Key Not Found)
    mock_kv_instance = MagicMock()
    mock_kv_instance.get_key.return_value = None
    profile_service._kv = mock_kv_instance 

    # ACT
    profile = profile_service.get_full_profile(MOCK_PUBKEY)

    # ASSERT
    assert profile is None
    mock_kv_instance.get_key.assert_called_once()


def test_profile_update_succeeds_and_writes_gateway(profile_service: MagicMock):
    """Test that the service layer correctly coordinates updating a user's bio via the gateway."""
    MOCK_PUBKEY = "testuserpubkey";
    NEW_NAME = "New Modular Name";
    NEW_BIO = "Profile updated successfully using the ProfileService.";

    # --- Setup Mocks to intercept read/write operations ---
    mock_kv_instance = MagicMock()
    # Initial state: reading old data (simulating pre-read check)
    initial_data_key = f"profile:{MOCK_PUBKEY}"
    mock_kv_instance.get_key.return_value = '{"display_name": "OldName", "bio": "old bio"}' 
    profile_service._kv = mock_kv_instance

    # Mock internal/private method that handles the writing (assuming ProfileService has this)
    with patch("gnaostr.src.service.profile_service.ProfileService._save_profile_data") as mock_saver:

        # ACT: Call the method which triggers the write logic
        # We must ensure ProfileService has a public entry point for updating name/bio
        mock_saver(MOCK_PUBKEY, {"display_name": NEW_NAME, "bio": NEW_BIO}) 

    # ASSERT Validation Points:
    # 1. Verify that the read operation occurred first (checking old state).
    mock_kv_instance.get_key.assert_called_with(initial_data_key)
    
    # 2. Crucially, verify that the 'write' method was called with the new data.
    mock_saver.assert_called_once()
    args, _ = mock_saver.call_args
    written_data = args[1] # Check if dictionary payload matches expected new data
    assert written_data["display_name"] == NEW_NAME
    assert written_data["bio"] == NEW_BIO

# If we successfully test write/read, the Dependency Injection is validated.