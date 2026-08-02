# Gnostr Development Tracking

This document tracks all bugs, issues, and problems that need fixing.

## Current Workstream

### 1. CI Pipeline Implementation (FIXING NOW)
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

### 2. Test Import Fix (CRITICAL)
**Status:** In Progress - Fixing conftest.py

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

### 3. Package Structure Fix
**Status:** Pending

**Description:** The `src` directory is not a proper Python package (missing `__init__.py` or incorrect structure).

**Root Cause:**
- Tests import `from src.renderer import ContentRenderer`
- Python cannot find `src` as an importable module

**Fix:**
- Ensure `src/__init__.py` exists and makes `src` an importable package
- OR adjust test imports to use `from . import renderer` style

---

### 4. GTK4 API Compatibility
**Status:** Pending

**Description:** Tests expect GTK4 API but might be using GTK3 patterns.

**Root Cause:**
- `Box.get_children()` in GTK4 returns a list directly (not a method call)
- Tests incorrectly call `box.get_children()` as a method

**Fix:**
- Update test expectations to match GTK4 API
- Use `box.get_children()` as property, not method call

---

### 5. GStreamer Pipeline Issues
**Status:** Pending

**Description:** `uridecodebin` element may not be available in CI environment.

**Root Cause:**
- GStreamer plugins missing or version mismatch
- Tests mock Gst but pipeline creation fails

**Fix:**
- Ensure CI installs required GStreamer plugins (`gstreamer1.0-plugins-good`, `gstreamer1.0-plugins-bad`)
- Add fallback for missing elements

---

### 6. Pytest Fixture Scope Mismatch
**Status:** In Progress

**Description:** `mock_renderer_modules` fixture defined with `scope="module"` but uses `monkeypatch` which is function-scoped.

**Fix:**
- Change fixture scope to `function`
- OR remove `monkeypatch` dependency

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

### Network/SSH - Remote repository access via SSH (192.168.5.134:222) is intermittent

### Code Quality
- [ ] Several flake8 errors in existing code (E722, F841, F541, E501)
- [ ] No comprehensive test coverage for UI components
- [ ] No type hints in most files

### Documentation
- [ ] README lacks detailed build/run instructions
- [ ] No contribution guidelines

---

## Progress Log

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

- ⏳ Task 7 — Simplify video controls (no volume, GIF cleanup, click-toggle). Not started.

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
4. 🔶 Add unit tests for VideoPlayer controls
