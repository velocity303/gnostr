# DM Initiation UX + Startup Feed Freshness

**Status:** approved spec (reconciled from drafts by @miku + @tux)
**Supersedes drafts:** `docs/specs/drafts/dm-initiation-nostr-protocol.md`, `docs/specs/drafts/dm-ux-and-feed-freshness.md`
**Depends on:** `docs/specs/nip44-nip17-dms.md` (shipped); gift-wrap transition (issue #19) is a **dependency** for the protocol hardening units below, NOT for the UX units — the picker/thread flows are wire-agnostic and land on `show_dm_thread(peer)` regardless of kind-4 vs 1059 envelope.
**Runtime floor:** GTK 4.22 / libadwaita ≥1.4 (Librem 5). Banned APIs (see `tests/test_dm_gtk422_api.py`): `set_tightening_ratio`, `set_scroll_on_focus`, `set_enable_ime`, `Widget.get_children`.

## Why

1. Starting a new DM is unintuitive: the only path to a first-ever conversation is finding a peer's profile view. There is no compose affordance and no simplified path to DM someone from the follows list.
2. On startup the feed renders the local SQLite cache (`FeedModel.load_first`, newest-first) before relays connect; when relays come up, live stragglers flush inline and jump the viewport, and if the app was closed for days the user sits on stale posts until new events happen to arrive.

## Reality check (verified in code)

- `client.publish_dm` (client.py:776) publishes **kind-4 with NIP-44 v2 ciphertext**; the NIP-59 gift-wrap envelope (kind 14 rumor → 13 seal → 1059 wrap, double-wrapped self-copy) is issue #19 and NOT yet implemented. Units P1–P5 below are wire-agnostic and ship now; the gift-wrap transition stays gated on #19.
- The kind-10050 targeting machinery is real and reusable: `resolve_dm_relays` (5 s timeout-as-negative, `DM_RELAY_TTL=3600`, waiter coalescing, negatives never cached).
- `request_once` REQs never auto-close on EOSE (leak class); `subscribe()` CLOSEs the prior sub with the same sub_id (unique sub_ids required).
- After `load_first`, `FeedModel.at_top` is True → every live event flushes inline (viewport jump). `prepend_new` dedups against window+buffer and inserts by `(created_at, id)` DESC — merge-safe, verified headlessly.

---

## Phase A — DM initiation UX (UI)

### A1 — "New Message" button on the DM inbox
`src/gnostr/ui/dm_inbox_view.py`: third flat button in the toolbar row (`back_row`), tooltip "New message". Icon resolved via theme probe (candidates `mail-message-new-symbolic` → `chat-message-new-symbolic` → `list-add-symbolic`; first `Gtk.IconTheme.get_for_display(display).has_icon()` hit wins — factor the `_resolve_fab_icon` pattern into `ui/icons.py:resolve_icon(candidates)` and reuse from main.py). Click → `main_window.show_dm_recipient()`.
`src/gnostr/main.py`: `show_dm_recipient()` mirrors `show_dm_inbox` — `Adw.NavigationPage(title="New Message")` pushed on `content_nav`. NavigationPage, not a dialog: every DM surface here is a nav page; modals lose the shared back contract on Phosh and the inbox behind it is empty in exactly the case where New DM matters.

### A2 — Recipient picker: `DmRecipientView` (new file)
`src/gnostr/ui/dm_recipient_view.py` (+ `src/gnostr/ui/meson.build` `install_data` — missing-file-in-meson is a known startup-crash class).
Layout: back row → `Gtk.SearchEntry` (placeholder "Search follows, or paste npub / hex") → ScrolledWindow → `Adw.Clamp(720/504)` → `Gtk.ListBox` (boxed-list, single-click activate). Rows: `Adw.ActionRow` 64 px, `Adw.Avatar(40)` + `ImageLoader.load_avatars`, title=name, subtitle=`npub[:16]…`; dataset = `db.get_following_list(pub_key)`; `client.fetch_profile(pk)` per row fills names live.
Filter (view-local): case-insensitive substring on display name / npub prefix. Identifier paste: `nostr_utils.resolve_profile_identifier(text)` hit (hex/npub/nprofile/nevent-author) pins a synthetic first row "Message <npub…>" → `show_dm_thread(pk)`. `nsec` input is rejected as recipient (DM-to-self is a no-op). NIP-05 input (`a@domain.tld`, charset `a-z0-9-_.`) resolves via P2 when available; until then the paste path is hex/npub only.
Empty follows: `Adw.StatusPage(system-users-symbolic, "No follows yet", "Follow some people, or paste an npub above to start a conversation.")`.
Row tap → `show_dm_thread(pk)`; the picker stays under the thread in the NavigationView stack.
Rejected alternative: a "select mode" flag on `FollowersListView` — it is owner-keyed and reused for foreign profiles; a mode flag branches a clean widget contract for a mode that is meaningless there.

### A3 — DM shortcut on follows rows (the simplified followers path)
`src/gnostr/ui/followers_list_view.py::_make_row`: suffix `Gtk.Button` (48×48 flat, resolved mail/chat glyph, tooltip "Send message") → `show_dm_thread(row.pubkey)`; row tap still opens the profile. `Gtk.Button` claims the press inside itself — no CLAIM gesture on the row (house rule). Skip the suffix for `pk == pub_key`.

### A4 — Inbox empty state
`dm_inbox_view.py::reload()` zero-conversation branch: `Adw.StatusPage(mail-unread-symbolic, "No messages yet", "Start a conversation — tap New Message.")` with a pill `suggested-action` button → `show_dm_recipient`. Renders only when `_gateway_result_seen` AND empty; skeleton shimmer stays authoritative pre-gateway (never fake an empty result).

### A5 — Reachability feedback contract
`dm-unreachable` → MainWindow toast (exists). The picker does NOT gate Send on reachability: the async 10050 lookup inside `publish_dm` is the single decision point (protocol logic stays out of widgets). Optional annotation only: P4's batch probe may badge follower rows `reachable/unknown` asynchronously; a badge is advisory and never blocks a row tap.

**Not-in-scope A:** NIP-50 relay-backed name search; group DMs; DM from PostWidget/quote cards; DMThreadView composer changes; sidebar changes; 10050 settings UI.

## Phase P — Protocol hardening (client/gateway)

### P1 — `probe_dm_reachability(peer_pk, callback)` (client.py)
Public wrapper over `resolve_dm_relays` — same cache, waiter list, 5 s timeout; no second cache. Tri-state: `(pk, relays)` reachable / `(pk, None)` no-10050 / in-flight. Protocol grounding: NIP-17 Publishing — clients MUST publish only to the recipient's 10050 relays; no list ⇒ do not try. Caveats the UI must not overstate: 10050-present ≠ deliverable (auth-required relays, relays may drop gift wraps — NIP-59); negatives are never cached, so a stale "unreachable" badge must never block Send; `dm-unreachable` remains the authoritative mid-send failure signal.

### P2 — NIP-05 discovery: `lookup_nip05(identifier)` (profile_nips.py)
Discovery direction (address → pubkey): GET `https://<domain>/.well-known/nostr.json?name=<local>`, read `names[<local>]` → 64-hex. NIP-05 normative: no redirects (fetch with redirects disabled), hex-only keys, reject bad local-part charset client-side, follow the pubkey not the address. Off-UI-thread, same worker pattern as `verify_nip05`. Returns `(pubkey_hex, relay_hints)` or None.

### P3 — Kind-10050 tag-name tolerance (client.py:672)
Parse `t[0] in ("r", "relay")`. NIP-17's example uses `["relay", …]`; implementations (nostr-tools, NIP-65 convention) publish `["r", …]`. Publish `r` (majority, matches `publish_dm_relay_list`), parse both — a spec-literal peer's list must not read as empty → false "unreachable".

### P4 — Batch DM-capability probe: `fetch_dm_relay_status(pubkeys, callback)`
Chunk ≤50 authors: `{"kinds":[10050],"authors":[chunk],"limit":len}` via `request_once` on all connected relays; settle on EOSE-or-timeout. Present ⇒ DM-capable (feed positives into the same `dm_relay_cache`, TTL 3600, so the first DM to a probed follower skips lookup); absent ⇒ unknown, never cached. Kind 10050 is replaceable — one event per author per relay, hence fan-out + timeout-as-negative. No persisted capability cache (goes stale against peers editing their 10050; the 1 h TTL already covers the cost).

### P5 — Client-side initiation throttle
Gift wraps are signed by one-time keys — relays cannot reputation-filter (NIP-17 Spam / NIP-59 Spam Protection); the client owns abuse control. Count new-conversation initiations (first message to a peer with no `dm_conversations` row): warn at 10/h, hard-stop at 30/h, in-memory counter. Replies in existing conversations exempt. The follower set is a kind-3 artifact with zero protocol standing — "DM your followers" is a UX filter, NOT a trust boundary; identical wire path for followers and strangers. NIP-13 PoW: explicitly deferred (no mainstream relay requires it).

**Dependency (issue #19, not owned here):** kind-4 → kind-14/13/1059 double-wrap transition per NIP-17/NIP-59 (unsigned rumor, seal `tags:[]` signed by real key, wrap with fresh one-time key + `p` tag, self-copy wrap, randomized past timestamps per layer, verify `seal.pubkey == rumor.pubkey` on unwrap). All Phase A units ship before/without it.

**Not-in-scope P:** NIP-42 relay auth (gateway gap, tracked separately); kind 15 file messages; `expiration` tags; group-DM room model; server-side spam filtering.

## Phase B — Startup feed freshness

### B1 — One bounded freshness REQ on first relay CONNECTED
`main.py`, first-CONNECTED block (`not self._feed_live`): after the `switch_feed` idle hop, call `_pull_feed_freshness()`:
`since = max(newest_cached_created_at - 60, now - 7d)` (60 s clock-skew overlap makes relay echo-back dedup load-bearing; 7 d floor bounds the window), `{"kinds":[1],"authors":contacts[:300],"since":since,"limit":50}` via `request_once` with a unique sub_id (`fresh_following_<ts>`). Binding efficiency definition: **exactly one extra REQ per startup** for the following feed — not a re-sync. `request_once` bypasses `subscribe()`, so it never CLOSEs the live `sub_following`.

### B2 — EOSE auto-close + pill instead of viewport jump
`client.py::request_once`: add the sub_id to `snapshot_ids` so the existing EOSE handler CLOSEs it (also fixes the same leak for `fetch_user_relays`/`fetch_contacts_for`).
`main.py::on_event_received`: while a freshness hold is active (`_freshness_hold = monotonic + 5 s` set in B1), route inserts to `feed_view.buffered_new()` instead of `flush_new()` — the startup gap-fill surfaces as the existing "N new posts" pill; the user chooses when the viewport moves. Ordering guarantee unchanged: events are DB-persisted before `event-received`; `prepend_new` dedups and inserts `(created_at, id)` DESC (mirrors `get_feed_following`'s ORDER BY) — merge verified headlessly (fresh events slot correctly; relay echo-back dedups to `[]`).

### B3 — Empty-follows startup
`on_contacts_updated`: if own kind-3 arrived and no freshness pull ran yet, fire `_pull_feed_freshness()` once (covers first-ever login where the following table is empty at CONNECTED time).

### B4 — Global feed: explicitly no extra pull
`switch_feed("global")` already issues a `snapshot=True` REQ on the first-CONNECTED re-run and renders nothing from cache — that snapshot IS the freshness pull, and it auto-closes on EOSE. A second global REQ would be a flood for zero gain.

**Not-in-scope B:** periodic background freshness timers; DM freshness REQ (kind-4 sub opens at login); `until`-bounded backfill; FeedModel window/eviction internals; legacy inline global/me path.

## Verification plan
- Unit: recipient resolver decision table (hex/npub/nprofile/nsec-reject/nip05-candidate); 10050 tag parse (`r` + `relay`); freshness `since` computation; throttle counter thresholds.
- GTK-real (offscreen): picker row build + filter; StatusPage empty states; 48 px touch targets; banned-API guard extended to new files.
- Headless merge: FeedModel freshness-page merge + dedup (already reproduced — keep as regression test).
- On-device (tier-2): Librem 5 / Phosh — New Message flow end-to-end under OSK, followers-row DM button, pill tap after startup gap-fill, stale-cache startup (close app days, reopen → pill, not jump). Desktop at 950 px sidebar.
