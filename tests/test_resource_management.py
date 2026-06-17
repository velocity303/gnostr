import pytest
from unittest.mock import MagicMock, patch
from src.util.cache_manager import BoundedCacheManager
from src.service.feed_service import FeedService


@pytest.fixture(scope="function")
def cache_manager():
    return BoundedCacheManager(max_size=5)


@pytest.fixture(scope="function")
def mock_event_repo():
    return MagicMock()


@pytest.fixture(scope="function")
def feed_service(mock_event_repo):
    return FeedService(event_repo=mock_event_repo)


def test_cache_hit_on_global_fetch(feed_service, mock_event_repo, cache_manager):
    cached_data = {
        "source": "cache",
        "events": [{"id": "mock_event1", "content": "Cached content"}],
        "cursor": "initial"
    }
    cache_manager.set("global_feed:initial:20", cached_data, ttl_seconds=3600)

    with patch("src.service.feed_service.CACHE", cache_manager):
        result = feed_service.get_paginated_global_feed(current_cursor="initial", page_size=20)

    assert result["source"] == "cache"
    assert len(result["events"]) > 0
    mock_event_repo.find_paginated_events.assert_not_called()


def test_cache_miss_and_successful_fetch(feed_service, mock_event_repo, cache_manager):
    cache_manager.delete("global_feed:initial:20")

    MOCK_SUCCESS_EVENTS = [{"id": "mock_event1", "content": "LIVE data"}]
    mock_event_repo.find_paginated_events.return_value = MOCK_SUCCESS_EVENTS

    with patch("src.service.feed_service.CACHE", cache_manager):
        result = feed_service.get_paginated_global_feed(current_cursor="initial", page_size=20)

    assert result["source"] == "live"
    mock_event_repo.find_paginated_events.assert_called_with({
        "kind": 1, "authors": [], "cursor": "initial"
    }, limit=20)

    cached_value = cache_manager._cache.get("global_feed:initial:20")
    assert cached_value is not None


def test_expired_cache_forces_refetch(feed_service, mock_event_repo, cache_manager):
    cache_key = "global_feed:initial:20"
    initial_data = {"source": "live", "events": [], "cursor": None}
    from src.util.cache_manager import CacheEntry
    cache_manager._cache[cache_key] = CacheEntry(initial_data, ttl_seconds=0.01)

    import time
    time.sleep(0.05)

    mock_event_repo.find_paginated_events.return_value = []

    with patch("src.service.feed_service.CACHE", cache_manager):
        result = feed_service.get_paginated_global_feed(current_cursor="initial", page_size=20)

    mock_event_repo.find_paginated_events.assert_called()


def test_cache_size_limitation_and_lru_eviction(cache_manager):
    MAX = cache_manager.max_size

    for i in range(MAX):
        cache_manager.set(f"key_{i}", f"value_{i}")

    assert cache_manager.size == MAX

    cache_manager.get("key_0")

    cache_manager.set("new_overflow_item", "value")

    assert cache_manager.size == MAX
    assert cache_manager.get("key_0") is not None
