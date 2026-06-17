import pytest
from unittest.mock import MagicMock, patch
from src.service.profile_service import ProfileService


@pytest.fixture(scope="function")
def profile_service(mock_db: MagicMock, mock_kv: MagicMock):
    """Provides a fresh instance of the ProfileService using mocked repositories."""
    return ProfileService(kv_repo=mock_kv, db_repo=mock_db)


def test_get_profile_success_happy_path(profile_service: MagicMock):
    MOCK_PUBKEY = "fakepub123"
    MOCK_NAME = "TestUser"

    mock_kv_instance = MagicMock()
    mock_kv_instance.get_key.return_value = '{"display_name": "' + MOCK_NAME + '", "picture": "url/to/pic.jpg"}'
    profile_service._metadata_service._kv = mock_kv_instance

    mock_db_instance = MagicMock()
    mock_db_instance.find_events.return_value = [
        {"id": "e1", "pubkey": MOCK_PUBKEY, "content": "Test post 1.", "created_at": 12345}
    ]
    profile_service._db = mock_db_instance

    profile = profile_service.get_full_profile(MOCK_PUBKEY)

    assert profile is not None
    assert profile["display_name"] == MOCK_NAME
    assert 'recent_posts' in profile
    assert len(profile['recent_posts']) == 1

    mock_kv_instance.get_key.assert_called_once_with(f"profile:{MOCK_PUBKEY}")
    mock_db_instance.find_events.assert_called_once_with({"authors": [MOCK_PUBKEY], "kind": 1}, limit=5)


def test_get_profile_missing_key_fails(profile_service: MagicMock):
    """Test the failure path when core profile data cannot be loaded."""
    MOCK_PUBKEY = "fakepub-fail"

    mock_kv_instance = MagicMock()
    mock_kv_instance.get_key.return_value = None
    profile_service._metadata_service._kv = mock_kv_instance

    profile = profile_service.get_full_profile(MOCK_PUBKEY)

    assert profile is None
    mock_kv_instance.get_key.assert_called_once()


def test_profile_update_succeeds_and_writes_gateway(profile_service: MagicMock):
    """Test that the service layer correctly coordinates updating a user's bio via the gateway."""
    MOCK_PUBKEY = "testuserpubkey"
    NEW_NAME = "New Modular Name"
    NEW_BIO = "Profile updated successfully using the ProfileService."

    mock_kv_instance = MagicMock()
    profile_service._kv = mock_kv_instance

    profile_service._save_profile_data(MOCK_PUBKEY, {"display_name": NEW_NAME, "bio": NEW_BIO})

    mock_kv_instance.set_key.assert_called_once()
    args, _ = mock_kv_instance.set_key.call_args
    assert args[0] == f"profile:{MOCK_PUBKEY}"
    assert args[1].startswith('{')