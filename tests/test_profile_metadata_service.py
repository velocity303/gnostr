import pytest
from unittest.mock import MagicMock
from src.gateway.gateway import IKeyValueStore
from src.service.profile_metadata_service import ProfileMetadataService, ProfileMetadataError


@pytest.fixture
def mock_key_store():
    return MagicMock(spec=IKeyValueStore)


class TestProfileMetadataService:

    def test_metadata_retrieval_success(self, mock_key_store):
        mock_key_store.get_key.return_value = '{"pubkey": "testpub", "name": "Jane Doe", "bio": "DevOps enthusiast.", "picture_url": "http://pic.com/jane"}'

        service = ProfileMetadataService(kv_repo=mock_key_store)
        profile = service.get_metadata("testpub")

        assert profile is not None
        assert profile["name"] == "Jane Doe"
        assert "recent_posts" not in profile

    def test_metadata_retrieval_key_not_found(self, mock_key_store):
        mock_key_store.get_key.return_value = None

        service = ProfileMetadataService(kv_repo=mock_key_store)
        profile = service.get_metadata("nonexistentpub")

        assert profile is None

    def test_metadata_corruption_raises_error(self, mock_key_store):
        mock_key_store.get_key.return_value = '{"name": "corrupted", "bio": "data"'

        service = ProfileMetadataService(kv_repo=mock_key_store)
        with pytest.raises(ProfileMetadataError):
            service.get_metadata("corruptpub")