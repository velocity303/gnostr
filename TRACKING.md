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

**Description:** The test environment fails because it cannot import the actual `gi` module required by `renderer.py`. All tests fail with `ModuleNotFoundError: No module named 'gi'`.

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
- Python cannot find `src` as a module

**Fix:**
- Ensure `src/__init__.py` exists and makes `src` an importable package
- Or adjust test imports to use `from . import renderer` style

---

### 4. GTK4 API Compatibility
**Status:** Pending

**Description:** Tests expect GTK4 API but might be using GTK3 patterns.

**Root Cause:**
- `Box.get_children()` in GTK4 returns a list directly (not a method call)
- Tests incorrectly call `box.get_children()` as method

**Fix:**
- Update test expectations to match GTK4 API
- Use `box.get_children()` as property, not method call

---

### 5. GStreamer Pipeline Issues
**Status:** Pending

**Description:** `uridecodebin` element may not be available in CI environment.

**Root Cause:**
- GStreamer plugins missing or version mismatch
- Tests mock GStreamer but pipeline creation fails

**Fix:**
- Ensure CI installs required GStreamer plugins (`gstreamer1.0-plugins-good`, `gstreamer1.0-plugins-bad`)
- Add fallback for missing elements

---

### 6. Pytest Fixture Scope Mismatch
**Status:** In Progress

**Description:** `mock_renderer_modules` fixture defined with `scope="module"` but uses `monkeypatch` which is function-scoped.

**Fix:**
- Change fixture scope to `function`
- Or remove `monkeypatch` dependency

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
- Identified animated GIF profile images as second priority
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

# Run linter
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
