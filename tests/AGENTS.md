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
- `test_feed_pagination.py` — following-feed keyset pagination (`database.get_feed_following`): first page, full coverage exactly-once, stability under new events mid-scroll, equal-created_at tiebreak, short-page end-of-DB, no-OFFSET source guard, EXPLAIN QUERY PLAN index usage — imports the mocked-GLib Database
- `test_feed_model.py` — FeedModel (workstream #0, task 2): widget-agnostic feed window. load_first/load_older keyset cursor, end-of-DB sentinel, newest-side eviction that never touches the cursor, live-prepend buffer + flush, reset, max_window=1 / page_size=1 boundaries — fully mocked Database (no SQLite, no GTK)
- `test_feed_gtk_real.py` — tier-2 real-GTK feed harness: drives the actual `FeedView` + `FeedModel` under a real `Gtk.Application` (xvfb) — load_first, load_older, near-top re-pull + anchor restore, live pill/flush, end-of-DB sentinel, window invariants. Self-skips (exit 77) when GTK4/Adw or a display is absent, so it is a no-op in CI without xvfb
- `test_clipboard_gtk_real.py` — tier-2 real-GTK clipboard harness (issue #33): `ExportKeyDialog.on_copy` against the real Gdk clipboard under xvfb. Same self-skip (exit 77) rule
- `test_dm_recipient_gtk_real.py` — tier-2 real-GTK recipient-picker harness (Phase A fixback): `DmRecipientView` constructed under real GTK — filter, paste-pin, activate, own-key rejection, empty-follows restore. Same self-skip (exit 77) rule
- `test_profile_db.py` — profiles table persistence: full NIP-01/NIP-24 field round-trip, deprecated-field handling, schema migration, NIP-39 external identities + NIP-58 badges + badge-definition storage — imports the mocked-GLib Database
- `test_profile_nips.py` — profile NIP protocol helpers: NIP-05 verification (mocked urllib), NIP-39 external identities, NIP-58 profile badges, LUD-16 resolution — imports real `profile_nips` (GTK-free)
- `test_search_resolver.py` — search identifier resolver: npub/nprofile/nsec/hex/nostr: → pubkey — imports real `nostr_utils` (GTK-free)
- `test_search_dialog.py` — SearchDialog construction smoke test (mocked gi)
- `test_follow_sync.py` — follower sync: kind-3 pull reconciliation (`_handle_event` → `save_contacts`, ANY author reconciles that owner's rows) + `sync_followers` push (DB list → kind-3, OK-ack tracked) + `fetch_contacts_for` (any author's kind-3 sub). NostrClient subclasses mocked GObject (imported class is a Mock), so tests extract the real methods from client.py via ast and bind them to a FakeClient
- `test_follows_view.py` — FollowersListView logic: ast-extracts the real methods from followers_list_view.py (the gi mocks make the imported widget class a Mock) and binds them to a FakeView — reload builds one row per followed pubkey, owner-matched `contacts-updated` self-refresh (foreign owners ignored), refresh → `fetch_contacts_for(owner)`, row activation → `show_profile`, own-vs-foreign title wording
- `test_dm_relay_targeting.py` — G15 NIP-17 relay targeting (spec req 4): ast-extracts NostrClient/MainWindow/DMThreadView methods onto a FakeClient with FakeRelays — no-10050 peer aborts with ZERO publish calls + `dm-unreachable` → MainWindow toast "Recipient is not reachable for DMs" + failed optimistic bubble; peer with 10050 publishes ONLY to those relays; TTL cache (no re-fetch; negatives not cached); concurrent sends share one lookup; own 10050 default = 2 write relays. GLib stub must be installed AFTER exec(client.py) — client.py's own `from gi.repository import GLib` rebinds the name to conftest's Mock.
- `test_dm_gtk422_api.py` — static guard: source-scans `dm_inbox_view.py`/`dm_thread_view.py` for GTK/Adw APIs removed in the Librem 5's GTK 4.22 runtime (`set_tightening_ratio`, `set_scroll_on_focus`, `set_enable_ime`, `.get_children(`) — each one raised AttributeError on-device despite the mocked suite passing; extend BANNED when a new removed API is hit.
- `test_dm_ui_wiring.py` — G3 DM UI wiring (headless): ast-extracts real methods from dm_inbox_view.py / dm_thread_view.py / client.py / main.py and binds them to Fakes — inbox skeleton removed on first gateway result only, rows from `get_conversations`, row tap → `show_dm_thread`; thread Send disabled-while-blank, optimistic sending bubble (opacity 0.6, keyed by event id) solid-on-ack / failed+retry-on-reject, mark-read on open; `client.publish_dm` stores the self-copy under the RECIPIENT's conversation (unread 0) + emits `dm-conversations-updated` (needs a real-curve-point peer hex — NIP-44 rejects off-curve keys); `ensure_dm` opens the kind-4 sub; sidebar badge show/hide + `MainWindow.update_dm_badge` reads `gateway.total_unread`
- `test_onboarding_keys.py` — generate_keypair (SEC2 range, nsec round-trip, randomness) + `dialogs.resolve_login_input` (hex/nsec/ncryptsec/wrong-password) — pure pytest
- `test_nip49.py` — NIP-49 ncryptsec: official NIP decryption vector, wrong-password/tamper raises, round-trip incl. unicode password — pure pytest
- `test_nip06.py` — NIP-06 derivation: 6 official BIP-39 vectors (trezor vectors.json, passphrase TREZOR), entropy round-trip, 2 official NIP-06 keypair vectors, BIP-32 test vector 1, checksum/unknown-word/word-count rejection, pubkey + nsec agreement with nostr_utils — pure pytest
- `test_key_health.py` — key_health store round-trip (GLib.get_user_data_dir monkeypatched to tmp), no-key-material assertion, corrupt-file reads as None, plus sidebar backup-reminder copy via ast-extracted `_refresh_key_health` + FakeView
- `test_onboarding_wizard.py` — CreateAccountWindow gate rule: ast-extracts `_on_gate_confirmed` (gi mocks make the class unconstructable) + FakeView; type-back gate must block/allow the stack advance
- `test_relay_log_wrap.py` — Sidebar Relay Activity wrap regression (G13 mobile overflow): AST-reads the `relay_log_label` construction contract (wrap=True, wrap_mode WORD_CHAR, max_width_chars + hexpand, selectable, no ellipsis, ScrolledWindow NEVER/AUTOMATIC + max_content_height 180) and ast-extracts `append_relay_log` onto a fake self — a 100+ char relay line must append intact, newest on top
- `test_relay_usage_economy.py` — relay usage economy (issue #26): ast-extracts NostrRelay/NostrClient methods onto a fake self with a fake WebSocketApp — token-bucket REQ pacing (burst free, then 18/s), queue dedup (same sub_id twice = 1 REQ, window expiry lets TTL refetch through), CLOSED handling (refused sub dropped everywhere, rate-limit reason surfaced once), randomized exponential reconnect backoff (reset on CONNECTED, never wakes a parked relay), live-sub replay on reconnect (snapshots never replay; a sub made while down is recorded and replayed at CONNECTED), idle parking + wake (streaming relays never parked; a park is silent in connection status), and the check_connections sweep driving both
- `test_simple.py` — basic import/smoke tests

## Local Contracts
- `conftest.py` MUST mock `gi.repository` modules (Gtk, Adw, GLib, Gdk, Gst, Gio) before any test imports real modules
- `conftest.py` sets `collect_ignore_glob = ["*_gtk_real.py"]` (issue #35): tier-2 real-GTK harnesses call `sys.exit(77)` at import time when GTK4/Adw or a display is absent, and pytest imports every `test_*.py` during collection — the SystemExit aborts the whole run as INTERNALERROR. Real-GTK harnesses are NEVER collected by pytest; they run standalone under `xvfb-run` via their `meson.build` test targets. Every new `test_*_gtk_real.py` harness inherits this rule automatically.
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
