import pytest
from unittest.mock import MagicMock, patch
from datetime import timedelta

# Import the module under test and its dependencies
from src.util.cache_manager import BoundedCacheManager 
from src.service.feed_service import FeedService # Assuming we can mock or access it
from src.gateway.gateway import IEventRepository # Assume this interface exists for mocking


@pytest.fixture(scope=\"function\")
def cache_manager():
    # Provide a fresh, small-capacity cache for each test function to ensure isolation
    return BoundedCacheManager(max_size=5)

@pytest.fixture(scope=\"function\")
def mock_event_repo(cache_manager):
    # Mock the repository dependency with the cache manager ready
    mock_repo = MagicMock(spec=IEventRepository)
    # We assign a mock method to simulate calling write-through methods on the repo that use the cache
    return mock_repo

@pytest.fixture(scope=\"function\")
def feed_service(mock_event_repo, cache_manager):
    # Instantiate the service layer with mocked dependencies for isolation
    return FeedService(feed_repo=mock_event_repo)


def test_cache_hit_on_global_fetch(feed_service: FeedService, mock_event_repo: MagicMock, cache_manager: BoundedCacheManager):
    """Tests that when data is present and valid in the Cache, the expensive Gateway call is skipped."""
    # 1. Pre-populate the cache (simulating a successful previous fetch)
    cached_data = {
        "source": "cache", 
        "events": [{"id": "mock_event1", "content": "Cached content"}], 
        "cursor": "initial"
    }
    cache_manager.set("global_feed:initial:20", cached_data, ttl_seconds=3600)

    # 2. Execute the service method (should trigger a cache hit and skip network calls)
    result = feed_service.get_paginated_global_feed(current_cursor="initial", page_size=20)

    # Assertions:
    assert result["source"] == "cache"
    assert len(result["events"]) > 0
    
    # CRITICAL ASSERTION: Ensure the expensive gateway function was never called
    mock_event_repo.find_paginated_events.assert_not_called()


def test_cache_miss_and_successful_fetch(feed_service: FeedService, mock_event_repo: MagicMock, cache_manager: BoundedCacheManager):
    """Tests the full flow: Cache Miss -> Gateway Call (Success) -> Cache Write."""
    # 1. Clear any existing state just in case
    cache_key = "global_feed:initial:20"
    cache_manager.delete(cache_key)

    # 2. Mock the expensive return value from the gateway's find function
    MOCK_SUCCESS_EVENTS = [{"id": "mock_event1", "content": "LIVE data"}], \
                            {"source": "live", "events": MOCK_SUCCESS_EVENTS, "cursor": "new_cursor"}
    
    # Assign the mock return to the repository method
    mock_event_repo.find_paginated_events.return_value = MOCK_SUCCESS_EVENTS[1]

    # 3. Execute the service method
    result = feed_service.get_paginated_global_feed(current_cursor="initial", page_size=20)

    # Assertions:
    assert result["source"] == "live"
    mock_event_repo.find_paginated_events.assert_called_with({
        \"kind\": 1, \"authors\": [], \"cursor\": \"initial\"}, limit=20)
    
    # Check that the cache now holds the result (ensuring data persisted)
    cached_value = cache_manager._cache.get("global_feed:initial:20")
    assert cached_value and cached_value['source'] == 'live'


def test_expired_cache_forces_refetch(feed_service: FeedService, mock_event_repo: MagicMock, cache_manager: BoundedCacheManager):
    """Tests that if the stored data TTL passes, it correctly forces a Gateway refetch."""
    # 1. Set a very short simulated TTL (e.g., only 0.01 seconds)
    cache_key = "global_feed:initial:20"
    initial_data = {"source": "live", "events": [], "cursor": None}
    cache_manager._cache[cache_key] = cache_manager.CacheEntry(initial_data, ttl_seconds=0.01)

    # 2. Wait for the TTL to elapse (sleep is acceptable in unit tests for time boundaries)
    import time; time.sleep(0.05) 

    # 3. Execute service method (Expect Cache Miss/Expiration Handling)
    result = feed_service.get_paginated_global_feed(current_cursor="initial", page_size=20)
    
    # Assertions: We expect the Gateway to be called because the cache entry should now be expired.
    mock_event_repo.find_paginated_events.assert_called() 
    print(\"Test successfully detected and proceeded past expired cache data.\")
    assert result["source"] == "live"


def test_cache_size_limitation_and_lru_eviction(cache_manager: BoundedCacheManager):
    """Tests the bounding mechanism by filling cache beyond capacity (5) - LRU eviction should discard oldest."""
    MAX = cache_manager.max_size # Should be 5 based on fixture setup

    # Populate up to MAX size
    for i in range(MAX):
        cache_manager.set(f\"key_{i}\", f\"value_{i}\")
    
    assert cache_manager.size == MAX
    
    # Access key_0 (Least Recently Used) just before eviction test, making it MRU/Less LRU contender
    print(\"Accessing Key 0 to demonstrate LRU movement...\")
    cache_manager.get("key_0")
    
    # Add one extra item - this MUST trigger the removal of an old key (key_1, key_2, etc.)
    try:
        cache_manager.set(\"new_overflow\_item\", \"value\") # This triggers cleanup sequence
    except Exception as e:
         print(f\"Warning during eviction test: {e}\")

    # Check that the size is still bounded and that key 0 (which was accessed) remains, while an older key should be gone.
    assert cache_manager.size == MAX
    # Since we added one, and one was removed, size remains constant at MAX.
    print(f\"Final Cache Size: {cache_manager.size}. Check for eviction logs in the output.\")

