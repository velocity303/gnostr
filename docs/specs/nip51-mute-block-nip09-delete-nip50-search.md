# Spec: Mute/Block (NIP-51 kind 10000), Deletion (NIP-09 kind 5), Event Search (NIP-50)

**Author:** @miku · **Status:** final for @codecat · UI contract from @linux-desktop-researcher incorporated.

## A. Mute list — NIP-51 standard list, kind 10000

Protocol facts:
- Kind 10000 is **replaceable** (latest per pubkey wins; same swap semantics as our kind-3 handling in `client.py`).
- Items are tags: `["p", pubkey]` (user mute), `["t", hashtag]`, `["word", lowercase]`, `["e", event-id]` (thread mute). Phase 1 implements `p` mutes; word/hashtag filtering is a cheap add-on if time allows.
- Read: `{"kinds":[10000], "authors":[<pk>]}` on fetch; write: full-list replacement event (append new item at end per NIP-51 chronological convention), publish to write relays (10002 list we already maintain).

**Important protocol correction for the UI plan:** the Nostr protocol has **no "block" event kind**. Mute (kind 10000) is the only wire-level concept. "Block" as users understand it (mutual invisibility, hide my replies from them, no notification paths) is a **client-local** tier. Therefore:
- Mute list → kind 10000 (round-trips across devices — @linux-desktop-researcher's requirement satisfied).
- Block list → **local DB table only** (`blocked_pubkeys(pk TEXT PRIMARY KEY, added_ts)`); blocked pubkeys are ALSO inserted into kind 10000 (so other NIP-51-compliant clients mute them too) but the block flag stays local. Block behavior = mute behavior + suppress my own content's interaction affordances toward them (no reply/quote buttons on their posts, they never appear in "seen by" paths) + stronger UI (destructive styling).

### Filter hook — hard requirement (both @linux-desktop-researcher and I agree)
Filtering runs in the **feed model layer** (`service/feed_service.py` / feed model), NOT post_widget visibility:
- On event ingest AND on paged-load, drop events whose `pubkey ∈ muted ∪ blocked`, and (phase 1.5) `t`-tag/word matches, BEFORE the page is assembled — so muted authors never consume the paged-load quota (no gap-jitter at 360px).
- Blocked authors' replies are also excluded from thread views.
- Mute list cache lives in DB + in-memory set refreshed on kind-10000 event arrival (add `mute-list-updated` GObject signal mirroring `contacts-updated`).

### UI (contract from @linux-desktop-researcher — implement as given)
- post_widget.py gets a `Gtk.MenuButton` + `Gio.Menu` top-right, conditional items: "Mute @x", "Block @x" (destructive style). Same entry point in profile_view header.
- Mute: no confirmation dialog; undo affordance (inline banner with Undo, 5s auto-dismiss — libadwaita has no toast-with-action on our libadwaita-1 target, banner is correct).
- Block: `Adw.MessageDialog`, destructive action, explains mutual-visibility semantics (local + propagated mute).
- Settings "Privacy" page: AdwActionRow list per entry with un-mute/un-block.

## B. Deletion — NIP-09 kind 5

- Kind 5: `e` tags listing event ids to delete. Publish to the write relays.
- **Semantics:** deletion is a request; relays may ignore it. Client MUST also tombstone locally: delete rows from events table and remember deleted ids in `deleted_events(id TEXT PRIMARY KEY, deleted_ts)` so a re-fetch from a lagging relay doesn't resurrect the post.
- Scope: only allow deleting own events (UI shows "Delete" in the new post overflow menu only for own posts). Deleting replies/reposts: also fine, same kind-5 shape.
- If we later add DM message deletion, that's NIP-17's wrapped kind-5 path — out of scope here.

## C. Event search — NIP-50

- Relay capability discovery: we already hold RELAY NOTE (kind 10002 relay docs) / can probe `{"searchable": true}` from NIP-11 — phase 1: just try the query and degrade.
- Filter shape: `{"kinds":[0,1], "search": "<query>", "limit": 60}` + optional `since`/`authors`. Relay returns best-match subset.
- Extend `SearchDialog` (currently identifier-only): segmented/two-tab — "People" (existing local/npub resolution) + "Posts" (NIP-50 network query). Respect kind **10007** Search relays if the user has one (single-relay query, since results can't be merged meaningfully across relays).
- Results render in a normal feed list; clicking a post opens existing thread view.

## Test plan (@titan)
1. Mute on device A → kind 10000 visible on relays → device B (fresh DB) fetches → same filtering (round-trip requirement).
2. Muted author's posts absent from global + follows feed, thread replies, and **paged-load quota unchanged** (load 20, mute 5 authors, still exactly 20 visible).
3. Blocked author: no reply/quote affordance on their posts; kind 10000 contains them.
4. Delete own post → gone locally; lagging relay re-broadcast does not resurrect (deleted_events table).
5. Search "hello" on searchable relay returns kind-1 matches; searchable=false relay shows graceful error.
6. Librem 5: overflow menu reachable with touch targets ≥44px at 360px width; MessageDialog fits screen.
