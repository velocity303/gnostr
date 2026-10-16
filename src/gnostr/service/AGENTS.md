# DOX — src/gnostr/service/

## Purpose
Service layer for feed and profile data — fetches, caches, and serves Nostr events to UI widgets. Separates data logic from UI rendering.

## Ownership
- `feed_service.py` — FeedService: manages feed event lists, filtering, caching
- `feed_model.py` — FeedModel: widget-agnostic bounded feed window, keyset cursor state, eviction + live-prepend buffer (workstream #0)
- `profile_service.py` — ProfileService: profile lookups and caching
- `profile_metadata_service.py` — ProfileMetadataService: metadata extraction from profiles
- `dm_service.py` — DMService (GObject.Object): NIP-44 v2 encrypt/decrypt for kind-4 rumors. Decryption runs on a worker thread and emits `dm-decrypted` on the main context (UI consumes signals only — never decrypt on the UI thread); after decrypt it writes the plaintext back through `gateway.update_content` so inbox previews/threads show readable text. `decrypt_for_event` recognizes our OWN relay echo (sender == our pubkey) and returns the payload verbatim — NIP-44 cannot decrypt our own message (conversation key is derived with the peer), and rewriting the plaintext self-copy row back to ciphertext is the bug this guard prevents.

## Local Contracts
- Services are instantiated by NostrClient and exposed to UI via the client reference
- UI widgets call service methods, not database methods directly
- FeedService maintains the in-memory event list for the active feed
- ProfileService caches profile data to avoid redundant relay lookups
- ProfileMetadataService extracts display_name, picture, banner from profile events
- FeedModel is GTK-free: it owns only event ids + a `(created_at, id)` keyset cursor, and depends on a Database-like object. It never touches GTK, so it is unit-testable headless. Ordering mirrors `database.get_feed_following` (`created_at DESC, id DESC`). `cursor` is always the OLDEST in-window row; `evict_newest(n)` drops the top (newest) overflow and never touches the cursor. `load_older`/`load_newer` are the bidirectional re-pull (keyset `before`/`after`); a short page sets `exhausted` (older) or `at_top` (newer). The model self-bounds to `max_window` after each load and promotes buffered `new_ids` into the window at their sorted position (leaving the buffer) on re-pull, so live events reach the window without the view reaching into internals.

## Work Guidance
- Services operate on data already stored in Database — they're cache/query layers, not persistence layers
- FeedService supports multiple feed types: following, global, user-specific
- ProfileService uses the database cache first, falls back to relay fetch
- Keep service methods synchronous — async relay fetching is handled by the client layer

## Verification
- `pytest tests/test_profile_service.py tests/test_profile_metadata_service.py` — service tests pass
- Mock database and client in tests — never require real relay connections
