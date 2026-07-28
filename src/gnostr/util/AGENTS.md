# DOX — src/gnostr/util/

## Purpose
Utility components — shared helpers for caching and connection state tracking used across the application.

## Ownership
- `cache_manager.py` — CacheManager: generic LRU cache for events and profiles
- `connection_state.py` — ConnectionState: relay connection state enum and tracking

## Local Contracts
- CacheManager is used by both Database and services for memory-efficient caching
- ConnectionState is used by Gateway and UI for status indicators
- No UI-rendering code lives here — this is pure data logic

## Work Guidance
- CacheManager uses a simple dict with max-size eviction — no external caching dependencies
- ConnectionState tracks states: CONNECTED, WARNING, DISCONNECTED, RECONNECTING
- Keep utility functions stateless where possible — pure functions preferred
- Do not import GTK modules here — this layer must be testable without display

## Verification
- `pytest tests/test_resource_management.py` — cache and resource tests pass
- No GTK/GStreamer imports in this directory — verifiable in headless CI
