import time
from typing import Any, Optional, Dict, List
from collections import OrderedDict

class CacheEntry(object):
    """Represents a cached item with its expiration timestamp."""
    def __init__(self, value: Any, ttl_seconds: int = 300):
        self.value = value
        self.ttl = ttl_seconds # Time-to-live in seconds
        self.cached_at = time.time()

    def is_expired(self) -> bool:
        """Checks if the entry has passed its TTL."""
        return (time.time() - self.cached_at) > self.ttl

class BoundedCacheManager:
    """
    A simple, thread-safe, cache implementation that supports Time-To-Live (TTL) 
    and enforces a strict maximum size limit using Least Recently Used (LRU) eviction policies.
    This is designed to prevent the cache from 'ballooning' indefinitely with stale data, 
    while ensuring highly transient data like feed statuses expire.
    """
    def __init__(self, max_size: int = 500):
        if max_size <= 0:
            raise ValueError("Cache size must be a positive integer.")
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self.max_size = max_size

    def get(self, key: str) -> Optional[Any]:
        """Retrieves an item if it exists and has not expired."""
        if key not in self._cache:
            return None
        
        entry = self._cache[key]
        if entry.is_expired():
            print(f"Cache eviction (TTL): Key '{key}' was found but is expired.")
            self.delete(key) # Clean up the stale entry immediately
            return None
        
        # Use LRU logic: Move the accessed item to the end for "most recently used".
        self._cache.move_to_end(key) 
        return entry.value

    def set(self, key: str, value: Any, ttl_seconds: int = 300):
        """Sets a new or replacement item in the cache, respecting TTL and max size."""
        # 1. Check if entry exists to handle updates correctly
        existing = self._cache.get(key)
        if existing is None and not self._is_full():
             pass # Will create new entry below

        # 2. If the key already exists, we must delete it first to control its position/existence in OrderedDict
        if key in self._cache:
            self._cache.pop(key)

        # 3. Evict if necessary before setting (Size Check)
        if self._is_full():
            evicted_key, _ = self._cache.popitem(last=False) # Pop the absolute oldest (LRU) item
            print(f"Cache eviction (SIZE LIMIT): Removed least recently used key '{evicted_key}'.")

        # 4. Set the new entry and move it to the end (MRU position)
        self._cache[key] = CacheEntry(value, ttl_seconds)
        
    def delete(self, key: str) -> bool:
        """Removes an item from the cache by key."""
        if key in self._cache:
            del self._cache[key]
            return True
        return False

    @property
    def size(self) -> int:
        return len(self._cache)

    def _is_full(self):
        """Checks if the cache has reached its theoretical maximum capacity."""
        return len(self._cache) >= self.max_size

# Expose a default manager instance for easy import/use in other modules
# Users should ideally initialize this with a proper backend (Redis, etc.) but this serves as an effective prototype.
CACHE = BoundedCacheManager(max_size=1000)