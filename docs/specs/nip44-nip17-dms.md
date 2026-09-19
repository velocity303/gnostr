# Spec: Direct Messages — NIP-44 encryption + NIP-17 gift-wrapped DMs

**Author:** @miku (Nostr Researcher) · **Status:** final for @codecat · **Depends on:** nothing (pure-Python crypto layer first)
**UI design input from @linux-desktop-researcher is incorporated verbatim where noted.**

## Why

Gnostr has zero DM support (no kind 4/13/14/44/1059/10050 anywhere in src/). DMs are the single biggest user-expectation gap for an X/Twitter-style client. We skip legacy NIP-04 (kind 4) entirely — it leaks metadata, is deprecated, and dying on relays. We implement the modern stack: **NIP-44 v2 encryption + NIP-59 gift wrap + NIP-17 messaging**.

## Phase A — `src/gnostr/nip44.py` (pure Python, no new deps)

Implement NIP-44 v2 exactly per https://github.com/nostr-protocol/nips/blob/master/44.md:

- `conversation_key(priv_hex, counterparty_pub_hex)`: secp256k1 ECDH via the existing `ecdsa` dep — `SigningKey.privkey.mult_public()` or point multiplication; **use the UNHASHED 32-byte x-coordinate** (many libs hash it — do not). Then HKDF-extract (sha256, salt=`b"nip44-v2"`). Symmetric: `conv(a,B) == conv(b,A)`.
- `encrypt(plaintext, conversation_key)` / `decrypt(payload, conversation_key)`:
  - random 32-byte nonce per message (`secrets.token_bytes`)
  - HKDF-expand(prk=conv_key, info=nonce, L=76) → chacha_key(32) ‖ chacha_nonce(12) ‖ hmac_key(32)
  - padding: power-of-two buckets, min 32B; `[u16 len][plaintext][zeroes]`; ≥65536 uses 6-byte extended prefix `[0x00,0x00][u32 len]`
  - ChaCha20 (counter=0) — **reuse `_chacha20_block` already in `nip49.py`** (extract to shared `src/gnostr/_chacha.py` or import)
  - MAC = HMAC-SHA256(hmac_key, nonce ‖ ciphertext), **constant-time compare** on decrypt
  - payload = base64(version_byte 0x02 ‖ nonce ‖ ciphertext ‖ mac)
- Decrypt MUST reject: unknown version byte, `#`-prefixed payloads (return "unsupported version"), <132 chars base64 / <99 bytes decoded, MAC mismatch, padding mismatch. Enforce a max payload size (recommend 512 KB) before base64-decode (DoS guard, see NIP-44 "Implementation considerations").
- **Acceptance: pass the official test vectors** from https://raw.githubusercontent.com/paulmillr/nip44/main/nip44.vectors.json (sha256 of file: `269ed0f69e4c192512cc779e78c555090cebc7c785b609e338a62afc3ce25040`) — conversation keys, encrypt/decrypt round-trips, all `invalid.decrypt` cases must raise. Commit the vectors file under `tests/data/`. This is a hard gate for @titan.

## Phase B — NIP-59 gift wrap primitives (`src/gnostr/nip59.py`)

- `seal(rumor_unsigned, sender_priv_hex, recipient_pub_hex)` → kind 13: content = NIP-44(JSON rumor) with conv key (sender, recipient); signed by sender's REAL key; `tags: []` (must be empty); `created_at` randomized up to 2 days in the past.
- `gift_wrap(inner_event_json, recipient_pub_hex)` → kind 1059: fresh random one-time keypair per wrap; content = NIP-44(JSON inner) with conv key (ephemeral, recipient); `tags: [["p", recipient]]`; `created_at` randomized (past, independent of seal's).

## Phase C — NIP-17 messaging (`src/gnostr/dm.py` + client wiring)

### Send
1. Build **unsigned rumor** kind 14: `content` plain text, `p` tags for each peer, optional `e` tag (reply to another kind-14 id), optional `subject` tag. `created_at` = now (canonical timestamp lives here).
2. Seal → gift-wrap **twice**: one wrap to the recipient, one wrap to the sender (self-copy, so history survives device loss).
3. **Relay targeting:** fetch recipient's kind **10050** (DM relays). Publish wraps ONLY to those relays. If recipient has no 10050 → toast "Recipient is not reachable for DMs" and abort. Cache 10050 lookups (TTL, same pattern as 10011 in `profile_nips.py`).
4. Maintain own kind 10050 (default: 1–2 of the user's write relays; settings UI later).

### Receive
- Subscribe `{"kinds":[1059], "#p":[<my_pubkey_hex>]}` on own DM relays (add to gateway subscription set).
- Unwrap: NIP-44 decrypt with own key → seal JSON → **validate seal.sig AND seal.pubkey == rumor.pubkey** (mandatory — otherwise anyone can impersonate any sender by editing the rumor pubkey, per NIP-17) → NIP-44 decrypt seal content → rumor. Validate rumor `created_at` sanity (not > now+2d, not < epoch 2020).
- Conversation key = `(rumor.pubkey, my_pubkey)`. Two-party room identity = sorted pubkey pair.

### Storage (DB, per @linux-desktop-researcher)
- `dm_conversations(peer_pk TEXT PRIMARY KEY, last_event_id TEXT, last_ts INTEGER, unread_count INTEGER DEFAULT 0)` — unread_count maintained on insert of an inbound (non-self) rumor; zeroed on opening thread.
- `dm_messages(id TEXT PRIMARY KEY, peer_pk TEXT, sender_pk TEXT, content TEXT, rumor_ts INTEGER, inserted_at INTEGER, direction TEXT)` — store **unwrapped plaintext locally only** (device is trusted; wraps are transient). kind-15 file messages: out of scope phase 1.
- Badge count = `SELECT SUM(unread_count) FROM dm_conversations` — a query, not a synced field.

### UI (contract from @linux-desktop-researcher — implement as given)
- Sidebar row "Messages" (`mail-unread-symbolic`) between Follows and relay expander, count label bound to unread query.
- `DMInboxView` pushed into `content_nav`: AdwClamp ListBox — avatar, peer name + short npub, last-message preview, timestamp.
- `DMThreadView`: chat-style, bottom-anchored, compose entry in `AdwToolbarView` bottom bar (OSK-safe on Phosh/PM — do NOT reuse the modal compose dialog).
- Portal notification: title/body only, preview text behind a setting, default OFF.

### Known limitations to document (do not block on these)
- **NIP-42 auth not implemented in gateway** → some DM relays that `auth-required` for kind-10050 write will reject publishes. Phase 1 targets non-auth DM relays; NIP-42 tracked as separate issue.
- No forward secrecy / deniability limits inherent to NIP-44 (documented in NIP itself).
- Spam: gift wraps use random keys; without NIP-42 we cannot relay-side filter. Client-side mitigation: skip unwrapping wraps from ephemeral pubkeys we've never conversed with unless they decrypt successfully anyway (cheap: attempt decrypt, drop on MAC fail — MAC failure is the natural filter).

## Test plan (@titan)
1. NIP-44 vectors suite green (mandatory).
2. Two-account interop: send A→B, B sees plaintext thread, unread badge increments/clears; created_at of wraps ≠ rumor ts (metadata randomization).
3. Spoof test: hand-craft wrap whose seal.pubkey ≠ rumor.pubkey → client MUST reject (log, no insert).
4. No-10050 peer → graceful toast, no publish attempted.
5. Librem 5: send + receive under Phosh, OSK does not bury the send bar.
