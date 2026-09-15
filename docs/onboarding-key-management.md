# Design Spec — Onboarding & Secure Key Management

Status: **proposed** (scope agreed 2026-09-15; no implementation started)
Owner: root (`AGENTS.md`); implementation contracts land in `src/gnostr/AGENTS.md` and a new `src/gnostr/signer/AGENTS.md` when each phase begins.

## Problem

1. **New users have no path in.** Nostr has no account creation — an identity *is* a private key. Gnostr currently only accepts an existing `nsec` paste (`LoginDialog` → libsecret), so first-time users must bring a key from elsewhere.
2. **Raw `nsec` paste feels unsafe to users** (correct instinct: plaintext key in clipboard + UI + no safe backup artifact).
3. **No backup guidance.** After login there is no prompt, export, or verification that the user can survive losing their device.

## Protocol landscape (settled vs unsettled)

| Tier | Mechanism | NIP | Status | Adopt? |
|---|---|---|---|---|
| Raw nsec | plaintext key paste | — | legacy | keep as fallback only |
| Encrypted backup | `ncryptsec` = nsec ⊕ password (XChaCha20-Poly1305, scrypt KDF, base64 `ncryptsec1...`) | NIP-49 | **stable, widely adopted** | **P1** |
| Seed phrase derivation | BIP-39 → BIP-32 path `m/1237h/0h/0h/0h/0h` | NIP-06 | marked *unrecommended* for normal users (wallet-tree confusion) | P3, power-user only |
| Remote signing | key lives in external signer; app requests signatures over relays via `bunker://<remote-signer-pubkey>?relay=wss://...&secret=<token>` | NIP-46 | **gold standard**, mature in practice; NIP-44 v2 payload encryption | **P2** |
| Browser extension signing | window.nOSTR | NIP-07 | web-only | skip (native app) |

Unsettled — do not depend on:
- Bunker `create_account` was **removed from NIP-46** and moved to a separate draft NIP; only nsec.app implements it in production. Onboarding stays client-side.
- No standard exists for syncing key material between devices; NIP-49 export/import is the convention.

Non-negotiable copy rule: identities are unrecoverable. New-key flows must state *"If you lose this key, your account is gone forever"* and the backup gate must be un-skippable before entering the app.

## P1 — Create account + backup gate + NIP-49 (settled standards, self-contained)

### Flows

**Create account** (new entry on welcome screen, alongside Login):
1. Generate keypair locally via existing `nostr_utils` primitives (CSPRNG; no network).
2. **Backup gate** (un-skippable, blocks entry): reveal `nsec` once (tap-to-reveal), require type-back confirmation. Offer optional screenshot-suppression hint (GTK can't guarantee it; state this honestly).
3. Offer NIP-49 wrap: set password → produce `ncryptsec` as the portable backup artifact; show copy/save-to-file.
4. Bootstrap: open EditProfile immediately (name/pfp/about); pre-seed default relay set; optionally show curated starter follows. Never land a new user on an empty feed.

**Login additions** (`LoginDialog` becomes 3-way): paste nsec (unchanged) | paste `ncryptsec:` + password (decrypt → keyring) | (P2) connect signer.

**Export** (Settings): logged-in user can produce an `ncryptsec` backup from the keyring-stored key (re-enter password to encrypt). This closes the loop for users who logged in before this feature existed.

### Module contracts

- `nostr_utils.py`: add `nip49_encrypt(nsec, password) -> str`, `nip49_decrypt(blob, password) -> str` (raises on wrong password/auth-tag failure). Pure functions, no GTK — unit-testable without mocks. Dependencies: XChaCha20-Poly1305 + scrypt via the `cryptography` package (new flatpak pip module + system dep note) or a vendored pure-Python fallback; decide at implementation, keep the module surface identical either way.
- `key_manager.py`: unchanged storage (libsecret stays the at-rest store). NIP-49 is a *portable backup format*, never the app's storage format.
- `dialogs.py` / new `ui/onboarding.py`: `CreateAccountFlow` (Adw.ViewStack wizard steps: generate → backup-gate → optional-nip49 → profile-bootstrap). UI mocks in tests per `tests/AGENTS.md` mocking rules; crypto tests are plain pytest.
- **Acceptance**: create-account produces a working identity end-to-end on the Librem 5 phone rig; ncryptsec round-trips against at least one external tool (e.g. tool Nostr tools / Amber interop) in a manual test; backup gate cannot be bypassed; no plaintext key ever written outside libsecret.

## P2 — NIP-46 remote-signing client ("log in with a bunker")

### Shape

New `src/gnostr/signer/` module — the *only* place NIP-46 lives. `client.py` keeps its sole-relay-ownership contract: the signer piggybacks on the existing gateway's relay connections (signer-specific relays added through the same gateway), never opening raw WebSockets itself.

- **Connect**: render `bunker://` connect URI as QR + manual copy; subscribe for NIP-46 `connect` on the signer relay; store remote-signer session (remote-signer-pubkey, per-user-pubkey, secret token).
- **Sign path**: `MainWindow`/`client.py` currently hold `priv_key` and sign directly. Introduce a `SignerBackend` interface (`sign_event(event) -> signed`, `get_public_key()`), implemented by `LocalKeyBackend` (wraps today's behaviour) and `BunkerBackend` (NIP-46 `sign_event` RPC over NIP-44 v2 boxes). Callers never learn which backend is active.
- **Perms/UX**: show connected signer in sidebar; disconnect action; per-sign-request prompt (event kind + content preview) with rate guard. `get_public_key` must be re-called after `connect` per current NIP-46.
- **Crypto surface**: NIP-44 v2 (chacha20 + HMAC conversation keys) — biggest new dependency; prefer vetted library, vendor pinned if needed.

### Acceptance

Cross-device matrix, using the phone as the signer test bed: Amber on the Librem 5 signing for desktop gnostr, and desktop gnostr (as signer-less client) against nsec.app. All publish flows (post/reply/like/repost/kind-3/kind-0) work through `BunkerBackend`. `LocalKeyBackend` keeps 100% of existing tests green.

## P3 — Optional / deferred

- NIP-06 mnemonic derivation as an explicit power-user option (multi-identity trees). Off by default; the NIP's own "unrecommended" note goes in the UI copy.
- Key-health surface: last-backup-verified timestamp, re-verify prompt in Settings.
- NIP-05 identity hookup during onboarding (nice-to-have, separate scope).

## Testing strategy (rig-first)

The Librem 5 Flatpak rig is part of acceptance for every phase: phone build must launch the new onboarding flow and screenshot cleanly (grim loop). NIP-49/NIP-44 vectors: use the test vectors embedded in the NIPs themselves (NIP-49 and NIP-44 both ship them) — these go in `tests/test_nip49.py` / `tests/test_nip46.py` as plain pytest, no GTK mocks needed for crypto.

## Out of scope

- Any hosted account service or key custodianship.
- Multi-device key sync protocols (no standard yet).
- Changing at-rest storage away from libsecret.
