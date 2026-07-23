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

### Status: PLANNED - Ready for Implementation

### Problem Statement
Current video playback in Gnostr has the following limitations:
1. **Auto-play**: Videos start playing immediately on load (can be disruptive)
2. **No user controls**: No way to pause, play, mute, or adjust volume
3. **YouTube links consumed**: YouTube URLs are resolved to raw streams and played directly, losing the ability to open the original YouTube page
4. **Complex pipeline**: Uses `uridecodebin` which is harder to control programmatically

### Target Features
1. **Player Controls**: 
   - Play/Pause toggle button
   - Mute/Unmute toggle button  
   - Volume slider (0-100%)
2. **Default State**: 
   - Videos start **paused** (no auto-play)
   - Videos start **muted** (volume = 0)
3. **YouTube Links**: 
   - Add "Open in YouTube" link button to original YouTube page
   - Preserve original URL for browser launch
4. **Better Pipeline**: 
   - Switch from `uridecodebin` to `playbin3` for simpler control API

---

### Implementation Plan

#### Phase 1: Refactor VideoPlayer Class
**File:** `src/gnostr/renderer.py`

**Changes:**
1. Replace `uridecodebin` pipeline with `playbin3`
2. Add state tracking: `_is_muted`, `_is_playing`, `_original_url`
3. Initialize pipeline with `PAUSED` state and `volume=0.0`
4. Expose control methods:
   - `toggle_play()`: Switch between PAUSED and PLAYING
   - `toggle_mute()`: Toggle mute state and update volume
   - `set_volume(value)`: Set volume (0.0-1.0)
   - `get_original_url()`: Return stored YouTube URL

**Code Structure:**
```python
class VideoPlayer:
    def __init__(self):
        self._pipeline = None
        self._original_url = None
        self._is_muted = True
        self._is_playing = False
        self._cache = {}
        self._lock = threading.Lock()
    
    def load_and_play(self, url, container, spinner, window_ref):
        # Fetch pipeline asynchronously
        # On ready: create playbin3 pipeline
        # Set state to PAUSED
        # Set volume to 0.0 (muted)
        # Store original URL if YouTube
    
    def toggle_play(self):
        if self._is_playing:
            self._pipeline.set_state(Gst.State.PAUSED)
        else:
            self._pipeline.set_state(Gst.State.PLAYING)
        self._is_playing = not self._is_playing
    
    def toggle_mute(self):
        self._is_muted = not self._is_muted
        volume = 0.0 if self._is_muted else 1.0
        self._pipeline.set_property("volume", volume)
    
    def set_volume(self, value):
        self._is_muted = (value == 0.0)
        self._pipeline.set_property("volume", value)
```

#### Phase 2: Add Control Bar UI
**File:** `src/gnostr/renderer.py` (in `_add_video` method)

**Changes:**
1. Create vertical box to hold video + controls
2. Add horizontal control bar below video with:
   - Play/Pause button (icon: `media-playback-start-symbolic` / `media-playback-pause-symbolic`)
   - Mute button (icon: `audio-volume-muted-symbolic` / `audio-volume-high-symbolic`)
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

**Code Structure:**
```python
def _add_video(self, box, url, window_ref):
    # Main container
    video_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    
    # Video display area
    video_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    # ... existing video loading code ...
    video_box.append(video_area)
    
    # Control bar
    controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    controls.set_margin_top(6)
    controls.set_margin_bottom(6)
    controls.set_margin_start(6)
    controls.set_margin_end(6)
    
    # Play/Pause button
    play_btn = Gtk.Button(icon_name="media-playback-start-symbolic")
    play_btn.connect("clicked", lambda b: player.toggle_play())
    controls.append(play_btn)
    
    # Mute button
    mute_btn = Gtk.Button(icon_name="audio-volume-muted-symbolic")
    mute_btn.connect("clicked", lambda b: player.toggle_mute())
    controls.append(mute_btn)
    
    # Volume slider
    vol_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL)
    vol_scale.set_range(0, 100)
    vol_scale.set_value(0)
    vol_scale.connect("value-changed", lambda s: player.set_volume(s.get_value() / 100))
    controls.append(vol_scale)
    
    # YouTube link (if applicable)
    if player._original_url and ("youtube.com" in player._original_url or "youtu.be" in player._original_url):
        yt_link = Gtk.LinkButton(uri=player._original_url, label="Open in YouTube")
        yt_link.set_halign(Gtk.Align.END)
        yt_link.set_hexpand(True)
        controls.append(yt_link)
    
    video_box.append(controls)
    box.append(video_box)
```

#### Phase 3: YouTube URL Handling
**File:** `src/gnostr/renderer.py`

**Changes:**
1. Modify `get_youtube_stream()` to return tuple: `(stream_url, original_url)`
2. Update `_add_video()` to store original URL in VideoPlayer instance
3. Ensure link button only appears for YouTube URLs

**Code Structure:**
```python
def get_youtube_info(url):
    """Extract stream URL and preserve original YouTube URL."""
    try:
        result = subprocess.run(
            ["yt-dlp", "-g", "-f", "best[ext=mp4]", url],
            capture_output=True, text=True, check=True
        )
        stream_url = result.stdout.strip()
        return stream_url, url  # Return both
    except subprocess.CalledProcessError:
        return url, url  # Fallback

# In _add_video:
stream_url, original_url = get_youtube_info(url)
player = VideoPlayer()
player._original_url = original_url  # Store for later
VideoPlayer.load_and_play(stream_url, ...)
```

#### Phase 4: Testing & Edge Cases
**Test Checklist:**
- [ ] Video starts paused (no auto-play)
- [ ] Video starts muted (volume = 0)
- [ ] Play/Pause button toggles correctly and updates icon
- [ ] Mute button toggles and updates icon
- [ ] Volume slider adjusts volume in real-time
- [ ] "Open in YouTube" link opens correct original page
- [ ] GStreamer pipeline handles errors gracefully
- [ ] Controls work with multiple videos on same page
- [ ] Non-YouTube videos don't show link button
- [ ] Pipeline state syncs with button states

---

### Files to Modify
| File | Changes |
|------|---------|
| `src/gnostr/renderer.py` | Refactor `VideoPlayer`, add control bar UI, update YouTube handling |

### Dependencies
- No new dependencies required
- Uses existing GTK4, GStreamer, yt-dlp

### Implementation Order
1. **Phase 1**: Refactor `VideoPlayer` to use `playbin3` with paused/muted defaults
2. **Phase 2**: Add control bar UI with play/pause, mute, volume
3. **Phase 3**: Add YouTube link button
4. **Phase 4**: Test and fix edge cases

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

### 2026-07-23 (Current)
- Fixed Flatpak build "Command 'gnostr' not found" error
  - Removed unnecessary PYTHONPATH finish-arg
  - Fixed `src/meson.build` to include `subdir('gnostr')`
- Fixed PyPI package download errors
  - Corrected `iniconf` → `iniconfig` package name
  - Updated pytest version from 9.1.0 → 9.1.1
  - Fixed all wheel URLs and SHA256 hashes
- Pinned yt-dlp to specific version (2026.7.4) with direct wheel URL
- Added GStreamer extension to Flatpak manifest for uridecodebin support
- **Planning Phase**: Enhanced video playback with controls

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
1. Complete Phase 1-4 of Enhanced Video Playback implementation
2. Test video controls on multiple platforms
3. Update documentation with new features
4. Add unit tests for VideoPlayer controls
