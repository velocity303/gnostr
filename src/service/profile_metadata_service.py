# src/service/profile_metadata_service.py
import json
from typing import Optional, Dict
from src.gateway.gateway import IKeyValueStore


class ProfileMetadataError(Exception):
    """Custom exception for profile metadata lookup errors."""
    pass


class ProfileMetadataService:
    """
    Dedicated service layer component responsible solely for retrieving,
    validating, and structuring immutable core user profile data.

    Its job is to coordinate the reading of fundamental properties (name, bio)
    from the Key/Store repository.
    """
    def __init__(self, kv_repo: IKeyValueStore):
        self._kv = kv_repo

    def get_metadata(self, pubkey: str) -> Optional[Dict]:
        """
        Fetches a comprehensive profile snapshot from core key-value stores
        for the given public key.

        Returns: A dictionary containing base profile data on success, None otherwise.
        Raises: ProfileMetadataError if internal data structures are corrupted.
        """
        print(f"Service Log: Starting mandatory metadata fetch for {pubkey} using KV Store.")

        # 1. Fetch core profile data by accessing the 'profile' key prefix
        metadata_json = self._kv.get_key(f"profile:{pubkey}")
        if not metadata_json:
            return None

        try:
            profile = json.loads(metadata_json)
        except json.JSONDecodeError as e:
            raise ProfileMetadataError(f"Failed to decode JSON for pubkey {pubkey}: {e}")

        # The returned dictionary should ONLY contain core profile fields
        return profile
