# DOX — tests/

## Purpose
Test suite for gnostr — pytest-based with xvfb for GTK rendering. Covers service logic, gateway connections, cache management, and codec support.

## Ownership
- `conftest.py` — test fixtures: mock GTK modules, mock client, mock database, xvfb setup
- `test_gateway.py` — gateway connection and event parsing tests
- `test_profile_service.py` — profile service tests
- `test_profile_metadata_service.py` — profile metadata service tests
- `test_resource_management.py` — cache manager and resource tests
- `test_codec_support.py` — codec/format support tests
- `test_simple.py` — basic import/smoke tests

## Local Contracts
- `conftest.py` MUST mock `gi.repository` modules (Gtk, Adw, GLib, Gdk, Gst, Gio) before any test imports real modules
- Tests never import real GTK — all GTK objects are mocked via unittest.MagicMock
- Mock objects must provide the attribute/property access that real GTK objects would
- `xvfb-run` is required for GTK tests — CI pipeline runs under xvfb
- Test files follow pytest conventions: `test_<name>.py` with `test_<function>` functions

## Work Guidance
- Use `monkeypatch` and `unittest.mock` for module-level mocking — never patch after import
- Mock scope: `conftest.py` defines `mock_gi_modules` fixture that patches sys.modules before test collection
- Database tests use temp files or in-memory SQLite — never production DB
- Gateway tests mock WebSocket connections — never connect to real relays
- Service tests mock both Database and Client — test logic, not integration
- Resource management tests verify cache eviction, not real file I/O
- Add new test files as `test_<feature>.py` and register in `meson.build` test targets

## Verification
- All tests pass under `xvfb-run pytest tests/`
- No test imports real GTK/GStreamer modules
- CI pipeline runs tests before build
- Test coverage targets: service layer (high), gateway (medium), UI (pending)
