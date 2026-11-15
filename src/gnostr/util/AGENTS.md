# DOX — src/gnostr/util/

## Purpose
Utility components — shared helpers for caching and connection state tracking used across the application.

## Ownership
- `cache_manager.py` — CacheManager: generic LRU cache for events and profiles
- `connection_state.py` — ConnectionState: relay connection state enum and tracking
- `collapse.py` — FEED-TRUNC: shared token-safe content cut (`safe_truncate` — never splits URL/domain/@mention/nostr:-ref, ellipsis appended) + `feed-collapse-chars` GSettings access (default 500; 0 = never collapse; other values clamped 200–5000) with the hard-fallback contract (no schema ⇒ default read / False write). Used by feed cards (main.py) AND the thread hero (thread_view.py) — the one cut helper.
- `scroll_gate.py` — ScrollGate: touch-activation cooldown after scroll momentum (issue #22)

## Local Contracts
- CacheManager is used by both Database and services for memory-efficient caching
- ConnectionState is used by Gateway and UI for status indicators
- ScrollGate is a pure timestamp gate; FeedView stamps it on the vadjustment's `value-changed`, UI handlers consult `in_cooldown()` — see `src/gnostr/ui/AGENTS.md`
- No UI-rendering code lives here — this is pure data logic

## Work Guidance
- CacheManager uses a simple dict with max-size eviction — no external caching dependencies
- ConnectionState tracks states: CONNECTED, WARNING, DISCONNECTED, RECONNECTING
- Keep utility functions stateless where possible — pure functions preferred
- Do not import GTK modules here — this layer must be testable without display. Exception: `collapse.py` imports Gio lazily inside a try-block for the GSettings hard-fallback contract (no display needed; `test_feed_collapse.py` guards no top-level gi import)

## Verification
- `pytest tests/test_resource_management.py` — cache and resource tests pass
- No GTK/GStreamer imports in this directory — verifiable in headless CI
