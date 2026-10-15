# Draft Spec: DM-initiation UX + startup feed freshness

**Author:** @tux (GTK/libadwaita researcher) · **Status:** draft for @codecat / Gatekeeper
**Depends on:** nothing new — rides the shipped DM stack (`docs/specs/nip44-nip17-dms.md`) and the FeedModel window contract (workstream #0).
**Runtime floor:** GTK 4.22 / libadwaita ≥1.4 (Librem 5). Every API named below is verified against the 4.22/Adw-1 docs or is already live in this codebase (see Evidence). Banned and NOT used: `Adw.Clamp.set_tightening_ratio`, `Gtk.ScrolledWindow.set_scroll_on_focus`, `Gtk.Entry.set_enable_ime`, `Gtk.Widget.get_children`.

---

## Phase A — DM-INITIATION UX — owns REQ-A: "a clear 'New DM' affordance AND a simplified path to DM someone from the followers list"

### A1 — 'New Message' button on the DM inbox

**Change:** `src/gnostr/ui/dm_inbox_view.py`, `DMInboxView.__init__` toolbar row (`back_row`).
Before: back + refresh buttons only. After: a third flat button `btn_new` appended after refresh — `Gtk.Button(icon_name=<resolved>, css_classes=["flat"])`, tooltip "New message", icon resolved against the active theme with the house `_resolve_fab_icon` pattern (candidates: `mail-message-new-symbolic` → `chat-message-new-symbolic` → `list-add-symbolic`; first `Gtk.IconTheme.get_for_display(display).has_icon(name)` hit wins — do NOT hard-code, same class of bug as the FAB glyph fix). Click → `main_window.show_dm_recipient()`.
`src/gnostr/main.py`: new method `show_dm_recipient()` mirroring `show_dm_inbox` — import `DmRecipientView`, construct, wrap in `Adw.NavigationPage(title="New Message", child=view)`, `self.content_nav.push(page)`.

**Why NavigationPage, not a dialog:** every DM surface in this app is a nav page (`show_dm_inbox`, `show_dm_thread`); a modal `Adw.Window` on Phosh loses the shared back-button contract and the OSK re-parents worse inside a modal. The picker needs no modal semantics — it mutates nothing until a row is tapped. Tradeoff named: a dialog would keep the inbox visible behind it, but the inbox is empty in exactly the case where New DM matters, so there is nothing worth keeping behind.

### A2 — Recipient picker: dedicated page `DmRecipientView`

**Change:** new file `src/gnostr/ui/dm_recipient_view.py` + add to `src/gnostr/ui/meson.build` `install_data` (missing-file-in-meson is a known Flatpak startup crash class — `src/gnostr/AGENTS.md`).

Layout (top→bottom), all inside the house pattern (plain `Gtk.Box` root, back row upper-left like `FollowersListView`/`DMInboxView`):
1. Back row: `Gtk.Button(icon_name="go-previous-symbolic", css_classes=["flat"])` → `content_nav.pop()`.
2. `Gtk.SearchEntry` (`placeholder_text="Search follows, or paste npub / hex"`, `hexpand=True`). GTK 4.0 core widget; read text via the `Gtk.Editable` interface (`get_text()`), filter on the `search-changed` signal (debounce not required — the list is in-memory).
3. `Gtk.ScrolledWindow` (policy NEVER/AUTOMATIC, vexpand) → `Adw.Clamp` (`set_maximum_size(720)`, `set_tightening_threshold(504)` — same numbers as the inbox) → `Gtk.ListBox` (`set_selection_mode(NONE)`, `set_activate_on_single_click(True)`, `add_css_class("boxed-list")`).
4. Rows: `Adw.ActionRow(title=name, subtitle=f"{npub[:16]}…")`, `set_size_request(-1, 64)`, `add_prefix(Adw.Avatar(size=40, text=name, show_initials=True))` + `ImageLoader.load_avatars` for the picture (identical row shape to `DMInboxView._make_row`). Data: `db.get_following_list(main_window.pub_key)` — the follows list IS the picker dataset. Kick `client.fetch_profile(pk)` per row so names/avatars fill live (same TTL-cached pattern).
5. Row tap → `main_window.show_dm_thread(pk)`. The picker page stays under the thread; back returns to it (stack semantics free via `Adw.NavigationView`).

Filter behavior (view-local, no business logic): case-insensitive substring on display name and on `hex_to_npub(pk)` prefix. Identifier paste path: if `nostr_utils.resolve_profile_identifier(text)` returns a pubkey (npub/nprofile/nsec/hex/nevent — already the `SearchDialog` resolver), pin a synthetic first row `Message <npub[:16]>…` whose tap → `show_dm_thread(resolved_pk)`. This makes DM-to-anyone reachable without a relay search; the picker never queries relays.

Empty follows list: replace the list with `Adw.StatusPage(icon_name="system-users-symbolic", title="No follows yet", description="Follow some people, or paste an npub above to start a conversation.")` — StatusPage's own description label covers the paste hint; no extra widgets.

**Why a dedicated picker instead of a 'select' mode on `FollowersListView`:** `FollowersListView` is owner-keyed and reused for foreign profiles (ProfileView "Following N"); a mode flag would branch its row-activated handler and its title/sync chrome for a mode that is meaningless for foreign owners. The picker is ~120 lines reusing the inbox row shape. Tradeoff named: row-shape duplication across three views vs. a mode flag polluting a widget contract that currently reads cleanly ("widgets render data"); duplication wins because the row shapes are already duplicated between inbox and follows and share nothing but style.

### A3 — DM-from-followers shortcut

**Change:** `src/gnostr/ui/followers_list_view.py`, `FollowersListView._make_row`.
Before: row tap → `show_profile`. After: unchanged row tap, plus a suffix `Gtk.Button(icon_name=<resolved mail/chat glyph, same resolver as A1 — factor it into a shared helper, e.g. `ui/icons.py:resolve_icon(candidates)`, and reuse from main.py's FAB resolver>, css_classes=["flat"])`, `set_size_request(48, 48)` (48px touch contract, same as dm_thread_view Retry), `set_valign(Gtk.Align.CENTER)`, tooltip "Send message", click → `main_window.show_dm_thread(row.pubkey)`. The button click must not also activate the row: `Gtk.Button` claims the press inside itself (house rule: do NOT add CLAIM gestures to rows — see PostWidget footer-button lesson in `ui/AGENTS.md`); the button's own `clicked` fires and the row's `row-activated` does not.
Skip the suffix on the owner's own row set only if `pk == main_window.pub_key` (don't offer DM-to-self).

This makes the followers list the simplified DM path without a second list UI: Follows → tap the bubble on a row → thread. A2's picker is the same dataset with search; A3 is the in-place shortcut on the existing list. Both land on `show_dm_thread` — one entry point, no new state.

### A4 — Inbox empty state

**Change:** `src/gnostr/ui/dm_inbox_view.py`, `reload()` zero-conversation branch.
Before: a dim `Adw.ActionRow(title="No messages yet")` inside the ListBox. After: `Adw.StatusPage(icon_name="mail-unread-symbolic", title="No messages yet", description="Start a conversation — tap New Message.")` with `set_child(Gtk.Button(label="New Message", css_classes=["pill","suggested-action"]))` wired to `show_dm_recipient`. The StatusPage replaces the ListBox content region (swap the clamp child, or hide the scroll and show the StatusPage as a sibling — pick hide/show siblings so the ListBox + skeleton machinery is untouched).
Skeleton contract preserved: the StatusPage renders only when `_gateway_result_seen` is True AND `convs` is empty; no gateway → keep the 3 shimmer rows (never fake an empty result).

### A5 — Reachability feedback (no change, contract restated)

`dm-unreachable` → MainWindow toast already exists (`main.py:on_dm_unreachable`); `DMThreadView` fails the optimistic bubble with inline Retry. The picker does NOT pre-check kind-10050 — the async lookup + 5 s timeout in `client.publish_dm` is the single decision point; a widget-side pre-check would duplicate protocol logic into a widget (contract violation).

**Not-in-scope (Phase A):** no relay-backed name search (NIP-50) in the picker; no group DMs; no DM from PostWidget/quote cards; no changes to `DMThreadView` composer; no sidebar row changes (Messages row already exists with its badge); no settings UI for kind-10050.

---

## Phase B — FEED-STARTUP-FRESHNESS — owns REQ-B: "the latest posts load efficiently on every startup"

### B1 — One bounded freshness REQ on first relay CONNECTED (following feed)

**Change:** `src/gnostr/main.py`, `on_status_changed` first-CONNECTED block (the `not self._feed_live` branch, ~line 446).
Before: re-runs `switch_feed` (re-subscribes `sub_following` `{"kinds":[1],"authors":contacts[:300],"limit":50}`) and pulls kind-3. The REQ has no `since`, so a relay that answers serves its newest 50 — but those trickle in through `prepend_new` and, because `at_top` is True after `load_first`, each one is flushed inline (`on_event_received` → `flush_new`), so the viewport jumps repeatedly as stragglers land, and if the live sub's events are slow the user sits on days-old cached posts.
After: in the same block, after the `switch_feed` idle hop, call a new `self._pull_feed_freshness()`:

```python
def _pull_feed_freshness(self):
    if not self.pub_key:
        return
    contacts = self.db.get_following_list(self.pub_key)
    if not contacts:
        return  # deferred: re-armed on first contacts-updated (B3)
    newest = 0
    m = self.feed_view.feed_model
    if m is not None and m.top_key:
        newest = m.top_key[0]           # newest cached created_at
    since = max(newest - 60, int(time.time()) - 7 * 86400)  # 60s clock-skew overlap; 7d floor
    self._freshness_hold = time.monotonic() + 5.0           # pill-hold window, B2
    self.client.request_once(
        f"fresh_following_{int(time.time())}",
        {"kinds": [1], "authors": contacts[:300], "since": since, "limit": 50},
    )
```

Efficiency definition (binding): exactly **one extra REQ per startup** for the following feed, bounded `limit: 50` + `since` window — not a re-sync. `request_once` bypasses `NostrRelay.subscribe`, so it never CLOSEs the live `sub_following` (subscribe's close-previous only fires inside `subscribe()`); the unique sub_id avoids the "bad close" class of bug.

### B2 — EOSE auto-close + pill instead of viewport jump

**Change 1:** `src/gnostr/client.py`, `NostrRelay.request_once` — add `self.snapshot_ids.add(sub_id)` before queueing, so the existing EOSE handler (`on_msg`: CLOSE + discard) auto-closes the freshness sub on every relay. One line; also fixes the same leak for `fetch_user_relays`/`fetch_contacts_for` which already use `request_once` with unique sub_ids and never close.

**Change 2:** `src/gnostr/main.py`, `on_event_received` model branch. Before: `if self.feed_view.feed_model.at_top: flush_new() else: buffered_new(...)`. After: buffer into the pill while a freshness hold is active — `getattr(self, "_freshness_hold", 0) > time.monotonic()` → always `self.feed_view.buffered_new(inserted)` (never `flush_new`), so the startup gap-fill surfaces as the existing "N new posts" pill and the user chooses when the viewport moves. Live events arriving during the hold are buffered too — harmless, the pill tap flushes everything in model order. No new pill widget: `FeedView._pill` / `buffered_new` / `on_pill_clicked` already do exactly this.
Ordering guarantee unchanged: events are DB-persisted before `event-received` (client contract), `FeedModel.prepend_new` dedups against window+buffer and inserts by `(created_at, id)` DESC (`_is_newer` mirrors `db.get_feed_following`'s `ORDER BY e.created_at DESC, e.id DESC`), so the freshness page merges into the cached window with no duplicates and no re-sort.

### B3 — Empty-follows startup (kind-3 arrives after connect)

**Change:** `src/gnostr/main.py`, `on_contacts_updated` (currently `pass`). If `pubkey == self.pub_key` and no freshness pull has run yet (`self._freshness_pulled` flag set in `_pull_feed_freshness`), call `self._pull_feed_freshness()` once. Covers first-ever login where the DB following table is empty at CONNECTED time.

### B4 — Global feed (explicit: no extra pull)

`switch_feed("global")` already issues `subscribe("sub_global", {"kinds":[1],"limit":20}, snapshot=True)` on the first-CONNECTED re-run and renders nothing from cache — the snapshot REQ *is* the freshness pull, and it auto-closes on EOSE. Adding a second global REQ would be a REQ flood for zero gain. No change; restated so the next commit doesn't add one.

**Not-in-scope (Phase B):** no background/periodic freshness timer (the 300 s `on_auto_refresh` stays connection-check-only); no DM freshness REQ (kind-4 sub already opens at login); no `until`-bounded backfill; no changes to `FeedModel` internals (window/cursor/eviction untouched — the freshness page enters through the existing `prepend_new` door); no change to the legacy inline global/me render path.

---

## Evidence

**GTK 4.22 / Adw ≥1.4 APIs used, verified:**
- `Adw.StatusPage` + `set_title/set_description/set_icon_name/set_child` — libadwaita docs (1.4 page; widget since 1.0). Already used in `main.py` login page.
- `Gtk.SearchEntry` + `GtkSearchEntry::search-changed` + `Gtk.Editable` `get_text()` — docs.gtk.org GTK 4 (core since 4.0). NOT `Gtk.Entry.set_enable_ime` (removed 4.16 — banned).
- `Adw.NavigationView.push/pop`, `Adw.NavigationPage(title=, child=)` — since Adw 1.4, already live in `main.py`.
- `Adw.ActionRow(title=, subtitle=)`, `add_prefix/add_suffix`, `Adw.Avatar(size=, text=, show_initials=)`, `Adw.Clamp.set_maximum_size/set_tightening_threshold` — all already live in `dm_inbox_view.py` (threshold form, not the banned `set_tightening_ratio`).
- `Gtk.IconTheme.get_for_display(display).has_icon(name)` — the `_resolve_fab_icon` pattern already in `main.py`; `mail-message-new-symbolic` and `chat-message-new-symbolic` confirmed present in the local Adwaita symbolic set (verified via `ls /usr/share/icons/Adwaita/symbolic/actions/`), `list-add-symbolic` is the guaranteed fallback.
- 48×48 touch targets via `set_size_request(48, 48)` — existing contract in `dm_thread_view.py`.
- NOT used (banned list, `tests/test_dm_gtk422_api.py`): `set_tightening_ratio`, `set_scroll_on_focus`, `set_enable_ime`, `.get_children(`. Extend the BANNED list if implementation hits a new removal.

**Minimal repro of the stale-startup sequence (headless, real `FeedModel` loaded from source, fake DB mirroring `get_feed_following` keyset semantics):**

```
window after load_first (stale cache): ['old2', 'old1', 'old0'] at_top: True top_key: (1759740802, 'old2')
buffered: ['new1', 'new0']                       # freshness page via prepend_new
window after flush: ['new0', 'new1', 'old2', 'old1', 'old0'] at_top: True top_key: (1759999970, 'new0')
dedup re-insert (relay echoes cached event): []  # prepend_new returns [] — no duplicates
dedup re-insert (relay echoes flushed event): []
OK: merge preserves (created_at,id) DESC; prepend_new dedups
```

This is the exact merge B1/B2 rely on: freshness events enter through `prepend_new`, land in `(created_at, id)` DESC position relative to the cached window, and dedup against both the window and the buffer. The `since = top_key[0] - 60` overlap is what makes the echo-back dedup load-bearing.

**On-device verification (tier-2, per tests/AGENTS.md):** Librem 5 / Phosh — picker under OSK (SearchEntry at top, no bottom composer involved), pill tap after startup gap-fill, inbox StatusPage at 360 px. Desktop: same flows at 950 px with sidebar visible.
