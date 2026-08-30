# DOX — src/gnostr/service/

## Purpose
Service layer for feed and profile data — fetches, caches, and serves Nostr events to UI widgets. Separates data logic from UI rendering.

## Ownership
- `feed_service.py` — FeedService: manages feed event lists, filtering, caching
- `feed_model.py` — FeedModel: widget-agnostic bounded feed window, keyset cursor state, eviction + live-prepend buffer (workstream #0)
- `profile_service.py` — ProfileService: profile lookups and caching
- `profile_metadata_service.py` — ProfileMetadataService: metadata extraction from profiles

## Local Contracts
- Services are instantiated by NostrClient and exposed to UI via the client reference
- UI widgets call service methods, not database methods directly
- FeedService maintains the in-memory event list for the active feed
- ProfileService caches profile data to avoid redundant relay lookups
- ProfileMetadataService extracts display_name, picture, banner from profile events
- FeedModel is GTK-free: it owns only event ids + a `(created_at, id)` keyset cursor, and depends on a Database-like object. It never touches GTK, so it is unit-testable headless. Ordering mirrors `database.get_feed_following` (`created_at DESC, id DESC`). `cursor` is always the OLDEST in-window row; `evict_newest(n)` drops the top (newest) overflow and never touches the cursor.

## Work Guidance
- Services operate on data already stored in Database — they're cache/query layers, not persistence layers
- FeedService supports multiple feed types: following, global, user-specific
- ProfileService uses the database cache first, falls back to relay fetch
- Keep service methods synchronous — async relay fetching is handled by the client layer

## Verification
- `pytest tests/test_profile_service.py tests/test_profile_metadata_service.py` — service tests pass
- Mock database and client in tests — never require real relay connections
