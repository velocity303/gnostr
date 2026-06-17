# src/service/profile_service.py
import json
from typing import Optional, Dict
from src.gateway.gateway import IKeyValueStore

from src.service.profile_metadata_service import ProfileMetadataService


class ProfileService:
    def __init__(self, kv_repo: IKeyValueStore, db_repo=None):
        self._kv = kv_repo
        self._db = db_repo
        self._metadata_service = ProfileMetadataService(kv_repo=kv_repo)

    def get_full_profile(self, pubkey: str) -> Optional[Dict]:
        metadata = self._metadata_service.get_metadata(pubkey)
        if metadata is None:
            return None

        recent_posts: list = []
        if self._db:
            found = self._db.find_events({"authors": [pubkey], "kind": 1}, limit=5)
            if found:
                recent_posts = found

        full_view = metadata.copy()
        full_view['recent_posts'] = recent_posts
        return full_view

    def _save_profile_data(self, pubkey: str, data: Dict):
        self._kv.set_key(f"profile:{pubkey}", json.dumps(data))
