# Gnostr Development Tracking

This document tracks all bugs, issues, and problems that need fixing.

## Current Workstream

### 12. Follows List + Feed Renames (NEW FEATURE — implemented 2026-08-31)
**Status:** ✅ COMPLETE — code + tests shipped (5 commits); DOX pass pending approval

**Goal (user-defined):** the sidebar "Follow Sync" row becomes a "Follows" list showing who you follow; the list is reusable for ANY profile (click their profile → see who they follow); on your own profile there's a Sync button that mimics today's Follow Sync behavior. Feed rows renamed "Following"→"My Feed", "Global"→"Global Feed".

**Design decisions:**
- The `following` table is already owner-keyed (`owner_pubkey, followed_pubkey`) — no schema change. Kind-3 ingestion generalized from own-pubkey-only to ANY author: the relay's newest kind-3 reconciles that author's rows (`save_contacts(owner, …)`), so foreign lists populate without extra storage machinery.
- `contacts-updated` signal carries the pubkey; an open `FollowersListView` reloads when it matches its owner.
- Sync behavior extracted to `MainWindow.sync_follows()` — shared by the own-profile button and the own Follows list sync button (same toasts + Relay Activity confirmation as the old row).
- Sidebar ids `following`/`global` unchanged (they're the FeedModel feed types); only display labels renamed.

**Implemented:**
- ✅ `client.fetch_contacts_for(pubkey)` — fetch any user's kind-3 (own `fetch_contacts` delegates).
- ✅ `_handle_event` kind-3 branch: reconciles ANY author's list into that owner's rows; activity-pane log line stays own-pubkey-only; relay-hint merge stays own-pubkey-only.
- ✅ `ui/followers_list_view.py` — `FollowersListView(main_window, owner_pubkey, is_own)`: avatar/name/npub rows, Refresh (fetch from relays), Sync button (own only, → `main.sync_follows()`), row tap → `show_profile`; added to `ui/meson.build` install list.
- ✅ Sidebar: `follow_sync` row removed → `followers` "Follows"; "My Feed" / "Global Feed" labels. `main.show_follows(owner_pubkey)` pushes the list page.
- ✅ ProfileView: "Following N" is now a tappable button → that pubkey's Follows list; own profile gains "Sync Follows" beside Edit Profile.
- ✅ Tests: `test_follow_sync.py` extended (fetch_contacts_for, foreign-kind-3 reconciliation contract flip, own-log regression); new `tests/test_follows_view.py` (ast-extraction logic tests: reload per pubkey, owner-matched self-refresh, refresh → fetch_contacts_for, row activation, title wording).

**Deferred (carried from #2):** automatic periodic sync (manual + startup only); relay-hint-targeted kind-3 fetch; foreign-profile follower count (reverse lookup).

---

### 0. Feed Pruning: Sliding Window + Cursor Pagination (NEW FEATURE — designed 2026-08-29)
**Status:** ✅ COMPLETE — implemented 2026-08-29, all 6 tasks shipped (see git log: `FeedModel`, keyset re-pull, model-driven FeedView, anchor restore)

**Goal (user-defined UX):** a bounded window of the newest posts; scrolling to the end pulls older posts from the DB; scrolled-past posts drop off the top and are re-pulled from the DB on scroll-up; the refresh button jumps to top and pulls in new posts. Fixes the two 🔶 leaks from 2026-07-31: unbounded `event_widgets`/posts_box retention and `switch_feed` wipe-and-resubscribe.

**Architecture decision (recorded): Option A — bounded Gtk.Box window with vadjustment-driven eviction — NOT Option B (Gtk.ListView).**
- Rationale: PostWidget is a stateful, variable-height, async ad-wrapped composite (image loads, quote-card registries, video handles, gestures). ListView's row recycling re-binds widgets to new events — every in-flight async callback becomes a wrong-content bug — and its performance advantage only exists for uniform-height rows. Feed scale (window ~30, hard cap ~50 widgets) is trivially handled by A. PostWidget is also used by thread_view (5 shapes) and profile_view; B would fork it into two maintenance tracks.
- B stays *possible* later: keep the data layer widget-agnostic. Introduce a `FeedModel` (ordered event-id list + created_at cursor state + DB pagination) that knows nothing about widgets; the widget window is just one renderer of it.
- C (VirtualListView) ruled out by user — uniform-height only.

**Contracts:**
1. **Anchor stability:** after any eviction/refetch/prepend, restore scroll so the topmost-visible post's event id stays in place (anchor event id + pixel offset captured before mutation, restored after).
2. **Cursor pagination:** `get_feed_following(owner, before_created_at, before_id, limit)` — `WHERE created_at < ? ORDER BY created_at DESC LIMIT ?` on the existing `idx_events_feed`; never OFFSET (degrades + double-serves rows when new events arrive mid-scroll).
3. **Widget state from data, never from the widget:** recreation must re-resolve like-state (`db.user_reaction`) and metrics labels (`client.metrics`) — the dropped widget must not be the only holder of any state.
4. **Live prepend policy:** while scroll offset > top, incoming live events do NOT prepend — buffer them and show a "N new posts" pill; tapping the pill (or refresh) jumps to top and applies them. Refresh button = jump-to-top + fetch new since last-seen.
5. **End-of-DB sentinel:** when a page returns < limit, mark exhausted; stop re-querying the bottom until a refresh resets it. Loading affordance on pull-up.
6. **Window sizes:** render ~30 rows, hard cap ~50 widgets (evict beyond cap from whichever end is away from the viewport). `event_widgets` entries evicted with their widgets; anything needing later lookup re-resolves from the DB (contract 3).

**Tasks (each a commit):**
1. `database.get_feed_following` cursor-pagination variant + tests (in-memory SQLite, assert no OFFSET, assert index usage via EXPLAIN QUERY PLAN).
2. `FeedModel` in `src/gnostr/service/` — event-id window, cursor state, load_older()/reset(), no GTK imports; unit tests.
3. FeedView scroll plumbing: expose vadjustment, near-bottom + near-top signals, anchor capture/restore helpers.
4. Eviction + re-pull: wire FeedModel to posts_box (prepend/append widgets, evict far-end beyond cap), end-of-DB sentinel + loading row.
5. Live-prepend policy + "N new posts" pill + refresh semantics (jump-to-top, fetch new since last-seen).
6. `switch_feed` uses FeedModel (no more wipe-and-resubscribe on refresh); DOX pass (ui/AGENTS.md, service/AGENTS.md, root AGENTS.md child index if a new module ships — remember the meson install-list guard).

---

### 1. Like Button + Relay Feedback (RESOLVED 2026-08-08)
**Status:** ✅ COMPLETE — verified working on-device

**Description:** The like button appeared to do nothing, the thread-view hero showed the wrong icon, and there was no feedback on whether likes/follows reached relays. Root-caused and fixed across 14 commits.

**Resolved items:**
- ✅ **Like button click never fired** — a `Gtk.GestureClick` CLAIM gesture on each action button competed with `Gtk.Button`'s internal click gesture and won on press, so `clicked` never fired and `_on_like` never ran. Removed the CLAIM gesture; the card's open-thread handler now checks the click target (`_on_card_clicked`) and skips opening the thread when the click is on an action button. Commit `a131851`.
- ✅ **Like/repost/reply/follow no-op on saved-key startup** — `perform_login` never called `client.set_keys`, so the client's keys stayed None and every `publish_*` returned False. Now pushes keys to the client. Commit `8096ab0`.
- ✅ **Feed crash — `IconTheme.has_icon_pixbuf`** — GTK2/3-era method that doesn't exist on GTK4; every PostWidget raised AttributeError. Switched to `has_icon()`. Commit `0afe54c`.
- ✅ **Thread hero wrong icon + no working like button** — hero PostWidget (`is_hero=True`) never applied liked state nor wired `_on_like`. Now applies outline/filled + `.liked` class and wires the like button. Commit `8cb46fe`.
- ✅ **No relay submission feedback** — added NIP-01 OK ack parsing (`parse_ok_message`), `publish-result` signal, `pending_publishes` tracking with 30s expiry, and `Adw.Toast` feedback. Commits `148ae03`, `affb1da`.
- ✅ **Relay activity log** — bounded `relay_log` deque + `relay-log-updated` signal feeding a sidebar "Relay Activity" pane. Commits `9da60e7`, `f9fb395`.
- ✅ **Publish observability** — `relay.publish()` returns True/False and logs when a relay isn't connected or the send fails; `NostrClient.publish()` logs `EVENT <id> sent to N/M relay(s)`. Commit `700f7bf`.
- ✅ **Relay subscription spam** — `bad close: invalid subscription id length` + `too many concurrent REQs` NOTICEs fixed by tracking `active_sub_ids` per relay and only CLOSing IDs actually opened. Commit `700f7bf`.
- ✅ **Like visual** — icon resolves against active icon theme with fallbacks; `.liked` accent CSS class toggles with state. Commit `93bf0ad`.

**Remaining (still open):**
- 🔶 **Relay connection verification** — the latest output.txt showed zero relay-activity lines, which was traced to the CLAIM-gesture bug (like never published). After the fix, the relays should connect and log. **Verify on-device that relays connect and OK acks appear in the Relay Activity pane.** If relays still don't connect, investigate the WebSocket connection layer (`connect_all` → `add_relay_connection` → `NostrRelay.start`).

---

### 2. Follower Sync: DB ↔ Relays — IMPLEMENTED 2026-08-29
**Status:** ✅ COMPLETE — pull + push implemented, 4 tests, DOX pass

**Description:** The user suspected recent follows only hit the local DB and never reached relays. Both reconciliation directions now implemented.

**Implemented:**
- ✅ **Pull (relay → DB):** on first relay CONNECTED, `main.on_status_changed` triggers `client.fetch_contacts` (kind-3, own pubkey, limit 1); `_handle_event`'s kind-3 branch reconciles the DB `following` table wholesale (`save_contacts` replaces rows) and logs `kind-3 received: N follows reconciled` to the Relay Activity pane.
- ✅ **Push (DB → relay):** new `client.sync_followers()` re-publishes the current DB following list via `_publish_contact_list(label="Follow Sync")` — tracked in `pending_publishes`, so the OK ack lands in the Relay Activity pane as `Follow Sync OK <relay>`. Sidebar gets a `Follow Sync` nav row wired through `main.on_menu_selected`.
- ✅ **Dedup:** `publish_contacts()` was a duplicate untracked kind-3 builder; now delegates to `_publish_contact_list` (acks tracked for that path too).
- ✅ **Tests:** `tests/test_follow_sync.py` — 4 tests: pull reconciles from relay p-tags, pull ignores other authors' kind-3, push publishes DB list + tracks ack + logs, push requires keys. Full suite 92 passed.

**Deferred:** automatic periodic sync (manual + startup only); relay-hint-targeted kind-3 fetch.

---

### 3. CI Pipeline Implementation (FIXING NOW)
**Status:** In Progress - Fixing test import issues

**Description:** Based on the established code, work with opencode to draft more wholesome unit tests that are tested in a way compatible with the ci.yaml that is described below. Implement this ci.yaml and make sure you can test the pipeline completely.

**Completed:**
- Created `.gitea/workflows/ci.yaml` for Gitea Actions
- Added linting steps (black, flake8)
- Added test runner with xvfb for GTK rendering
- Added system dependencies installation
- Added `test_codec_support.py` for codec testing

**Remaining:**
- [ ] Run CI pipeline on remote repository
- [ ] Verify Flatpak build step
- [ ] Add coverage reporting

---

### 4. Test Import Fix (CRITICAL)
**Status:** ✅ RESOLVED (2026-07-12, verified 2026-08-29) — full suite imports and runs headless (88 passed) with gi mocked in `conftest.py`.

**Description:** The test environment fails because it cannot import the `gi` module required by `renderer.py`. All tests fail with `ModuleNotFoundError: No module named 'gi'`.

**Root Cause:**
- Tests import `src.renderer` which imports `gi.repository.Gtk`
- The actual `gi` module is not available in the test environment (or CI)
- Tests need to mock `gi` BEFORE importing the module under test

**Fix Plan:**
1. Mock `gi` and all its submodules (Gtk, Gst, Adw, etc.) in `conftest.py`
2. Patch `sys.modules` to prevent actual imports
3. Ensure mock objects have proper attributes for test expectations
4. Fix fixture scope mismatches

**Completed:**
- Created comprehensive mock setup in `tests/conftest.py`
- Mocked all required GObject Introspection modules
- Set up proper fixture scoping

**Remaining:**
- [ ] Verify `src` is a proper package
- [ ] Test imports work correctly
- [ ] Verify mock objects have all required attributes

---

### 5. Nostr Type Recognition (NIP-19 / NIP-21) — IMPLEMENTED (naddr/long-form)
**Status:** ✅ COMPLETE — naddr (long-form article) support implemented + verified (2026-08-09). nrelay renders as a link. See Progress Log entry below.

**Description:** The user spotted `nostr:nevent...` references in the feed and asked which other Nostr bech32 types the app should recognize. Research against the authoritative [NIP-19](https://nips.nostr.com/19) and [NIP-21](https://nips.nostr.com/21) specs (nostr-protocol/nips master) revealed the complete set of standardized prefixes and the current gaps in gnostr.

**The complete NIP-19 bech32 prefix set (7 total):**
- `npub` — public key (bare 32-byte hex)
- `nsec` — private key (bare 32-byte hex) — **excluded from `nostr:` URIs by NIP-21**
- `note` — event id (bare 32-byte hex)
- `nprofile` — profile + optional relay hints (TLV)
- `nevent` — event + optional relay/author/kind hints (TLV)
- `naddr` — addressable event coordinate (TLV: d-tag + kind + pubkey + relays) — **MISSING in gnostr**
- `nrelay` — relay (deprecated 2024-07-25) — **MISSING in gnostr**

**Important correction:** `nquote`, `nroom`, `nsite`, `nclientauth`, `nchannel` are **NOT** in the official NIP-19 spec — they were community proposals that never standardized. They should NOT be implemented. The only real missing standardized type is `naddr` (plus the deprecated `nrelay`).

**Current gnostr support (verified in source):**
- ✅ `nostr:npub` / `nostr:nprofile` → inline bold `@name` mention (renderer.py `render()`)
- ✅ `nostr:note` / `nostr:nevent` → quote card (renderer.py `_add_nostr_card`, honors nevent relay hint)
- ✅ `nostr:naddr` → **quote card resolved by coordinate** (renderer.py `_add_naddr_card` + `database.get_event_by_a`) — NEW
- ✅ `nostr:nrelay` → **plain link** (deprecated NIP-19 type) — NEW
- ✅ `nsec` → login/key handling (nostr_utils.py `nsec_to_hex`) — not via `nostr:` URI, per NIP-21
- ✅ `is_nostr_reference()` now covers nevent/nprofile/naddr/nrelay/note/npub

**Why naddr matters:** NIP-33 addressable events (kinds 30000-39999, e.g. long-form articles kind 30023, NIP-23) are referenced by `a`-tag coordinates (`<kind>:<pubkey>:<d-tag>`), not by event id. `naddr` is the bech32 form of that coordinate. Gnostr can now display quoted long-form articles.

**Implementation plan (all phases complete):**

**Phase 1 — Decode layer (nostr_utils.py):** ✅
1. ✅ `decode_naddr(bech32)` → `(kind, pubkey, d_tag, relays[])` via TLV types 0/1/2/3. Unknown TLVs ignored per NIP-19.
2. ✅ `decode_nevent_full(bech32)` → `(event_id, relays[], author, kind)` — relay/author/kind hints now available.
3. ✅ `decode_nprofile(bech32)` → `(pubkey, relays[])`; shared `_decode_tlv` parser.
4. ✅ `is_nostr_reference()` includes `naddr` + `nrelay`.

**Phase 2 — Render layer (renderer.py):** ✅
5. ✅ `render()` routes `nostr:naddr...` to `_add_naddr_card()`.
6. ✅ `_add_naddr_card` builds coordinate `f"{kind}:{pubkey}:{d_tag}"`, checks `database.get_event_by_a()`, else `request_once` with `{"kinds":[kind], "authors":[pubkey], "#d":[d_tag], "limit":1}`.
7. ✅ `_add_nostr_card` honors the nevent relay hint via `decode_nevent_full`.
8. ✅ `nostr:nrelay` renders as a plain link.
9. ✅ Both card types share `_build_quote_card` (DB check → fetch → register pending → wire click); `_on_quote_clicked` opens thread for events, resolves coordinate for naddr.

**Phase 3 — Persistence (database.py):** ✅
10. ✅ `get_event_by_a(kind, pubkey, d_tag)` — kind+pubkey index scan, d-tag matched in Python from JSON tags, returns newest (addressable events are replaceable). Existing `idx_events_feed(pubkey, kind, created_at)` covers the query — no new index needed.

**Phase 4 — Signal wiring + Tests + DOX:** ✅
11. ✅ `client.py` emits `quote-event-received` (full event JSON) for EVERY kind after `save_event` — addressable events never hit the kind-1 `event-received` path.
12. ✅ `main.py` `on_quote_event_received` matches pending cards by event id (`("id", hex)`) or coordinate (`("a", kind:pubkey:d-tag)`) and populates via `_build_quote_content`. **Also fixes a latent bug:** note/nevent quote cards previously showed "Loading..." forever — `quote_widgets` was set but never consumed.
13. ✅ `test_nostr_types.py` — decode naddr/nevent/nprofile against real spec examples + `get_event_by_a` DB lookup. 7 tests, all pass.
14. ✅ DOX pass — `src/gnostr/AGENTS.md` (nostr_utils/renderer/database/client/main contracts) + `tests/AGENTS.md` + this TRACKING.md entry.

**Open questions (resolved during implementation):**
- naddr quote cards fetch via `request_once` to all connected relays (the relay hint is decoded but not yet used to target a specific relay — `request_once` fans out to all active relays; a relay-hint-targeted fetch is a future optimization).
- `nrelay` renders as a plain link (deprecated type, no connect action).

---

### 6. Package Structure Fix
**Status:** ✅ RESOLVED (2026-08-29 review) — `src/gnostr/__init__.py` exists and the package imports cleanly; `tests/test_package_import.py` guards it. The original `from src.renderer import` pattern is gone; tests import `gnostr.*` with `src/` on sys.path via `conftest.py`.

**Description:** The `src` directory is not a proper Python package (missing `__init__.py` or incorrect structure).

**Root Cause:**
- Tests import `from src.renderer import ContentRenderer`
- Python cannot find `src` as an importable module

**Fix:**
- Ensure `src/__init__.py` exists and makes `src` an importable package
- OR adjust test imports to use `from . import renderer` style

---

### 7. GTK4 API Compatibility
**Status:** ✅ RESOLVED (2026-08-29 review) — tests use GTK4-correct patterns throughout (`get_children()` as method returning a list); no GTK3-era calls remain in the suite.

### 8. GStreamer Pipeline Issues
**Status:** ✅ OBSOLETE (2026-08-29 review) — the appsink/`uridecodebin` path was removed entirely (gtk4paintablesink migration, 2026-07-31). Tests mock Gst; CI no longer needs plugin availability for this.

### 9. Pytest Fixture Scope Mismatch
**Status:** ✅ RESOLVED (2026-07-12) — fixed in the test-import pass; verified still function-scoped in `conftest.py`.

---

### 10. Profile Page Buildout (NIP-05/24/39/57/58) — IMPLEMENTED 2026-08-10
**Status:** ✅ COMPLETE — all implementable profile-page portions done, committed + pushed to Gitea

**Description:** Fully built out the profile page per the NIPs for identity verification, profile descriptions, banners, lightning addresses, external identities, and badges. Implemented across 6 commits (`8c68b7e` → `32d7f78`).

**Implemented:**
- ✅ **NIP-01/NIP-24 metadata persistence** — widened `profiles` table with `website`, `banner`, `nip05`, `lud16`, `bot`, `birthday`, `raw_json` columns + ALTER TABLE migration for existing installs; `save_profile` extracts all fields and stores raw kind-0 JSON; deprecated `displayName`/`username` ignored. Commit `8c68b7e`.
- ✅ **NIP-05 identity verification** — GTK-free `profile_nips.verify_nip05` (DNS check via `/.well-known/nostr.json`, returns False on any failure). Commit `4602e35`.
- ✅ **NIP-39 external identities** — `parse_external_identities` (kind 10011 i-tags → github/twitter/mastodon/telegram URLs). Commit `4602e35`.
- ✅ **NIP-58 badges** — `parse_profile_badges` (kind 10008 ordered a/e pairs) + `badge_definitions` table (kind 30009, keyed by coordinate). Commit `4602e35`.
- ✅ **LUD-16 lightning address** — `resolve_lnurl_endpoint` (user@domain → lnurlp URL). Commit `4602e35`.
- ✅ **Client wiring** — `_handle_event` handles kinds 10011/10008/30009 (parse + persist + emit signals); `publish_profile` kind-0 publish path; `fetch_external_identities`/`fetch_badges` TTL-cached fetches. Commit `953b851`.
- ✅ **ProfileView rebuild** — renders banner, bio, website, verified nip05 (async), lightning address, external identities, badges, following count; kicks off NIP-39/58 fetches on open. Commit `6178235`.
- ✅ **Edit Profile dialog** — `EditProfileDialog` (Adw.Window + PreferencesPage) with all metadata fields; publishes kind-0 via `publish_profile`; Edit button on own profile. Commit `32d7f78`.
- ✅ **Tests** — 26 new tests (`test_profile_db.py` + `test_profile_nips.py`); full suite 72 passed + 1 pre-existing codec failure.
- ✅ **DOX pass** — updated `src/gnostr/AGENTS.md` (base64), `src/gnostr/ui/AGENTS.md`, `tests/AGENTS.md`.

**Deferred (needs external integration, not implementable in-app):**
- 🔶 **Zap sending (NIP-57)** — `lud16` renders the address; actual zap-send needs a Lightning wallet integration decision.
- 🔶 **Global follower count** — not in local DB; needs a relay-side kind-3 query (local "following" count shown instead).
- 🔶 **Badge image fetch-on-demand** — badges render from cached kind-30009 definitions; missing definitions show a placeholder chip until fetched.

---

### 11. Search Feature — Paste Identifier → Profile (IMPLEMENTED 2026-08-11)
**Status:** ✅ COMPLETE — committed + pushed to Gitea

**Description:** Replaced the "Search coming soon" stub with a SearchDialog that accepts a pasted Nostr identifier (npub, nprofile, nsec, raw hex pubkey, or `nostr:` URI) and opens the matching user's profile. Implemented across 4 commits (`dbe67a8` → `eddaf8a`).

**Implemented:**
- ✅ **Identifier resolver** — `nostr_utils.resolve_profile_identifier(text)` maps any supported identifier → 64-char hex pubkey (or None): npub (new `npub_to_hex`), nprofile, nsec (own key via `nsec_to_hex` + `get_public_key`), raw hex, `nostr:` prefix; nevent/naddr resolve to author pubkey only when the NIP-19 TLV carries the hint. Commit `dbe67a8`.
- ✅ **SearchDialog** — `dialogs.SearchDialog` (Adw.Window) with entry + Search button; resolves, calls `show_profile`, closes on success; "Not a valid Nostr identifier" toast on failure. Commit `de98a54`.
- ✅ **Wiring** — `main.show_search_dialog()` launches `SearchDialog` (was a stub toast). Commit `eddaf8a`.
- ✅ **Tests** — 9 new tests (`test_search_resolver.py` 8 + `test_search_dialog.py` 1); full suite 83 passed + 1 pre-existing codec failure.
- ✅ **Flatpak-safe** — no new core modules (resolver in `nostr_utils.py`, dialog in `dialogs.py`), so no `meson.build` install-list change needed.
- ✅ **DOX pass** — updated `src/gnostr/AGENTS.md` (base64), `tests/AGENTS.md`.

**Deferred (out of scope, YAGNI):**
- 🔶 **NIP-50 relay text search** — keyword search over relay `search` filter is a separate, larger feature; this is identifier resolution only.

---

## Feature Implementation: Enhanced Video Playback

### Status: ✅ COMPLETE - VIDEO PLAYBACK VERIFIED WORKING (2026-07-31)

### Summary
Successfully implemented enhanced video playback controls with the following features:
- ✅ Videos start **paused** and **muted** by default
- ✅ Play/Pause button to toggle playback
- ✅ Mute/Unmute button with icon updates
- ✅ Volume slider (0-100%)
- ✅ "Open in YouTube" link button for YouTube videos
- ✅ Replaced complex `uridecodebin` pipeline with simpler `playbin3`

### Files Modified
- `src/gnostr/renderer.py` - Complete refactor of VideoPlayer class and _add_video method

---

### Implementation Completed

#### Phase 1: Refactor VideoPlayer Class ✅ COMPLETE
**Changes Made:**
1. ✅ Replaced `uridecodebin` pipeline with `playbin3`
2. ✅ Added state tracking: `_is_muted`, `_is_playing`, `_original_url`
3. ✅ Initialize pipeline with `PAUSED` state and `volume=0.0`
4. ✅ Exposed control methods:
   - `toggle_play()`: Switch between PAUSED and PLAYING
   - `toggle_mute()`: Toggle mute state and update volume
   - `set_volume(value)`: Set volume (0.0-1.0)

#### Phase 2: Add Control Bar UI ✅ COMPLETE
**Changes Made:**
1. ✅ Created vertical box to hold video + controls
2. ✅ Added horizontal control bar below video with:
   - Play/Pause button (icon: `media-playback-start-symbolic`)
   - Mute button (icon: `audio-volume-muted-symbolic`)
   - Volume scale (Gtk.Scale, 0-100, default 0)
   - YouTube link button (if applicable, aligned to end)

**UI Layout:**
```
┌─────────────────────────────────┐
│      [Video Display Area]       │
│                                 │
├─────────────────────────────────┤
│ [▶] [🔇] [──────○─────] [YouTube]│
└─────────────────────────────────┘
```

#### Phase 3: YouTube URL Handling ✅ COMPLETE
**Changes Made:**
1. ✅ Modified `_add_video()` to accept `original_url` parameter
2. ✅ Updated YouTube link parsing to pass original URL
3. ✅ Store original URL in VideoPlayer instance for link button
4. ✅ Link button only appears for YouTube URLs

#### Phase 4: Testing & Edge Cases
**Test Checklist:**
- [x] Video starts paused (no auto-play)
- [x] Video starts muted (volume = 0)
- [x] Play/Pause button toggles correctly and updates icon
- [x] Mute button toggles and updates icon
- [x] Volume slider adjusts volume in real-time
- [x] "Open in YouTube" link opens correct original page
- [x] GStreamer pipeline handles errors gracefully (try/except wrappers)
- [x] Controls work with multiple videos on same page
- [x] Non-YouTube videos don't show link button
- [x] Pipeline state syncs with button states
- [x] Position/seek bar tracks playback and allows seeking (added 2026-07-31)
- [x] Audio plays and mutes correctly (Flatpak pulseaudio socket added)

---

## Issue Encountered

### Network/SSH - Remote repository access via SSH to the self-hosted Gitea is intermittent

### Code Quality
- [x] flake8 errors zeroed out (2026-08-29, commit `0fa6468`) — tree is flake8-clean at max-line-length=120
- [ ] No comprehensive test coverage for UI components
- [ ] No type hints in most files

### Documentation
- [x] README build/run instructions current (2026-08-29) — read-only claim and uridecodebin prereq corrected
- [x] CONTRIBUTING.md added (2026-08-29) — build, conventions, meson install contract, DOX

---

## Progress Log

### 2026-08-31 (follows list + feed renames workstream, #12)
- ✅ **Any-author kind-3 reconciliation** — `_handle_event` kind-3 branch now saves ANY author's list into that owner's `following` rows (owner-keyed table, no schema change); activity-pane log + relay-hint merge stay own-pubkey-only. `contacts-updated` carries the pubkey. `client.fetch_contacts_for(pubkey)` fetches any user's list (own `fetch_contacts` delegates).
- ✅ **`FollowersListView`** (`ui/followers_list_view.py`) — reusable follows list (own or foreign owner), avatar/name/npub rows, Refresh from relays, own-only Sync button, row tap → profile; in the `ui/meson.build` install list (contract test enforces).
- ✅ **Sidebar** — "Follow Sync" row → "Follows" row (opens own list); feed labels "Following"→"My Feed", "Global"→"Global Feed" (ids unchanged). `MainWindow.sync_follows()` / `show_follows(owner)` extracted — shared by own-profile + own-list surfaces.
- ✅ **ProfileView** — "Following N" tappable → that user's Follows list; own profile gains "Sync Follows" (same toasts + Relay Activity confirmation as the old sidebar row).
- ✅ **Tests** — `test_follow_sync.py`: fetch tests + contract flip (foreign kind-3 now reconciles to that owner; own-kind-3 log regression). New `test_follows_view.py` (ast-extraction pattern, same as `test_follow_sync.py` — the gi mocks make the imported widget class a Mock). Suite: **136 passed**.

### 2026-08-29 (follower sync workstream)
- ✅ **Pull:** first-relay-CONNECTED now triggers `fetch_contacts` (kind-3 own-pubkey, limit 1); `_handle_event` kind-3 branch logs the reconciliation count to the Relay Activity pane.
- ✅ **Push:** `client.sync_followers()` + sidebar `Follow Sync` row; publishes the DB list as kind-3 tracked for OK acks (`Follow Sync OK <relay>` in the pane). `publish_contacts()` deduped into `_publish_contact_list`.
- ✅ **Tests:** `test_follow_sync.py` 4 tests (ast-extraction pattern — NostrClient subclasses mocked GObject so the class isn't importable; real methods are extracted from source and bound to a FakeClient). Suite 92 passed.

### 2026-08-29 (hygiene + guard pass)
- ✅ **Chronic red test fixed.** `test_codec_support.py::test_load_and_play_success` (the "pre-existing failure" across 4 workstreams) asserted the pre-lazy-load contract (`set_state(PLAYING)` at build). Replaced with two tests against the current contract: build leaves the pipeline PAUSED (lazy start), `autoplay=True` reaches PLAYING. Added an autouse fixture resetting the shared conftest gi mocks + the class-level `VideoPlayer._cache` between tests — call counts were accumulating across tests, making `assert_called_once` order-dependent. Commit `b113e06`.
- ✅ **flake8 zeroed: 14 → 0** (E741 x3, F841 x4, F541 x3, E501 x3, F811 x1). Notable: `post_widget.mk_met` named its label `l` and returned it — renamed to `lbl` consistently (`return lbl, btn`); main.py's shadowing local `LoginDialog` import dropped. Commit `0fa6468`.
- ✅ **Meson install-list contract guard** — `tests/test_meson_install_contract.py` parses every `meson.build` under `src/`: every `src/gnostr/*.py` in the root `install_data`, every sub-package `.py` in its own, every `.py`-bearing subdir declared via `subdir()`. Regression class fix for `c4f182d` (profile_nips missing → Flatpak-only import crash). Verified green + red on planted probes. Commit `e086e31`.
- ✅ **Black debt cleared** — tree fully black-clean (was 3 files); CI's advisory `black --check || true` can now only go red on new breakage. Commits `713e9fb`, `f95d97e`.
- ✅ **README corrected + CONTRIBUTING.md** — removed false "read-only" claim, `uridecodebin` → `playbin3` prereq, added feature summary and contribution guidelines. Commit `70498fb`.
- ✅ **TRACKING.md hygiene** — resolved stale Pending sections (#4 test imports, #6 package structure, #7 GTK4 API, #8 GStreamer plugins, #9 fixture scope — all closed 2026-07); fixed duplicate `### 5` numbering; closed the code-quality/documentation checkboxes above.
- Suite: **88 passed, 0 failed**; flake8 clean; black clean.

### 2026-07-11
- Cloned repository successfully
- Analyzed codebase structure
- Identified codec support as priority
- Identified animated GIF/WebM profile images as second priority
- Identified CI and linting as foundational needs
- Created TRACKING.md
- Implemented codec verification and error handling
- Enabled animated GIF/WebM profile images
- Created Gitea Actions CI pipeline
- Set up pre-commit hooks
- Added `test_codec_support.py`
- Updated README

### 2026-07-12
- Fixed test import issues
- Updated mock setup in `conftest.py`
- Fixed fixture scope mismatch
- Updated tests to use GTK4 API correctly
- Verified test collection works

### 2026-07-23
- Fixed Flatpak build "Command 'gnostr' not found" error
  - Removed unnecessary PYTHONPATH finish-arg
  - Fixed `src/meson.build` to include `subdir('gnostr')`
- Fixed PyPI package download errors
  - Corrected `iniconf` → `iniconfig` package name
  - Updated pytest version from 9.1.0 → 9.1.1
  - Fixed all wheel URLs and SHA256 hashes
- Pinned yt-dlp to specific version (2026.7.4) with direct wheel URL
- Added GStreamer extension to Flatpak manifest for uridecodebin support
- **Enhanced Video Playback Implementation:**
  - ✅ Phase 1: Refactored VideoPlayer to use playbin3 with paused/muted defaults
  - ✅ Phase 2: Added control bar UI with play/pause, mute, volume
  - ✅ Phase 3: Added YouTube link button with original URL preservation
  - ⏳ Phase 4: Testing and edge case verification (ready for user testing)

### 2026-07-31
- **Video playback verified working end-to-end**
  - ✅ Video frames render correctly (RGB pixel format via appsink caps fix)
  - ✅ Audio plays and mutes correctly (added `--socket=pulseaudio`)
  - ✅ Position/seek bar tracks playback and allows seeking
  - ✅ Playback confirmed (state transitions to PLAYING)
- **Key bugs fixed during playback debugging:**
  - `s.emit("pull-sample")` → `s.pull_sample()` (wrong GI method)
  - `parse_error()` on WARNING messages → `parse_warning()` + try/except
  - `new_from_bytes()` missing `has_alpha` arg (takes 7, gave 6)
  - Appsink not requesting RGB caps → added `video/x-raw,format=RGB`
  - `_start_position_timer` scope bug (container undefined) + indentation
- **Optimizations applied:**
  - ✅ Suppressed GStreamer CRITICAL noise: ERROR/EOS/WARNING checks use exact `==` match, so parse_* only called on pure-typed messages
  - ✅ Performance: frames >1280px downscaled via `scale_simple` before Gdk.Texture (4K→~1280px, cheaper GPU upload)
  - ✅ Debug logging: all 35 `🎬` prints gated behind `_DEBUG_VIDEO` flag (default off)

### 2026-07-31 (mobile memory/perf pass)
- **Targeted mobile memory + performance (gnostr runs on phones):**
  - ✅ Bounded `ImageLoader._cache` to 64 entries (LRU, was unbounded plain dict)
  - ✅ Fixed dead `size` param: `_worker_fetch` now actually downscales to requested size (avatars→64px, inline→MAX_WIDHT=800) before Gdk.Texture — was loading full-res textures into 40px widgets (~50x memory waste)
  - ✅ Bounded `VideoPlayer._cache` to 6 live players; on eviction tears down the oldest pipeline (`set_state(NULL)`) to release decoders/buffers
  - ✅ Reduced `ThreadPoolExecutor` 16 → 6 concurrent fetches (battery/data)
  - ✅ Removed `print` spam in `cache_manager.py` (perf + log flood)
- **Open / needs decision:**
  - 🔶 Feed `Clamp(maximum_size=600)` retains all prepended posts during a long scroll session, plus `window.event_widgets` never evicts — biggest remaining leak, needs feed-pruning design
  - 🔶 Video texture still churns every frame (new Gdk.Texture per frame); could drop MAX_DIM to ~640 for mobile or reuse a texture

### 2026-07-31 (stutter + animated profile pics pass)
- **Fixed non-GIF video stutter (root cause):**
  - ✅ Replaced appsink + manual per-frame Python conversion (`bytes()` copy → pixbuf → `scale_simple` → `Gdk.Texture` → `idle_add`) with **`gtk4paintablesink`** — GStreamer renders frames to a `Gdk.Paintable` natively in C with proper frame-dropping/sync. Zero Python per-frame work; position/seek/audio unchanged (operate on pipeline). Removed `on_sample`/`pull_sample`/appsink code entirely.
- **Animated profile pics (GIF/WebM):**
  - ✅ Added `autoplay` mode to `VideoPlayer`/`VideoLoader` (default off). Profile view now calls `load_and_play(..., autoplay=True)` so the 120px avatar actually plays + loops the animated media (EOS branch already seeks to 0).
  - 🔶 Feed avatars (40px, dozens per feed) stay **static first frame** for GIF and initials for WebM — animating all would be a battery/CPU trap. Visible-only animation is a future opt-in.
- **Caveats for testing:**
  - `gtk4paintablesink` must exist in the Flatpak gstreamer runtime (present in host `libgstgtk4.so`, ships in 24.08 extension). If video goes blank in the sandbox build, fall back to appsink path.
  - Profile-view avatar now autoplays on every profile open — verify it doesn't loop-hot on a long animation.

### 2026-07-31 (feed ordering + profile caching pass)
- **Fixed "stale DB entries load first" (ordering bug):**
  - `switch_feed` built the feed with `prepend` while iterating DB rows that are already newest-first (`ORDER BY created_at DESC`) — that put the **oldest at top**. Switched the DB-reload loop to `append`, so cached posts render newest→oldest top-to-bottom. Live events keep `prepend` (newest at top) — correct.
- **Fixed profile pics/names not loading or refreshing (root cause of animated-pic failure):**
  - `client.fetch_profile` used a one-shot `requested_profiles` **set** — a profile was requested once and never retried even if the relay returned nothing or data was incomplete. Replaced with a **TTL cache** (re-request after 600s) so failed lookups retry.
  - `main.on_profile_updated` was a no-op (`pass`) — profile metadata arrived but existing avatars/names were never re-rendered. Now refreshes name label + avatar image on every matching post widget.
- **DB efficiency:**
  - Added indexes `events(pubkey, kind, created_at)`, `events(created_at)`, `following(owner_pubkey)` — the feed query was a JOIN + ORDER BY over unindexed columns (full table scan on every load, slower as DB grows).
- **Open / needs decision:**
  - 🔶 `switch_feed` still wipes all posts + resubscribes on every refresh/auto-refresh (300ms timer) — jarring. A merge-only refresh (fetch new since last, don't wipe) is the follow-up.
  - 🔶 Feed avatars remain static first-frame for GIF / initials for WebM — animating all 40px avatars is a battery/CPU trap; visible-only animation is a future opt-in.

### 2026-08-02 (UX overhaul — Phase 1 quick wins)
Plan: `.hermes/plans/2026-08-02_gnostr-ux-overhaul.md` (10 tasks, 4 phases). Executing phase-by-phase, committing each task, updating this log.

- ✅ **Task 1 — @ mention inline spacing.** Root cause: `LINK_REGEX = (?:^|\s)(...)` consumed the leading whitespace during `re.split`, so mentions rendered as `text@user`. Changed to a lookbehind `(?<!\w)(...)` so the space stays in the text fragment → `text @user`. Verified across sample content (mention at start, mid, end, with punctuation, and http links — all correct). File: `src/gnostr/renderer.py`.
- ✅ **Task 2 — Load feed on startup (blank-feed-on-back fix).** Root cause: `MainWindow.__init__` never called `switch_feed`, so the feed root was empty until a sidebar item was clicked — going to Profile first then Back showed a blank feed. Fix: `__init__` now populates the feed from the DB right after login (`switch_feed(active_feed_type)` via idle_add), and `on_status_changed` re-runs `switch_feed` once on the first relay CONNECTED (startup subscribe is a no-op pre-connection, so this establishes the live sub). Guarded by `_feed_live` flag. File: `src/gnostr/main.py`.
- ✅ **Task 3 — Mobile scroll vs highlight + long-press copy.** Root cause: content labels used `selectable=True`, so touch drags on text grabbed selection instead of feeding the scroller. Fix: removed `selectable=True` from `_add_text`, `_text_label`, `_add_link` in renderer (links still clickable via `activate-link`). Added a `Gtk.GestureLongPress` on PostWidget that copies the whole post to the clipboard + "Copied post" toast; grouped it with the tap-to-open-thread gesture so a long-press doesn't also open the thread. Files: `src/gnostr/renderer.py`, `src/gnostr/ui/post_widget.py`.
- ✅ **Task 4 — Collapse long hero posts in thread view.** Long top posts (>500 chars) now render truncated with a "Show more"/"Show less" toggle so replies stay reachable. Refactored hero rebuild into `_rebuild_hero`; `_replace_hero` (thread refresh) keeps the current collapse state. File: `src/gnostr/ui/thread_view.py`.

### Phase 1 complete — quick wins shipped (Tasks 1-4). Starting Phase 2 (data completeness).

- ✅ **Task 5 — Fetch missing profile/mention/reply-author metadata.** `renderer._mention_name` now calls `client.fetch_profile(hex_pk)` when a mention's profile isn't cached, and `PostWidget` calls it for unknown post authors (covers feed, thread replies, and quote authors). `fetch_profile` is TTL-cached/deduped, and `on_profile_updated` already re-renders names/avatars + inline mention labels on arrival. Files: `src/gnostr/renderer.py`, `src/gnostr/ui/post_widget.py`.
- ✅ **Task 6 — Load missing reply parent context.** `ThreadView.load_parent_context` now fetches a parent via `client.request_once` when it's not cached (previously it silently no-op'd), and `on_event_received` renders it into `context_box` on arrival — including recursive grandparent fetches. Added `_pending_parents` tracking + `_render_arrived_parent`. File: `src/gnostr/ui/thread_view.py`.

### Phase 2 complete — data completeness shipped (Tasks 5-6). Starting Phase 3 (UX simplification).

- ✅ **Task 7 — Simplify video controls.** Removed the volume slider + `set_volume`; videos now play at full volume (`pipeline volume=1.0`, `_is_muted=False`) and users control loudness at the system level. Mute button moved adjacent to the progress bar (in the position row). Clicking the video frame toggles play/pause (`GestureClick` on video_area). Animated GIFs skip the seek bar + mute entirely (they loop, so those don't apply) and keep just play + the video. File: `src/gnostr/renderer.py`.
  - 🔶 Note: `src/gnostr/AGENTS.md` still says VideoPlayer uses appsink — stale since the gtk4paintablesink migration; flag for a later DOX pass.
- ✅ **Task 8 — Decorative @ mentions.** Mentions now render as **bold** `@name` links (theme-safe Pango `<span weight="bold">`), clearly distinct from body text. Applied in both `renderer.render()` and `main.on_profile_updated` (name refresh keeps the same styling). Avatar-in-mention flagged as a follow-up — it needs the `render()` → `Gtk.FlowBox` rework, which is the higher-risk Option B from the plan. Files: `src/gnostr/renderer.py`, `src/gnostr/main.py`.
  - 🔶 Follow-up: inline mention avatars via Gtk.FlowBox (Option B) — defer until after Task 10.
- ✅ **Task 9 — Follow/Unfollow toggle.** `ProfileView` now shows a pill Follow/Unfollow button on other users' profiles (hidden on your own), initialized from `get_following_list`. Toggling updates the DB via a new targeted `database.set_following()` and publishes a kind-3 contact list via new `client.publish_contacts()`. Files: `src/gnostr/ui/profile_view.py`, `src/gnostr/database.py`, `src/gnostr/client.py`.
- ✅ **Bugfix batch — 5 issues.** (1) **Collapse vanish:** hero + toggle now live in a dedicated `hero_section` sub-container; `_rebuild_hero` operates only on it (no fragile index math against the shared layout that also holds header/context/replies). (2) **Thread not scrollable:** the WHOLE thread is now one `Gtk.ScrolledWindow` (hero + context + toggle + replies), removed the nested replies ScrolledWindow. (3) **GIF blank after nav:** evicted players set `_pipeline=None`; `_fetch_player` cache-hit treats a None pipeline as a miss and rebuilds fresh; `_start_playback`/`toggle_play` guard against None pipeline and remove the dead widget before rebuilding. (4) **Removed play button** — the video frame is the play/pause control (lazy-start first tap, toggle later). (5) still open — choppy playback. Files: `src/gnostr/ui/thread_view.py`, `src/gnostr/renderer.py`.
- ✅ **UX batch 3 (6d429c5) — Show-more crash, box_remove CRITICAL, play-icon centering, first-frame latency.** (1) **Show-more crash:** `PostWidget.set_content` called `Gtk.Box.insert(rendered, 1)` — these bindings have NO index-based `insert()` → AttributeError on expand. Now removes ALL children and re-appends in order with the new rendered box in slot 1; header/footer re-appended as the same widget objects (no re-render). (2) **`gtk_box_remove` CRITICAL:** `insert_time_sorted` removed every collected child including the brand-new widget (not a child yet) → parent assertion. Guarded removes with `c.get_parent() is box`. (3) **Play icon centered:** placeholder is now a `Gtk.Overlay` with the icon dead-centered (valign CENTER) and the "Play video" label anchored at the bottom — no longer sits high in the box.
- ✅ **Video regression fix (1943d39).** (1) **High-speed playback** was caused by the speculative `sync=False/async=False` sink change (applied without a measurement, violating Task 10's measure-first rule) — removing the clock lets the sink render frames as fast as the decoder produces them instead of pacing to real time. **Reverted to DEFAULT sync/async.** (2) **GIF regression:** the GIF branch called `load_and_play` WITHOUT `autoplay=True` (introduced by lazy-load), so GIFs built the pipeline to PAUSED and never reached PLAYING — the spinner spun forever with no playback. Now passes `autoplay=True` to restore preroll+loop. Capture analysis: 0 evictions / 0 EOS / 0 codec errors / QOS=0 — the felt "freeze/resume" is NOT frame drops; the 2336 state-changed lines are per-element messages from a handful of builds (a complex playbin3 graph emits one message per element per transition), not repeated play/pause. The freeze source is still unmeasured — do NOT disable clock sync to chase it.
- ⏳ **Task 10 — Video stutter perf investigation.** **Lazy-load landed (0ada401):** builds dropped 23→9, preroll halved 3325→~800ms, 0 codec errors, 0 dropped frames — but playback **still choppy** with QOS=0, so it's not frame drops. New capture showed a **measurement regression**: only 1 real caps report of 9 — lazy-load autoplay races the state-change ahead of caps negotiation, so the inline caps query returned None. **Fix (deferred inspect):** `_inspect_pipeline` now runs via a 600ms `GLib.timeout_add` after first PAUSED/PLAYING so caps settle before querying. Latest capture: caps reports fire but **`video_caps=none` for all 6** — the 600ms defer helps GIFs (framerate 0/1) but REAL lazy-loaded videos race past 600ms to negotiate (preroll ~3.6s), so `get_current_caps()` still returns None for them; QOS stays 0 dropped frames, no evictions in this capture so no QOS summaries. The caps measurement needs a retry-until-non-None or a query-on-first-frame hook to correlate real-video resolution→choppiness.
- ✅ **Bugfix — Gtk-CRITICAL gesture-group assertion.** `post_widget.py` called `ctrl.group(lp)` *before* `lp` was attached to the widget, so `gtk_gesture_group()` (gtk_gesture.c:1587) compared `ctrl`'s widget (`self`) against `lp`'s NULL widget and asserted — firing on every non-hero post. Fixed by adding `lp` to the widget first, then grouping. `GestureSingle` does NOT auto-group (init only sets defaults), so renderer's lone `click_ctrl` was never implicated.

### 2026-08-08 (like button + relay feedback workstream)
Plan: root-cause the like button doing nothing, the thread-hero wrong icon, and the absence of relay submission feedback. Executed across 14 commits, verified working on-device.

- ✅ **Like button click never fired (root cause).** A `Gtk.GestureClick` CLAIM gesture on each footer action button (added to deny the card's open-thread gesture) competed with `Gtk.Button`'s internal click gesture and won on press — so `clicked` never fired and `_on_like` never ran. The like button silently did nothing regardless of keys or relays. **Fix:** removed the CLAIM gesture from the action buttons; the card's open-thread handler now checks the click target (`_on_card_clicked`) and skips opening the thread when the click is on an action button. Commit `a131851`. This was the missing piece — the earlier `set_keys` fix was necessary but not sufficient.
- ✅ **Like/repost/reply/follow no-op on saved-key startup.** `perform_login` never called `client.set_keys`, so the client's keys stayed None and every `publish_*` returned False ("No private key loaded"). Now pushes keys to the client, mirroring the fresh-login dialog path. Commit `8096ab0`.
- ✅ **Feed crash — `IconTheme.has_icon_pixbuf`.** GTK2/3-era method that doesn't exist on GTK4's `Gtk.IconTheme`; every PostWidget raised AttributeError and the feed never loaded. Switched to `has_icon()`. Commit `0afe54c`.
- ✅ **Thread hero wrong icon + no working like button.** Hero PostWidget (`is_hero=True`) never applied liked state (kept the constructor's outline star) nor wired `_on_like`. Now applies outline/filled + `.liked` class and wires the like button; repost/reply stay off the hero. Commit `8cb46fe`.
- ✅ **No relay submission feedback.** Added NIP-01 OK ack parsing (`parse_ok_message`), `publish-result` signal, `pending_publishes` tracking (event_id → label + 30s expiry), and `Adw.Toast` feedback ("Like ✓" / "Follow failed: <msg>"). Commits `148ae03`, `affb1da`.
- ✅ **Relay activity log.** Bounded `relay_log` deque (200) + `relay-log-updated` signal feeding a sidebar "Relay Activity" pane (collapsible, monospace, newest at top). Wired into publish/OK/timeout/connect/disconnect. Commits `9da60e7`, `f9fb395`.
- ✅ **Publish observability.** `relay.publish()` returns True/False and logs when a relay isn't connected or the send fails (was silently swallowed); `NostrClient.publish()` logs `EVENT <id> sent to N/M relay(s)`. Commit `700f7bf`.
- ✅ **Relay subscription spam.** `bad close: invalid subscription id length` + `too many concurrent REQs` NOTICEs fixed by tracking `active_sub_ids` per relay and only CLOSing IDs actually opened on that relay. Commit `700f7bf`.
- ✅ **Like visual.** Icon resolves against active icon theme with fallbacks; `.liked` accent CSS class toggles with state. Commit `93bf0ad`.
- 🔶 **Open — relay connection verification.** The latest output.txt showed zero relay-activity lines, traced to the CLAIM-gesture bug (like never published). After the fix, relays should connect and log. Verify on-device that OK acks appear in the Relay Activity pane; if relays still don't connect, investigate the WebSocket connection layer.
- 🔶 **Open — follower sync (new feature).** No explicit reconciliation between the DB `following` table and the relays' kind-3 contact list. Design documented in Current Workstream #2 — pull (reconcile DB from relay kind-3) + push (manual "Sync Followers" re-publish) + verification via Relay Activity pane.

### 2026-08-09 (NIP-19/NIP-21 nostr type support — naddr/long-form)
Plan: research the full NIP-19 bech32 type set, document gaps in TRACKING.md, then implement long-form (naddr) support. Committed docs first (`ea9ba4a`), then implemented across 4 phases.

- ✅ **Research (committed `ea9ba4a`).** Pulled authoritative NIP-19/NIP-21 from nostr-protocol/nips master. The complete bech32 set is npub/nsec/note/nprofile/nevent/naddr/nrelay. `nquote`/`nroom`/`nsite`/`nclientauth`/`nchannel` are community proposals, NOT in spec — correctly excluded. Only real gap was `naddr` (addressable event coordinate) + deprecated `nrelay`.
- ✅ **Phase 1 — decode layer (`nostr_utils.py`).** Added `decode_naddr` (kind:pubkey:d-tag + relays), `decode_nevent_full` (event id + relay/author/kind hints), `decode_nprofile` (pubkey + relays) via shared `_decode_tlv` parser (unknown TLVs ignored per NIP-19). `is_nostr_reference` now covers naddr/nrelay. Verified against real spec examples (naddr → kind 30023, pubkey, d-tag `18ff5416`, relay `wss://fiatjaf.com`).
- ✅ **Phase 2 — render layer (`renderer.py`).** `render()` routes `nostr:naddr` → `_add_naddr_card` (quote card resolved by coordinate), `nostr:nrelay` → plain link. `_add_nostr_card` honors the nevent relay hint. Both share `_build_quote_card` (DB check → fetch → register pending on `quote_widgets` → wire click); `_on_quote_clicked` opens thread for events, resolves coordinate for naddr.
- ✅ **Phase 3 — persistence (`database.py`).** `get_event_by_a(kind, pubkey, d_tag)` returns newest addressable event for a coordinate (kind+pubkey index scan, d-tag matched in Python from JSON tags). Existing `idx_events_feed(pubkey, kind, created_at)` covers the query — no new index.
- ✅ **Phase 4 — signal wiring + tests + DOX.** `client.py` emits `quote-event-received` (full event JSON) for EVERY kind after `save_event` — addressable events never hit the kind-1 `event-received` path. `main.py` `on_quote_event_received` matches pending cards by event id or coordinate and populates via `_build_quote_content`. **Fixed a latent bug:** note/nevent quote cards showed "Loading..." forever — `quote_widgets` was set but never consumed. Added `test_nostr_types.py` (7 tests: decode naddr/nevent/nprofile against spec examples + `get_event_by_a` DB lookup). DOX pass on `src/gnostr/AGENTS.md` + `tests/AGENTS.md`.
- ✅ **Verification.** 18 tests pass (7 new + 11 existing protocol/DB tests). Full suite: 46 passed, 1 pre-existing failure (`test_codec_support.py::TestVideoPlayer::test_load_and_play_success` — GStreamer state mock, unrelated, confirmed via git stash). black clean, flake8 clean on new code regions.
- 🔶 **Open — relay-hint-targeted fetch.** naddr/nevent relay hints are decoded but `request_once` fans out to all connected relays; targeting a specific hinted relay is a future optimization.

---

## Notes

### Environment Requirements
- Python 3.11+
- GTK4, libadwaita-1
- GStreamer 1.0 with plugins (uridecodebin, gtk4paintablesink available)
- pytest, black, flake8 (for development)

### Testing Commands
```bash
# Run tests with xvfb
xvfb-run pytest tests/

# Check GStreamer plugins
gst-inspect-1.0 | grep -E 'uridecodebin|gtk4paintablesink'

# Run linters
flake8 src/ tests/ --max-line-length=120
```

### CI Pipeline
- Runs on push to main branches
- Lints code with black and flake8
- Runs tests with xvfb
- Builds Flatpak bundle

### Pre-commit Hook
- Checks staged files only
- Runs black, isort, flake8 on Python files
- Runs tests for changed modules

---

## Next Steps
1. ✅ Performance optimization: downscale 4K frames to 1280px before texture conversion (done 2026-07-31)
2. ✅ Suppress GStreamer CRITICAL stderr noise: use `==` type checks instead of bitwise `&` (done 2026-07-31)
3. ✅ Gate debug 🎬 logging behind `_DEBUG_VIDEO` flag (done 2026-07-31)
4. ✅ Add unit tests for VideoPlayer lazy-start/autoplay contract (2026-08-29, `b113e06`)
5. 🔶 Feed pruning design: `Clamp(maximum_size=600)` + `window.event_widgets` never evict (biggest remaining leak); `switch_feed` wipe-and-resubscribe → merge-only refresh
