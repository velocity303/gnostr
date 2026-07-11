# Gnostr Development Tracking

This document tracks all bugs, issues, and problems that need fixing.

## Current Workstream

### 1. Codec Support for MP4/MOV Playback
**Status:** ✅ Implemented
**Description:** Ensure proper codecs are available for MP4 and MOV playback and ensure it renders well on feed and post views.

**Completed:**
- ✅ Verified GStreamer installation (uridecodebin, gtk4paintablesink available)
- ✅ Enhanced VideoPlayer with error handling
- ✅ Added codec error logging to bus watcher
- ✅ Tested codec detection for MP4, MOV, WebM, GIF

**Remaining:**
- [ ] Test with actual video files in production environment
- [ ] Add fallback error UI for codec failures
- [ ] Verify performance on mobile devices

**Testing:**
```bash
gst-inspect-1.0 uridecodebin
gst-inspect-1.0 gtk4paintablesink
```

---

### 2. Animated GIF Profile Images
**Status:** ✅ Implemented
**Description:** Enable animated gif profile images and it should work with both webm and gif.

**Completed:**
- ✅ Added video URL detection in profile_view.py
- ✅ Conditional rendering: VideoPlayer for animated, ImageLoader for static
- ✅ Proper sizing and layout for animated avatars

**Remaining:**
- [ ] Test with actual animated profile pictures
- [ ] Verify looping behavior
- [ ] Add fallback for non-animated fallback

---

### 3. CI Pipeline Implementation
**Status:** ✅ Implemented
**Description:** Based upon the established code, work with opencode to draft more wholistic unit tests that are tested in a way compatible with the ci.yaml that is described below. Implement this ci.yaml and make sure you can test the pipeline completely.

**Completed:**
- ✅ Created `.gitea/workflows/ci.yaml` for Gitea Actions
- ✅ Added linting steps (black, flake8)
- ✅ Added test runner with xvfb for GTK rendering
- ✅ Added system dependencies installation
- ✅ Added test_codec_support.py for codec testing

**Remaining:**
- [ ] Run CI pipeline on remote repository
- [ ] Verify Flatpak build step
- [ ] Add coverage reporting

---

### 4. Git Hooks and Linting
**Status:** ✅ Implemented
**Description:** Set up the appropriate git hooks and linting tools for yourself to ensure you can properly have the precommit hooks give you good output data that you can directly fix to ensure consistent performance and development flow by blending classic development pipelines with llm reasoning provided by you and opencode subagents.

**Completed:**
- ✅ Created `.git/hooks/pre-commit` script
- ✅ Configured black, isort, flake8 checks
- ✅ Added test runner to hook
- ✅ Set up error handling and feedback

**Remaining:**
- [ ] Test pre-commit hook manually
- [ ] Add hook installation instructions to README

---

## Issues Encountered

### Network/SSH
- [ ] Remote repository access via SSH (192.168.5.134:2222) is intermittent

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
- Identified animated GIF profiles as second priority
- Identified CI and linting as foundational needs
- Created TRACKING.md
- Implemented codec verification and error handling
- Enabled animated GIF/WebM profile images
- Created Gitea Actions CI pipeline
- Set up pre-commit hooks
- Added test_codec_support.py
- Updated README

---

## Notes

### Environment Requirements
- Python 3.11+
- GTK4, libadwaita-1
- GStreamer 1.0 with plugins
- pytest, black, flake8

### Testing Commands
```bash
# Run tests with xvfb
xvfb-run pytest tests/

# Check GStreamer plugins
gst-inspect-1.0 | grep -E 'uridecodebin|gtk4paintablesink'

# Run linter
flake8 src/ tests/
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
