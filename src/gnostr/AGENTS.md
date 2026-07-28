# DOX — src/gnostr/

## Purpose
Core application layer — GTK4 window, app lifecycle, Nostr client wiring, and signal handling. This is the backbone that connects UI widgets to the Nostr protocol layer.

## Ownership
- `main.py` — MainWindow class (window init, UI layout, signal handlers, login flow) and GnostrApp class (app activation)
- `window.py` — window.ui builder integration
- `client.py` — NostrClient: relay connections, subscriptions, event publishing
- `database.py` — Database: SQLite storage for events and profiles
- `dialogs.py` — LoginDialog, RelayPreferencesWindow, ComposeWindow
- `key_manager.py` — KeyManager: nsec storage/retrieval via libsecret
- `nostr_utils.py` — bech32 encode/decode, key derivation, event signing
- `connection_status.py` — relay status enum
- `renderer.py` — ContentRenderer, ImageLoader, VideoPlayer, VideoLoader

## Local Contracts
- `main.py` owns app startup and window construction. All widget instantiation happens here
- Window layout: Adw.ToastOverlay → Adw.ViewStack → split_view. FAB floats on content-area Gtk.Overlay, not a global overlay — toast overlay stays constrained to window size
- `client.py` is the sole relay communication layer — UI code never opens WebSockets directly
- Subscription management: `subscribe()` CLOSEs the previous subscription before sending a new REQ to prevent relay REQ floods
- `renderer.py` handles ALL content display rendering (text, images, video, nostr cards). UI widgets call `ContentRenderer.render()` and insert the returned box
- `database.py` is the single persistence layer — no other module writes to SQLite
- `key_manager.py` is the single key storage layer — no plaintext key files anywhere
- `dialogs.py` owns all modal dialogs. Call `.present()` on the dialog, never construct GTK windows inline

## Work Guidance
- MainWindow connects client signals in `__init__`: event-received, status-changed, contacts-updated, profile-updated, metrics-updated
- Login flow: KeyManager.load_key() → perform_login() → switch stack to "app" view
- Feed switching: switch_feed() clears post box, fetches from DB or subscribes via client
- All async image loading goes through ImageLoader thread pool (max 16 workers, cache in dict)
- VideoPlayer uses `playbin3` GStreamer pipeline, starts paused+mut ed, controls via toggle_play/toggle_mute/set_volume
- Error handling: renderer wraps everything in try/except, shows dim-label on failure
- Do NOT import `gi.repository` directly in test files — mock them in conftest.py

## Verification
- `pytest tests/test_gateway.py tests/test_profile_service.py tests/test_profile_metadata_service.py tests/test_resource_management.py` — core service tests pass
- No import errors for `gi` module in test suite
- MainWindow initializes without GTK assertion failures

## Child DOX Index
| Path | Scope |
|------|-------|
| `src/gnostr/gateway/AGENTS.md` | Relay connection gateway |
| `src/gnostr/service/AGENTS.md` | Feed/profile services |
| `src/gnostr/ui/AGENTS.md` | GTK widgets |
| `src/gnostr/util/AGENTS.md` | Cache and connection state |
