# Draft Spec: New-DM Initiation — protocol/crypto side

**Author:** @miku (Nostr researcher) · **Status:** draft for @codecat + independent confirmation by @linux-desktop-researcher
**Depends on:** `docs/specs/nip44-nip17-dms.md` (implemented); gift-wrap transition (issue #19) — see Dependency below.
**Scope boundary:** protocol/crypto/UX-seam only. No GTK widget decisions — the seam handed to the GTK researcher is named explicitly in each Change.

## Reality check before anything else

The parent task states NIP-59 gift wrap is implemented. The code says otherwise:
`client.py:776 publish_dm()` builds a **kind-4** event carrying NIP-44 v2 ciphertext
("NIP-59 gift-wrap envelope is follow-up scope, per issue #19"). The receive path
(`dm_gateway.py:42`) already names kind-14/1059 in its contract, and the 10050
targeting machinery (`resolve_dm_relays`, `DM_RELAY_TTL=3600`, `DM_RELAY_TIMEOUT_MS=5000`,
waiter coalescing, no-negative-caching) is real and reusable. This draft specifies
initiation against the **NIP-17-correct wire** (kind 14 rumor → kind 13 seal → kind 1059
wrap); the kind-4→kind-14/13/1059 transition is a hard dependency, not part of this spec.

**Dependency (issue #19, not owned here):** per NIP-17 "Encrypting", the unsigned kind-14
rumor MUST be sealed (kind 13, sender's real key, `tags: []`) and gift-wrapped (kind 1059,
fresh one-time keypair, `tags: [["p", recipient]]`) **twice** — once to the recipient, once
to the sender (NIP-59 "Other Considerations": "if the author wants to retain an encrypted
copy, a single rumor may be wrapped and addressed for each recipient individually"; NIP-17:
"sealed and gift-wrapped to each receiver **and the sender individually**"). Seal and wrap
`created_at` randomized up to 2 days in the **past**, independently per layer (NIP-17 SHOULD;
NIP-59: independent timestamps per layer defeat correlation). Canonical timestamp lives on
the rumor only.

## Phase DM-INIT — owns REQ-INIT: "User can start a DM with any resolved pubkey, with reachability known before first send."

### Change 1 — `src/gnostr/profile_nips.py`: add `lookup_nip05(identifier) -> tuple[str, list[str]] | None`

Before: only `verify_nip05(pubkey, nip05)` exists — the *verification* direction (pubkey →
does this address claim it). After: the *discovery* direction (address → pubkey), which
NIP-05 "Finding users from their NIP-05 identifier" specifies as the same flow reversed:
GET `https://<domain>/.well-known/nostr.json?name=<local-part>`, read `names[<local-part>]`
→ 64-char lowercase hex pubkey; if a `relays` object is present, capture
`relays[<pubkey>]` as discovery hints.

Protocol constraints (NIP-05, all normative):
- No-redirect: the endpoint MUST NOT return HTTP redirects; fetchers MUST ignore them
  (use `urllib` with redirects disabled — `HTTPRedirectHandler` returning None).
- `<local-part>` charset is `a-z0-9-_.` only; reject anything else client-side before the fetch.
- The reply keys are hex; npub in a nostr.json response is non-conformant (NIP-05 "Public
  keys must be in hex"; NIP-19: bech32 "not meant to be used in the core protocol").
- Clients MUST follow the pubkey, not the address (NIP-05 "Clients must always follow public
  keys"): resolution result is a one-shot seed for the composer, never a stored identity.
- Run off the UI thread (same worker pattern as `verify_nip05` in ProfileView).

Seam to GTK researcher: resolver returns `(pubkey_hex, relay_hints)` or `None`; "not found"
and "domain unreachable" are distinguishable only by exception class — decide whether the
UI distinguishes them.

### Change 2 — `src/gnostr/nostr_utils.py`: recipient-input decision table (no new parser)

`resolve_profile_identifier(text)` (line 355) already covers: raw hex, `npub`, `nprofile`
(pubkey + relay hints via TLV), `nsec` (own key — reject for DM recipient: DMing yourself is
a no-op room), `nevent`/`naddr` (author pubkey when TLV-2 present), `nostr:` URI prefix.
Add one rule, not a new function: if the text contains `@` and a dot-containing domain and
is not a bech32 prefix, it is a NIP-05 candidate → hand to Change 1. Everything else that
fails `resolve_profile_identifier` is an invalid recipient — no fuzzy name search, no
relay-side full-text guessing (NIP-01 has no text search; `#t`-tag guessing is abuse of the
tag index).

Cited: NIP-19 (bare keys + TLV shareables; unknown TLVs ignored; 5000-char cap), NIP-21
(`nostr:` URI form). Note: `nprofile` relay hints are **profile** hints (kind 0/1
discovery); they are NOT DM relays and MUST NOT be used for DM publish targeting — NIP-17
publish targeting is 10050-only (Change 3).

### Change 3 — `src/gnostr/client.py`: accept both `r` and `relay` tag names on kind 10050

Before: line 672 parses only `[t for t in tags if t[0] == "r"]`. After: `t[0] in ("r", "relay")`.
Why: NIP-17 "Publishing" writes the example with `["relay", "wss://…"]` tags, but the
dominant implementation convention (nostr-tools, most clients, mirroring NIP-65 kind-10002
which uses `r`) publishes `["r", url]`. This is exactly a "spec text vs implementations vary"
seam: **publish `r`** (interop majority, matches our existing `publish_dm_relay_list`),
**parse both** (a peer's 10050 written by a spec-literal client must not read as empty →
false "unreachable"). Same treatment when we later read peers' lists in bulk (Change 5).

### Change 4 — `src/gnostr/client.py`: expose `probe_dm_reachability(peer_pk, callback)` as the composer's async pre-check

Before: `resolve_dm_relays(peer, cb)` exists but is private and only fired from the send
path. After: public wrapper with identical semantics — reuse the cache, the waiter list,
and the 5 s timeout verbatim; **no second cache**. Callback tri-state: `(peer_pk, relays)`
reachable / `(peer_pk, None)` no-10050 / (no call yet) in flight.

Protocol grounding: NIP-17 Publishing — "Clients MUST only publish events to the relays
listed in the recipient's kind 10050 event. If such a list is not found that indicates the
user is not ready to receive messages and clients shouldn't try." A pre-send probe is
therefore protocol-sanctioned, not speculative.

Two honest caveats the UI must not overstate (hand to GTK researcher):
1. **10050-present ≠ deliverable.** The peer's DM relay may `auth-required` for writes
   (NIP-42, not implemented in gateway — known limitation from the parent spec) or may
   drop gift wraps (NIP-59: "Relays may choose not to store gift wrapped events"). The
   probe is advisory; the send path's OK-ack (`publish-result`) remains the only proof of
   acceptance. Never render "reachable" as a guarantee.
2. **Probe-then-send is racy by design.** Negative results are deliberately NOT cached
   (existing `_resolve_dm_relays` behavior — a peer who later sets a 10050 becomes
   reachable immediately), so a stale "unreachable" badge must never block the Send button;
   Send re-probes through the same path. The existing `dm-unreachable` signal stays the
   authoritative failure notification mid-send.

### Change 5 — `src/gnostr/client.py`: batch DM-capability probe for follower lists — `fetch_dm_relay_status(pubkeys, callback)`

Protocol data that backs "people I can DM": **kind 3 `p`-tags (who they are) ∩ kind 10050
presence (whether they can receive)**. There is no other DM-capability signal in the
protocol — no handshake, no capability flag, no consent mechanism. NIP-17 is explicit that
absence of 10050 IS the capability answer ("not ready to receive messages"). Kind 10002
(NIP-65 outbox) is read/write inbox metadata, NOT DM capability; do not conflate.

Mechanics: chunk the pubkey list (≤50 per REQ), fire
`{"kinds":[10050],"authors":[chunk…],"limit":<len(chunk)>}` via `request_once` on the
connected relays; settle on EOSE-or-timeout like the single probe. Kind 10050 is
**replaceable** (NIP-01: 10000≤k<20000), so relays return at most one event per
(pubkey, kind) — a pubkey absent from the response has no 10050 *on that relay*, which is
why the probe must fan out to all connected relays and treat timeout as the negative
verdict, same as the single-peer path. Positive results feed the SAME `dm_relay_cache`
(TTL `DM_RELAY_TTL`=3600 s) so the first DM to a probed follower skips lookup entirely;
negatives stay uncached (same rule as Change 4).

Cache/TTL answer for the parent question: followers list itself = existing owner-keyed
`following` table refreshed by `fetch_contacts_for` (kind 3, replaceable, limit 1); DM
capability rides `dm_relay_cache` at 1 h TTL. No new table needed — a persisted
dm-capability cache would go stale against peers who add/remove their 10050 and buys
nothing the 1 h TTL doesn't already give.

Seam to GTK researcher: signal/callback shape `(pubkey → reachable|unknown)` for annotating
`FollowersListView` rows; whether the badge is inline, deferred, or on-demand is their call.
Rows must render before the probe resolves (protocol has no fast answer; 5 s worst case).

### Change 6 — `src/gnostr/client.py`: client-side initiation throttle (abuse control; the protocol offers nothing else)

NIP-17 "Spam" + NIP-59 "Spam Protection": gift wraps are signed by random one-time keys, so
relays cannot rate-limit or reputation-filter by pubkey. The relay-side answer is NIP-42
auth-gated gift-wrap acceptance (not in our gateway — tracked separately). The client-side
answer is ours to own: count **new-conversation initiations** (first message to a peer with
no `dm_conversations` row) and soft-throttle past a threshold (suggest: warn at 10/hour,
hard-stop at 30/hour, in-memory counter, not a DB feature). Replies inside existing
conversations are exempt. Followers-vs-strangers: the follower set is a client-side
kind-3 artifact with zero protocol standing — anyone can publish a kind 3 naming anyone;
"DM your followers" is a UX convenience filter, NOT a trust boundary, and must not be
presented as a protocol-sanctioned whitelist. Do not build different crypto/targeting for
followers vs strangers — identical wire path; only the throttle UI may differ.

NIP-13 PoW on wraps: clients MAY attach PoW to demonstrate non-spam (NIP-59). Recommend
NOT implementing now: no mainstream DM relay requires it, and it costs client CPU for zero
interop gain. Revisit if a target relay demands `pow:` OK prefixes.

## Protocol requirements (cited, REQUIRE vs varies)

| # | Requirement | Status | Source |
|---|---|---|---|
| R1 | Publish DM wraps ONLY to relays in recipient's kind 10050; no list ⇒ do not send | MUST | NIP-17 Publishing |
| R2 | Rumor (kind 14) unsigned; sealed kind 13 with `tags: []`, signed by sender's real key; wrapped kind 1059 with fresh one-time key, `p` tag = recipient | MUST | NIP-59 §2–3, NIP-17 Encrypting |
| R3 | Double wrap: one wrap per receiver AND one to the sender (self-copy) | MUST (NIP-17 wording "individually") | NIP-17 Encrypting, NIP-59 Other Considerations |
| R4 | Verify `seal.pubkey == rumor.pubkey` on unwrap | MUST (impersonation vector) | NIP-17 |
| R5 | Seal + wrap `created_at` randomized ≤2 days past, independent per layer; canonical ts on rumor only | SHOULD | NIP-17, NIP-59 |
| R6 | Kind 14 `.content` plain text; `p` tags define the room; room identity = pubkey + p-tag set; optional `subject` tag = conversation title (newest wins) | MUST / convention | NIP-17 Chat Rooms, Message Rumor Definitions |
| R7 | 10050 tag name: spec text says `relay`, implementations publish `r` | VARIES → publish `r`, parse both | NIP-17 example vs nostr-tools/NIP-65 convention |
| R8 | Keep own 10050 small (1–3 relays), spread across relays | SHOULD | NIP-17 Relays |
| R9 | Recipient resolution: hex/npub/nprofile (+nevent/naddr author) local; NIP-05 discovery via well-known, no redirects, hex-only, follow pubkey not address | NIP-05 normative | NIP-05, NIP-19 |
| R10 | No protocol-level DM capability beyond 10050; no consent/handshake mechanism exists ("No Moderation") | fact | NIP-17 Benefits & Limitations |
| R11 | Ephemeral wrap kind 21059 exists for real-time-only DMs | not needed | NIP-59 §4 |
| R12 | NIP-42: relays SHOULD serve 1059 only to p-tagged recipients; clients SHOULD support auth to such relays | SHOULD (gateway gap, known) | NIP-17 Relays, NIP-59 Spam Protection |

## Not-in-scope

- GTK widget layout, composer surface, recipient-picker design, badge rendering — @linux-desktop-researcher owns; this spec only names the callback/signal shapes at each seam.
- The kind-4 → kind-14/13/1059 wire transition itself (issue #19) — dependency, not this spec.
- NIP-42 relay auth in the gateway (separate issue, inherited limitation from parent spec).
- Kind 15 file messages, kind 5 delete-wraps, disappearing/`expiration` tags (NIP-17 allows; phase-2).
- Group DMs (>2 peers): NIP-17 supports multi-`p` rooms but the room-identity model in the parent spec is a sorted two-pubkey pair; changing it is a separate spec.
- Server-side spam filtering, NIP-13 PoW (explicitly deferred, Change 6).
- Contact-request/consent UX: no protocol mechanism exists; any "message requests" inbox is pure client-side policy.

## Evidence

Single-peer reachability probe (existing machinery, unchanged wire):

```json
["REQ","dmrelays_ab12cd34",{"kinds":[10050],"authors":["<peer_hex>"],"limit":1}]
```
Any EVENT ⇒ reachable (relays = `r`/`relay` tag values). EOSE-without-EVENT on all
relays + 5 s timeout ⇒ unreachable (R1: do not publish).

Batch follower probe (Change 5, ≤50 authors per REQ):

```json
["REQ","dmbatch_0",{"kinds":[10050],"authors":["<pk1>","<pk2>","…"],"limit":50}]
```
Present-in-response ⇒ DM-capable; absent after timeout ⇒ unknown/unreachable (never cached).

NIP-05 discovery (Change 1):

```
GET https://example.com/.well-known/nostr.json?name=bob   (follow redirects: OFF)
→ {"names":{"bob":"<64-hex>"},"relays":{"<64-hex>":["wss://…"]}}
```
`names.bob` ≠ claimed pubkey on their kind 0 ⇒ still usable as identity hint per NIP-05
discovery flow, but store the hex, never the address (R9).

Recipient room identity after first message (unchanged): conversation key =
`(rumor.pubkey, my_pubkey)`; room = sorted pubkey pair (parent spec).
