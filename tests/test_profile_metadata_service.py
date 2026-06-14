# tests/test_profile_metadata_service.py
import pytest
from unittest.mock import MagicMock
from gnostr.src.gateway.gateway import IKeyValueStore # Assuming this interface is available for mocking
from src.service.profile_metadata_service import ProfileMetadataService

@pytest.fixture
def mock_key_store():
    # Provides a mocked instance of the core key-value repository contract
    return MagicMock(spec=IKeyValueStore)

class TestProfileMetadataService:
    \"\"\"Unit tests for ProfileMetadataService, validating metadata retrieval.\"\"\"

    def test_metadata_retrieval_success(self, mock_key_store):
        # SETUP MOCK DATA: A typical profile object stored as JSON string
        mock_key_store.get_key.return_value = '{"pubkey": "testpub", "name": "Jane Doe", "bio": "DevOps enthusiast.", "picture_url": "http://pic.com/jane"}'

        # ACT: Instantiate the service with the mock dependency
        service = ProfileMetadataService(db_repo=MagicMock(), kv_repo=mock_key_store)
        profile = service.get_metadata("testpub")

        # ASSERT: Verify the correct data was processed and returned
        assert profile is not None
        assert profile["name"] == "Jane Doe"
        assert "recent_posts" not in profile # Key validation: must NOT include activity data

    def test_metadata_retrieval_key_not_found(self, mock_key_store):
        # SETUP MOCK DATA: The key does not exist
        mock_key_store.get_key.return_value = None

        # ACT
        service = ProfileMetadataService(db_repo=MagicMock(), kv_repo=mock_key_store)
        profile = service.get_metadata("nonexistentpub")

        # ASSERT: Expect graceful handling of missing data
        assert profile is None

    def test_metadata_corruption_handling(self, mock_key_store):
        # SETUP MOCK DATA: The key exists but the content is invalid JSON
        mock_key_store.get_key.return_value = '{"name": "corrupted", "bio": "data"' # Missing closing brace

        # ACT
        service = ProfileMetadataService(db_repo=MagicMock(), kv_repo=mock_key_store)
        profile = service.get_metadata("corruptpub")

        # ASSERT: Expect None to be returned upon JSON decoding failure, fulfilling resilience requirement.
        assert profile is None