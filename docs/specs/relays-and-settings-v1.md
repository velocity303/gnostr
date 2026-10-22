# gnostr relays-and-settings-v1 — relay management + settings/sidebar restructure

Status: DRAFT for usability review (tux) before implementation.

## Motivation

Relay handling today is a flat, unordered `set[str]` with no roles: every
connected relay gets every publish and every REQ. Users cannot express
"post to my fast relays, read from these too, DMs only through X". At the
same time the sidebar has accreted admin furniture (Relay Activity log, key
health, Back Up Key, Logout button) that crowds the everyday navigation.
This spec fixes both: role-aware, ordered relays with a real management UI
in Settings, and a sidebar trimmed to daily-use items.

## Current state (verified against main @ 6c17d20)

- `client.py`: `self.relay_urls = set(DEFAULT_RELAYS)`; persisted via
  `save_config()` as `{"relays": [...]}` (unordered JSON list). No ordering,
  no roles, no per-relay metadata.
- Publish paths: `publish_post`, `publish_reaction`, `publish_repost`,
  `publish_contacts`, `publish_profile`, `publish_relay_list` broadcast to
  all connected relays; `publish_dm` already targets peer relays via
  `_publish_dm_targeted` (NIP-17). `publish_to_relays(event, urls)` exists
  as a generic helper.
- `RelayPreferencesWindow` (dialogs.py): single-page `Adw.PreferencesWindow`
  with groups: Sync (Import from Profile), Active Relays (add entry +
  per-row remove), Media Upload, Proxy, Tor.
- Sidebar (ui/sidebar.py): signer row → Relay Activity expander (monospace
  log) → key-health label → "Back Up Key" button → "Logout" button →
  status row (tor icon + connection emoji). Logout fires immediately with
  no confirmation (main.py `on_logout_clicked`).
- NIP-65: `publish_relay_list` emits kind-10002 without read/write markers.

## Scope

### A. Data model (client.py)

New per-relay entry persisted in the same config file under a new key
`relay_entries` (list, order = user preference):

```json
{"url": "wss://nos.lol", "roles": ["read", "write"], "enabled": true}
```

- Roles: `read` (REQ/feed queries), `write` (publishes), `dm` (DM
  targeting). A relay may hold any combination. Default for new relays:
  `["read", "write"]`.
- `enabled: false` keeps the entry but never connects (parked; distinct
  from the idle-park mechanism — user intent survives reconnects).
- Migration: on load, if `relay_entries` is absent, derive it from the
  legacy `relays` list (sorted for determinism, roles `["read","write"]`)
  and save. Keep `relay_urls` as a derived property
  (`{e.url for e in entries if e.enabled}`) so existing call sites and
  tests keep working unchanged.
- Routing changes at publish time:
  - posts/reactions/reposts/profile/contacts → relays with `write`.
  - feed/REQ subscriptions → relays with `read`.
  - DMs → `dm`-role relays first; if none, keep current NIP-17 peer-relay
    targeting; if that yields nothing, fall back to `write` relays.
  - Priority order = list order (used for fallback ordering and display).
- kind-10002 publish gains NIP-65 markers: `["r", url, "read"]` /
  `["r", url, "write"]` per entry roles (plain `["r", url]` when both).
- Import-from-profile merges into entries (new relays appended with default
  roles; existing entries keep their roles/order).

### B. Settings restructure (dialogs.py)

`RelayPreferencesWindow` becomes multi-page (Adw.PreferencesWindow native
sidebar/stack tabs — on phones it renders as a page stack with a back
button, which is the desired mobile behavior):

1. **General** — Sync group (Import from Profile) + key backup section
   (moved from sidebar): key-health label + "Back Up Key" button.
2. **Relays** — one `Adw.ActionRow` per entry, in preference order:
   - Title: relay URL (host part emphasized where possible).
   - Suffixes: three compact role toggles (R / W / DM as `Gtk.ToggleButton`
     with tooltips — switches would need three rows per relay and blow up
     the row height on a 360px screen), then a remove button.
   - Up/down arrow buttons for ordering (touch-friendly; drag-and-drop in
     a PreferencesWindow is fiddly on phosh — revisit later if desired).
   - `Adw.ExpanderRow` per relay for details: connection state, last error,
     last publish result.
   - "Add New Relay" row stays at the bottom; new relays append to the end.
   - Relay Activity log moves here as a collapsible group at the bottom of
     the page (fed by the existing `relay-log-updated` signal).
3. **Media Upload** — existing upload group, unchanged.
4. **Network** — existing Proxy + Tor groups, unchanged.

Every mutation (role toggle, reorder, enable, add, remove) persists via
`save_config()` and re-publishes kind-10002 (debounced, same pattern as
today's relay add/remove).

### C. Sidebar trim (ui/sidebar.py, main.py)

- Remove: Relay Activity expander, key-health label, "Back Up Key" button,
  "Logout" text button.
- Status row becomes: `[tor icon] [network status icon] [logout icon]` —
  all same-size symbolic icons, inline. Logout icon:
  `system-log-out-symbolic`, tooltip "Log out".
- The status row is HORIZONTALLY CENTERED in the sidebar footer
  (`status_row.set_halign(Gtk.Align.CENTER)`). On the FLX1 the rounded
  bottom bezel clips bottom-left content, so a left-aligned icon cluster
  is partially hidden; centering keeps all three icons visible. The row
  must stay centered even when the tor icon is hidden (2 icons) — center
  the cluster, don't reserve slots.
- Logout click → `Adw.AlertDialog` ("Log out of gnostr?" / body: "Your
  feed cache stays on this device; you'll need your key or password to sign
  back in.") with destructive "Log out" action → existing
  `main.on_logout_clicked()` flow.
- `append_relay_log` stays on the sidebar class API but becomes a no-op
  shim forwarding to the settings window when open (settings window
  subscribes to `relay-log-updated` directly; main.py keeps emitting).
- If the settings window is closed while a backup/relay-log action is
  pending, nothing breaks — those widgets are owned by the window.

### D. Out of scope (explicit)

- Drag-and-drop relay reordering (buttons first; measure on phosh).
- Per-relay NIP-11 capability UI beyond the details expander.
- Relay suggestions/discovery ("popular relays" browser).
- Changing the NIP-17 DM initiation flow itself (only its relay selection).

## Acceptance criteria

1. Relays page shows entries in preference order; up/down reorders persist
   across restarts; role toggles persist and are reflected in the
   kind-10002 event sent on next publish.
2. A relay with `write` only receives no feed REQs; `read` only receives no
   EVENTs (verifiable via Relay Activity log lines, which gain the role
   tag, e.g. `[w] publish OK`).
3. DMs route to `dm`-role relays when present; behavior unchanged when no
   relay has the `dm` role.
4. Legacy config (no `relay_entries`) migrates on first load without
   losing relays; `relay_urls` property keeps all existing tests green.
5. Sidebar shows only: navigation, signer row (when signer active), status
   row with the three icons, horizontally centered in the footer (FLX1
   bezel must not clip any icon). Logout requires confirmation; cancel
   keeps the session.
6. Settings opens on the last-used page; all four pages reachable on a
   360×648 logical viewport (Librem 5) without horizontal scrolling.
7. Full pytest suite green; new tests for: migration, routing by role,
   10002 markers, reorder persistence, logout confirmation wiring.

## Usability review questions for tux

1. R/W/DM toggle buttons vs switches vs a per-relay popover with checkboxes —
   which reads best at 360px width?
2. Is "Relay Activity" better as the bottom of the Relays page (proposed)
   or its own Settings page?
3. Logout icon placement: right end of the status row (proposed) vs a
   menu. Destructive-action distance: is a single confirmation dialog
   enough for a key-bearing app?
4. Page naming: General / Relays / Media Upload / Network — clearer names?
5. Anything in the sidebar trim that a daily user would miss (key backup
   visibility is the main risk — key-health warnings currently live there).
