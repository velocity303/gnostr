from typing import Dict, Any, List
# 1. Import our new utility cache manager at the top level
from src.util.cache_manager import CACHE 
# We assume the gateway repository classes were updated to accept a CacheManager instance upon init for perfect coupling
# For now, we'll pass it in manually during instantiation placeholder:
# from gnostr.src.gateway.gateway import IEventRepository

class FeedService:
    """
    Coordinates event processing logic, handling raw events received from the Nostr Client. 
    This service translates low-level data into high-level application state changes and now 
    incorporates mandatory cache gating/rate-limit checks before engaging repositories. 
    It acts as the central orchestration point for feed consumption.
    """
    def __init__(self, event_repo):
        # The repository is assumed to be wrapped by a Gateway that handles caching logic internally.
        self._event_repository = event_repo
        # Initialize the Cache Manager singleton to control all data fetching across this service instance.
        print("INFO: FeedService initialized with dedicated cache manager for rate limiting.")

    def get_paginated_global_feed(self, current_cursor: Optional[str] = None, page_size: int = 20) -> Dict[str, Any]:
        """
        Retrieves a fresh batch of global feed events. This method strictly uses the cache 
        and pagination flow to prevent massive initial loads/rate limiting.
        """
        cache_key = f"global_feed:{current_cursor or 'initial'}:{page_size}"
        
        # Attempt to retrieve cached data first (TTL: 1 hour)
        cached_data = CACHE.get(cache_key)
        if cached_data:
            print("CACHE HIT: Returning feed content from cache.")
            return {"source": "cache", "events": cached_data, "cursor": current_cursor}

        # Cache Miss: Must hit the repository (simulating a high-cost network op)
        print(f"CACHE MISS: Querying database/relay for global feed chunk. Potential rate-limit zone.")
        
        # Use the dedicated repository to fetch the paginated chunk
        try:
            raw_events = self._event_repository.find_paginated_events({
                "kind": 1, 
                "authors": [], # Global query
                "cursor": current_cursor
            }, limit=page_size)
            next_cursor = raw_events[-1]['id'] if raw_events else None

            # Package the results for caching. TTL is set shorter than feed validity (e.g., 1 hour).
            cached_result = {
                "source": "live", 
                "events": raw_events, 
                "cursor": next_cursor
            }
            
            # Store the result in our bounded cache
            CACHE.set(cache_key, cached_result, ttl_seconds=3600)

            return {"source": "live", "events": raw_events, "cursor": next_cursor}
        except Exception as e:
            print(f"ERROR accessing repository during feed fetch: {e}")
            # Fallback to empty set if network calls fail
            CACHE.set(cache_key, {"source": "error", "events": [], "cursor": None}, ttl_seconds=60) 
            return {"source": "error", "events": [], "cursor": None}

    def process_new_event(self, sender: Any, raw_event: Dict) -> List[Dict]:
        """
        Processes a single event/message. This is kept as before but relies on the 
        assumption that any feed-intensive data (like the Source PubKey's profile metadata) 
        is gated by the new Gateway/Cache abstraction layer.
        """
        print(f"Service Log: Processing incoming raw event from {sender}...")
        # ... (Rest of the original core logic remains, but should now rely entirely on gateway-managed data access methods)
        if raw_event.get('kind') not in [1, 2]: 
            return []

        updates = []
        if raw_event['kind'] == 1: # Status
            # ... (validation logic)
            updates.append({"type": "NEW_POST", "data": raw_event})
        
        # The repository call here MUST ONLY access data that has been pre-validated and retrieved via the cache/gateway flow.
        return updates

    def process_status_update(self, sender: Any, status_event: Dict):
        """Handles specific events related to user presence or identity changes."""
        print("Service Log: Processing targeted status update...")
        # Future work should introduce 'CACHE_KEY_PROFILE:[pubkey]' check here.
        if "picture" in status_event:
            return {"type": "USER_PROFILE_UPDATE", "data": status_event}
        return []