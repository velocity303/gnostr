# src/service/profile_service.py (Facade)
import json
from typing import Optional, Dict
from gnostr.src.gateway.gateway import IKeyValueStore, IActivityAggregationGateway

# Explicitly import the two specialized services we have implemented and tested independently
from gnostr.service.profile_metadata_service import ProfileMetadataService
from gnostr.service.activity_service import ActivityService

class ProfileService:
    """
    The Facade service layer component for user profile data. 
    It coordinates calls to specialized metadata and activity services, 
    executing the business logic that previously resided in this single class.
    This pattern ensures the View/UI only interacts with one cohesive entry point, 
    while relying on distinct, decoupled domain services underneath.
    """
    def __init__(self, kv_repo: IKeyValueStore, activity_gateway: IActivityAggregationGateway):
        # The constructor is responsible for initializing and injecting all required dependencies.
        # We pass the repositories needed by the sub-services.
        metadata_service = ProfileMetadataService(kv_repo=kv_repo) 
        activity_service = ActivityService(db_repo=activity_gateway)
        
        self._metadata_service = metadata_service
        self._activity_service = activity_service

    def get_user_view(self, pubkey: str) -> Optional[Dict]:
        """
        The primary public method. Fetches and composites all necessary profile information 
        by delegating to specialized components (Metadata for Identity, Activity for Feed).
        This composition is the core output of this facade service.
        """
        print(f"Service Log: Composing full user profile view for {pubkey} using Facade Pattern.")

        # 1. Fetch Core Metadata (Identity)
        metadata = self._metadata_service.get_metadata(pubkey)
        if metadata is None:
            return None # Critical failure: Cannot build a profile without core identity.

        # We must pass the gateway to the ActivityService, as it encapsulates DB interaction
        activity_results = self._activity_service.get_recent_activity(pubkey)
        
        # 2. Composition: Merge results into the final single view object
        full_view = metadata.copy() # Start with core profile info
        if activity_results:
            full_view['recent_posts'] = activity_results 
        else:
            full_view['recent_posts'] = []
            
        return full_view

    def update_following_status(self, current_user_pubkey: str, target_pubkey: str, status: str):
        """
        Placeholder method for state change operations. Maintains the façade pattern 
        for operational logic not covered by simple fetching calls.
        """
        print(f"Service Log: Executing operational changes for {target_pubkey} ({status}).")
        # Future expansion point for Relationship Management service integration.
