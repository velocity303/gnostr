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
- `test_nip_builders.py` — protocol-layer NIP tag structure (reaction/repost/reply) — imports real `nostr_utils` (GTK-free) and asserts exact tag layout
- `test_nostr_types.py` — NIP-19/NIP-21 bech32 decoding (naddr/nevent/nprofile against spec examples) + `database.get_event_by_a` addressable-coordinate lookup — imports real `nostr_utils` (GTK-free) and the mocked-GLib Database
- `test_publish_ack.py` — NIP-01 OK ack parsing — imports real `nostr_utils.parse_ok_message` and covers accepted/rejected/string-bool/not-OK/short paths
- `test_following.py` — follow/unfollow DB persistence + `user_reaction` like-toggle query
- `test_profile_db.py` — profiles table persistence: full NIP-01/NIP-24 field round-trip, deprecated-field handling, schema migration, NIP-39 external identities + NIP-58 badges + badge-definition storage — imports the mocked-GLib Database
- `test_profile_nips.py` — profile NIP protocol helpers: NIP-05 verification (mocked urllib), NIP-39 external identities, NIP-58 profile badges, LUD-16 resolution — imports real `profile_nips` (GTK-free)
- `test_search_resolver.py` — search identifier resolver: npub/nprofile/nsec/hex/nostr: → pubkey — imports real `nostr_utils` (GTK-free)
- `test_search_dialog.py` — SearchDialog construction smoke test (mocked gi)
- `test_follow_sync.py` — follower sync: kind-3 pull reconciliation (`_handle_event` → `save_contacts`) + `sync_followers` push (DB list → kind-3, OK-ack tracked). NostrClient subclasses mocked GObject (imported class is a Mock), so tests extract the real methods from client.py via ast and bind them to a FakeClient
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
