# feed_service.py
from typing import Dict, Any, List
# We assume we have a repository contract for fetching or saving activity streams
# Must implement IFeedRepository in our Gateway layer before this is functional
# from gnostr.src.gateway.gateway import AbstractRepository 
# Replace this with the actual concrete gateway implementation once defined:

class FeedService:
    """
    Coordinates event processing logic, handling raw events received from the Nostr Client. 
    This service translates low-level data (an Event object) into high-level application state changes 
    (e.g., "increment count of X", "update user avatar"). It is the primary business logic layer for feeds.
    """
    def __init__(self, feed_repo):
        # Injecting dependency on a repository that handles fetching/saving complex feed states (e.g., subscriptions)
        self._feed_repository = feed_repo 

    def process_new_event(self, sender: Any, raw_event: Dict) -> List[Dict]:
        """
        CORE LOGIC: Processes a newly received raw event and determines its impact on the user's view.
        
        Args:
            sender: The user or external entity that sent the message (source pubkey).
            raw_event: The structured dictionary of the incoming Nostr Event.

        Returns:
            A list of dictionaries, where each dict represents a necessary UI/State update 
            (e.g., {"type": "NEW_POST", "data": ...}, {"type": "USER_UPDATE", "pubkey": ...}).
        """
        print(f"Service Log: Processing incoming event from {sender}...")

        # 1. Preliminary Validation (The first safety check)
        if raw_event.get('kind') not in [1, 2]: # Only process Status/Event updates for feed generation
            print("INFO: Event kind is not handled by the FeedService.")
            return []

        updates = []
        
        # 2. Core Business Rule Logic (Example: Determining if a post needs to be shown)
        if raw_event['kind'] == 1: # Status
            # Check for content presence, format validation, etc.
            content = raw_event.get('content', '').strip()
            if not content:
                print("WARNING: Received blank status event.")
                raw_event['processed'] = "BLANK" # Tagging it as processed/ignored
                updates.append({"type": "IGNORED_EVENT", "data": raw_event})
                return updates

            # Check cross-cutting concerns (e.g., is the content too long? does it contain specific keywords?)
            if len(content) > 2048:
               print("WARNING: Content exceeds length limit.")
               raw_event['processed'] = "TRUNCATED"
            updates.append({"type": "NEW_POST", "data": raw_event})

        # 3. State Management and side effects (The 'Repository' aspect)
        # We use the repository layer to update meta-info about the stream processing itself.
        if updates:
            # Example: Persist that we successfully saw this event for metrics/auditing
            self._feed_repository.save_processed_event(raw_event, success=True)

        return updates


    def process_status_update(self, sender: Any, status_event: Dict):
        """Handles specific events related to user presence or identity changes."""
        print("Service Log: Processing targeted status update...")
        # Example logic: Check for picture URL in event. Adjust local cache if found.
        if "picture" in status_event:
            return {"type": "USER_PROFILE_UPDATE", "data": status_event}
        return []

    def process_contacts_update(self, sender: Any, contacts_event: Dict):
        """Handles changes to the user's contact list or local subscription status."""
        print("Service Log: Processing contact management update.")
        # This interacts with IKeyValueStore via self._feed_repository (if we expand its scope)
        return []

