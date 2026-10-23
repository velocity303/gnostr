# gnostr-zaps-payments-v1 — Nostr payments protocol contract (zaps + wallet)

**Card:** t_1a1f4545 · **Author:** miku (Nostr protocol researcher) · **Date:** 2026-10-21
**Companion:** `gnostr-btc-backend-v1` (t_efbb2ccb, satoshi) — node/wallet tooling is his; protocol objects are this doc.
**Status:** scoping spec for James' review — no implementation. All rulings from James' comments of 2026-10-21 (batch 1, batch 2, security rule) are folded in.

---

## 0. Scope ruling (v1 / v2 / v3)

| Version | Payment rail | Wallet backend | Zaps |
|---|---|---|---|
| **v1** | Lightning only (confirmed) | **NWC only** (NIP-47) | Identified zaps send + receive/display |
| v2 | Lightning | + direct LND (REST+macaroon, Tor) + direct CLN (clnrest+runes) | + anonymous zaps (§1.5), + NWC extensions (NWC-321, notifications) |
| v3+ | on-chain **if ever** | — | — |

On-chain is out of scope entirely (James, batch 2). The `WalletBackend` interface (§2.4) is the expansion seam: v2 backends implement the same DTOs, zero call-site rework.

**v1 milestone recommendation:** one release, three child cards, order C → A → B:
- **C — NWC backend** (`nwc.py` ~200 lines reusing G3 relay + NIP-44 stack, zero new pip deps per satoshi §2; connection UI + budget config): ~3–4 days
- **A — zap send** (LNURL-pay client, 9734 builder, confirmation dialog, pay via WalletBackend): ~3–5 days
- **B — zap receive/display** (9735 subscription + verification + feed aggregates): ~2–3 days

---

## 1. Zaps — NIP-57 protocol contract

### 1.1 Zap target resolution (kind-0 metadata)
Precedence (client-side rule; NIP-57 itself is silent on ordering — this is the de-facto consensus order used by Amethyst/Flystr/Nos):
1. `lud16` (lightning address, LUD-16): GET `https://<domain>/.well-known/lnurlp/<user>` (clearnet) or `http://<onion>/.well-known/lnurlp/<user>` (onion — LUD-16 explicitly allows onion).
2. `lud06` (bech32 `lnurl1...`, LUD-06): decode bech32 → GET the URL.
3. **NIP-05 fallback (convention, not spec):** if neither present and `nip05` is `user@domain`, try `user@domain` as a lightning address. Recommend v1: include (cheap, big coverage win). Flagged as convention in code comments.
4. None → not zapable; hide the zap button.

### 1.2 LNURL-pay metadata gate (LUD-06/LUD-16)
From the `payRequest` response, v1 **requires** `allowsNostr: true` AND a `nostrPubkey` (32-byte hex). Without both, no zap receipt can be guaranteed → treat endpoint as not zapable (show "recipient doesn't support zaps"). `minSendable`/`maxSendable` (msats) bound the amount entry field. `commentAllowed` (int, chars) caps the comment box. `payerData` — v1: send nothing (all fields optional per LUD-16 payerData spec); if the server marks a field `mandatory: true`, surface an error "recipient requires profile sharing" and abort (do not silently send name/pubkey).

### 1.3 Zap request (kind 9734) — normative
Signed with the **sender's main key** (this is the one payment-adjacent operation that does use it — it signs nothing spendable, see §3.1), **never published to relays**, sent as HTTP GET to the callback:
`<callback>?amount=<msat>&nostr=<urlencoded canonical JSON of the event>[&comment=...]`

Required tags (NIP-57):
- `["relays", <wss...>]` — ≥1 relay where the sender wants the 9735 receipt. v1 rule: the user's configured write relays (same set DMs use).
- `["amount", "<msats>"]` — decimal string, msat.
- `["lnurl", "<bech32 lnurl>"]` — the recipient's LNURL (bech32 form, even when discovered via lud16 — derive from the metadata URL).
- `["p", "<recipient pubkey hex>"]`; for note-zaps also `["e","<event id>"]` and `["k","<target kind as string>"]`.
- `content`: comment, default `"Zap!"`.

Server-side checks the callback performs (state them so code-cat can build the fake LNURL server for tests): valid BIP-340 sig over the canonical serialization, `amount` within min/max sendable, `lnurl` tag matches the endpoint, `p` matches recipient, `created_at` recent (±60s tolerance is common; do not rely on it).

Callback response: `{"pr": "<bolt11>", "successAction": {...}, "routes": []}`. Client then pays `pr` via `WalletBackend.pay_invoice`.

### 1.4 Zap receipt (kind 9735) — verification rules (code-cat MUST implement all)
Published by the LNURL server after settle, to the relays in the request's `relays` tag (+ recipient inbox hints). Client-side verification before rendering a zap as real:
1. Receipt `pubkey` == the LNURL server's `nostrPubkey` from §1.2 metadata (cache metadata per-lnurl; if unknown, fetch it).
2. `description` tag parses as JSON == the zap request: recompute its `id` (sha256 of canonical serialization) and verify its BIP-340 `sig`.
3. `bolt11` tag present; bolt11 decoded amount (msat) ≥ request `amount` tag (overpayment tolerated by convention; underpayment = reject).
4. `p` tag == zap request `p` tag (recipient); `P` tag == zap request `pubkey` (sender).
5. If `description-hash` present: sha256(description bytes) must match.
6. Dedupe: one receipt per zap request (key: bolt11 tag, fallback description id). Multiple relays echo the same receipt — same event id, dedupe trivially; different receipts for one request (rare, split payments) → render once, sum for aggregate.
7. Event-level: valid id + sig per NIP-01 (existing pipeline).

**Feed aggregates (protocol boundary only):** emit per-event zap total = Σ bolt11-decoded msat of *verified* receipts referencing the event; per-profile totals likewise. Rendering is tux's overlay; the contract is the verified-receipt set + totals.

### 1.5 Anonymous zaps — explicit complexity call → **v2** (James' default: defer)
Correction for the record: the operator's note cites "LUD-17" for anon zaps; **LUD-17 actually defines `lnurlp://` raw URL schemes** (draft, adoption stalled). Anonymous zaps are **not standardized by any LUD or NIP**; deployed variants are:
- **keysend zaps:** pay the recipient node directly (recipient must publish node pubkey somewhere), zap request smuggled in keysend TLV 548212. NWC exposes `pay_keysend`, but **no LNURL server issues the 9735** — the sender must self-publish the receipt, which breaks §1.4 rules 1 and 3 (no third-party attestation, no bolt11↔request binding).
- **LNURL without the `nostr` param:** invoice comes back but the server never sees the request → no receipt at all → zaps don't show in feeds.

**Call: NOT a flag on the same callback path.** It needs a separate invoice-acquisition path, separate receipt provenance (self-issued), and a second verification mode (attested vs self-claimed) that downstream consumers must understand. → **v2.** v1 identified zaps already leak nothing extra beyond what §3.4 lists.

### 1.6 Zap splits (LUD-18): out of scope — zero client work
Splits are executed server-side by the LNURL provider; the client pays one invoice either way. A `lud16` with splits configured works in v1 transparently. No client feature.

---

## 2. Wallet integration — NIP-47 NWC (v1 backend)

### 2.1 Connection URI
`nostr+walletconnect://<wallet-pubkey-hex>?relay=<wss-url>&secret=<hex32>[&lud16=...]`
The URI **is a spending credential** (§3.2). `relay` is the NWC relay — NIP-47 mandates requests/responses flow on the URI's relay, which settles satoshi's open question "dedicated relay vs social relays": **whatever relay the URI names**; docs recommend minting the connection on a private/dedicated relay. `lud16` param (if present) auto-fills the user's own zap address.

### 2.2 Protocol mechanics (all cross-validated, §5)
- **Capability gate:** GET kind **13194** info event from wallet pubkey on the NWC relay. `content` = space-separated method list; `encryption` tag values `nip44_v2`/`nip04`. No info event → assume legacy nip04. Gate every call on the advertised method list; surface missing methods as "wallet doesn't support X".
- **Request:** kind **23194**, from the connection `secret` key, `p` tag = wallet pubkey, content = NIP-44 v2 (preferred) or NIP-04 (legacy fallback only when info event lacks `nip44_v2`) encrypted JSON `{"method":..., "params":{...}}`.
- **Response:** kind **23195**, from wallet pubkey, `p` tag = client pubkey, `e` tag = request event id, same encryption. Match responses to in-flight requests by `e` tag. Timeout 30s → `WALLET_TIMEOUT` error to UI.
- **v1 required methods:** `pay_invoice`, `make_invoice`, `get_balance`, `lookup_invoice`, `get_info`, `list_transactions` (pin per satoshi §2). `pay_keysend`/`multi_pay_invoice`/NWC-321 → v2.
- **Error codes are UX, not retry fuel:** `QUOTA_EXCEEDED` → "monthly zap budget reached" (point at §2.5); `INSUFFICIENT_BALANCE` → show balance; `PAYMENT_FAILED` → show message, never auto-retry a spend.

### 2.3 Receiving zaps into the user's own node
Nostr-side requirements only (node-side = satoshi's card): the user's `lud16`/`lud06` in kind-0 must resolve to an LNURL-pay endpoint whose `nostrPubkey` signs 9735s and whose callback returns invoices settling on the user's node. For James' LND: run the `nostr-wallet-connect-lnd` bridge (it serves LNURL-pay + NWC from the same node). The app never hosts an LNURL endpoint.

### 2.4 WalletBackend interface (the v2 expansion seam)
DTOs mirror NIP-47 JSON exactly so v2 direct-LND/CLN adapters are drop-in:
```
WalletBackend (abstract):
  get_info()        -> {alias, color, methods: [str], network}
  get_balance()     -> {balance_msat: int}
  make_invoice(amount_msat, description, expiry_s?) -> {invoice, payment_hash, created_at}
  pay_invoice(bolt11, amount_msat?) -> {preimage, fees_paid_msat}
  lookup_invoice(payment_hash? | bolt11?) -> {settled: bool, ...}
  list_transactions(from_ts, until_ts, limit, offset, type?) -> {transactions: [...]}
```
v1 implements `NWCBackend` only. v2 adds `LndRestBackend` / `ClnRestBackend` behind the same signatures. Call sites (zap flow, panel) never branch on backend type.

### 2.5 Zap budget (James batch 2)
- **Default: 100,000 sats/month.** Two enforcement layers:
  1. **Hard cap at the wallet service** — set when the connection is minted (Alby Hub UI / bridge config). The app cannot exceed it; `QUOTA_EXCEEDED` is the signal.
  2. **Client soft cap** — GSettings key `monthly-zap-budget-sats` (int, default `100000`), surfaced in the wallet-connection preferences page next to the URI entry. The app tracks Σ spent per calendar month (from settled `pay_invoice` results persisted locally) and blocks the confirmation dialog when the next zap would exceed it. Soft cap ≤ hard cap is the user's responsibility; the app warns if soft > hard is detected from a `QUOTA_EXCEEDED`.
- Config surface: wallet connection UI = URI field (masked, libsecret-backed) + budget spin button + "test connection" (calls `get_info`).

---

## 3. Security requirements

### 3.1 Key separation
- Zap request (9734) signing uses the **main key** — unavoidable (identity of the zapper) and harmless: it signs an attestation, not a payment. Payment authority is the **NWC connection secret**, a separate 32-byte key that cannot spend anything without the wallet service honoring it. The main nsec is never used for NWC.
- Blast radius (satoshi §3.3): leaked NWC URI ≤ budget until revoked in the Hub/bridge UI. Leaked main key ≤ identity, not funds.

### 3.2 Storage
NWC URI + secret → **libsecret via existing KeyManager** (same treatment as NIP-44 DM key). Never GSettings plaintext, never config JSON, never the Flatpak dir. GSettings holds only non-secret prefs (budget number, connection label).

### 3.3 Double-pay / replay guards
- In-flight lock: one `pay_invoice` per (bolt11) at a time; a second confirm on the same invoice is a no-op.
- Before paying, `lookup_invoice(bolt11)`; if already settled → skip pay, treat as success.
- Persist paid-bolt11 set locally; zap button on an already-paid invoice shows "paid".
- Budget ledger append-only, keyed by (month, bolt11).

### 3.4 Amount confirmation UX contract (protocol boundary)
Every interactive zap: explicit dialog showing **sats** (bolt11 msat ÷ 1000, rounded, with exact msat in tooltip), recipient display name, recipient LNURL **domain**, fee-free guarantee note (fees ≤ amount per NWC `fees_paid`). No auto-pay anywhere in v1; no "zap last amount remembered" shortcut. The dialog emits one `pay_invoice` or nothing.

### 3.5 Privacy leak table (per zap, identified flow)
| Observer | Learns |
|---|---|
| LNURL server (recipient's) | sender pubkey, sender IP, amount, comment, target event, relays list |
| Public relays | 9735 receipt: sender, recipient, amount (bolt11), comment, target — public by design |
| NWC relay | encrypted 23194/23195 (NIP-44): sees timing + relay membership only, not amounts |
| Network observer (Tor off) | LNURL server domain + payment routing |
v1 mitigations: onion LNURL endpoints supported end-to-end (§1.1); NWC relay sees nothing plaintext. Anonymity vs the LNURL server is exactly what §1.5 (v2) addresses.

### 3.6 Node-identity redaction (James' security rule, 2026-10-21)
James' LND identity (node pubkey, `.onion` host, endpoints, macaroons, NWC URIs) **never appears in repo, spec docs, fixtures, or commits**. This spec and `test_vectors.json` use synthetic keys (`1111…`–`5555…`) and `example.com` hosts only. Tests read `TEST_LND_REST_URL` / `TEST_NWC_URI` from env at run time; real credentials live in libsecret/env. Docs say "your node". The scan-secrets pre-commit hook is the backstop, not the plan.

---

## 4. Test vectors — `test_vectors.json` (attached, regenerated by `gen_vectors.py`)

All deterministic, synthetic-key, verified by execution 2026-10-21:
- **Cross-validation harnesses (green):** BIP-340 signer cross-checked against the official BIP-340 CSV (all vectors); NIP-44 v2 implementation cross-checked against official `paulmillr/nip44` vectors — **85/85 pass** (conversation keys incl. ECDH edge cases, padding table, encrypt/decrypt round-trips, long-message sha256).
- `keys` — 5 synthetic keypairs (sender, recipient, LNURLP nostr key, NWC wallet, NWC client).
- `v1_lnurlp_metadata` — fixed payRequest JSON (`allowsNostr`, `nostrPubkey`, `commentAllowed: 280`, min/max sendable) + its `lnurl_bech32` encoding.
- `v2_zap_request` — kind 9734 (1000 sats = `amount` tag `1000000` msat) with byte-exact canonical JSON, `id`, BIP-340 `sig`; `v2_callback_url` — the exact GET URL (urlencoded `nostr` param).
- `v3_zap_receipt` — kind 9735 signed by the LNURLP key; `description` == the 9734 canonical JSON; `bolt11` = NIP-57 appendix sample invoice (hrp `10u` = 1000 sats, matches amount tag); `preimage` present. Exercises every §1.4 rule.
- `v4_nwc_*` — connection URI, kind 13194 info event (encryption tag), kind 23194 request + 23195 response pair **NIP-44 v2 encrypted** (payloads byte-exact vs official vectors), plaintexts (`pay_invoice` request / `{preimage, fees_paid}` response), plus the **NIP-04 legacy payload pair** for bridge-compat testing.
- `bolt11_hrp_*` — sats→hrp multiplier checks (21000 sat → `210u`, 1000 sat → `10u`).
- From satoshi's survey (use together): BOLT-11 decode vector `lnbc2500u1pvjluez…` → mainnet / 250000 sat / ts 1496314658 / hash `00010203…0102` / desc "1 cup coffee".

code-cat asserts both sides (builder + verifier) against these; the fake NWC wallet service and fake LNURL server for CI are built from the same fixtures (satoshi §5 test strategy).

---

## 5. Open questions — ALL RESOLVED (James, 2026-10-21)

Scoping is closed. James' rulings: node type (LND), Lightning-only, budget default (100k sats/mo), anon zaps (v2, §1.5), relay question (§2.1: URI's relay, per NIP-47). Final product calls:
1. **Wallet UI surface:** LOCKED — dedicated Wallet preferences page + per-post zap button.
2. **First live endpoint:** LOCKED — `nostr-wallet-connect-lnd` bridge on James' existing LND (Alby Hub docs secondary). James supplies endpoint details at implementation start.
3. **NIP-05 fallback (§1.1 step 3):** LOCKED — include the convention, flagged in code comments.

**SEQUENCING (James, 2026-10-21):** Tor/.onion transport (`gnostr-tor-onion-support-v1`) is implemented BEFORE zap cards C/A/B.

---

## §1 Handoff JSON

```json
{
  "spec_id": "gnostr-zaps-payments-v1",
  "domain": "nostr",
  "claim": "v1 zaps = NIP-57 identified zaps (9734→LUD-06/16 callback→pay→9735 with full receipt verification) paid through a NIP-47 NWC backend behind a NIP-47-shaped WalletBackend interface; anon zaps need a separate receipt-provenance model and are deferred to v2; on-chain is out of scope.",
  "citations": [
    "NIP-57 (zap request 9734 tags/flow, zap receipt 9735 + verification, appendices A–C)",
    "NIP-47 (URI format, kinds 13194/23194/23195, nip44_v2 required unless legacy, method set, error codes)",
    "NIP-44 v2 (conversation key = HKDF-Extract(salt='nip44-v2'), padding, AAD=nonce MAC) — impl cross-validated 85/85 vs paulmillr/nip44 official vectors",
    "BIP-340 (signing; impl cross-validated vs official test-vectors.csv)",
    "LUD-06/LUD-07 (payRequest, callback), LUD-16 (lightning address incl. onion + payerData), LUD-18 (server-side splits)",
    "LUD-17 = lnurlp:// schemes (NOT anon zaps — misnomer corrected in §1.5)",
    "gnostr-btc-backend-v1 §2/§3/§7 (satoshi): NWC-first, Alby Hub/bridge endpoints, budget model, BOLT11 decode vector"
  ],
  "code_contract": {
    "api_or_abi": "WalletBackend ABC (§2.4: get_info/get_balance/make_invoice/pay_invoice/lookup_invoice/list_transactions, NIP-47-shaped DTOs); NWCBackend(uri) parses nostr+walletconnect://, gates on kind 13194, sends NIP-44-encrypted 23194, matches 23195 by e-tag, 30s timeout; ZapFlow: resolve_target(kind0)->lnurl -> fetch_metadata(gate allowsNostr+nostrPubkey) -> build_9734(tags per §1.3) -> GET callback -> confirm dialog (§3.4) -> WalletBackend.pay_invoice -> verify_9735(7 rules §1.4) -> aggregate totals",
    "test_vectors": [
      "test_vectors.json (attached): 9734 fixture id=ec408d8c79ce8a3d4b59ac8e393f25b7072a767f2f08e58d1ce2764b358d5699 sig=89c71ad5…cd8a9a; v2_zap_request_canonical_json is the FULL event JSON (incl. id+sig) — verifier recomputes id from NIP-01 serialization [0,pubkey,created_at,kind,tags,content] of the parsed object (verified: sha256 == id); callback URL v2_callback_url byte-exact",
      "9735 fixture id=2ea1fe3299ad000727bca7bac6be3c2c83905d03988b347a5cbec41a5e16a6ba: description == 9734 canonical JSON, bolt11 hrp 10u == amount tag 1000000 msat — verifier must accept; tamper any one field → reject",
      "NWC round-trip: 23194 content decrypts (NIP-44, conversation key from nwc_client_sec×nwc_wallet_pub) to v4_nwc_request_plaintext; response e-tag == request id; NIP-04 legacy payload pair included for bridge compat",
      "BOLT11 decode: lnbc2500u1pvjluez… => {mainnet, 250000 sat, 1496314658, hash 00010203…0102, '1 cup coffee'} (satoshi, stdlib-verified)"
    ],
    "standardness_vs_consensus": "Normative: NIP-57 tag sets/verification, NIP-47 kinds+encryption, LUD-06/16 wire formats. Convention (flagged in code): lud16>lud06 precedence, NIP-05 fallback, created_at ±60s callback tolerance, overpayment tolerance. Budget enforcement is wallet-service-side (hard) + app soft cap (GSettings monthly-zap-budget-sats default 100000) — QUOTA_EXCEEDED is terminal UX, never a retry. Node identity never committed: TEST_LND_REST_URL/TEST_NWC_URI env placeholders only (James security rule)."
  },
  "open_questions": [
    "Wallet UI surface (dedicated page vs composer) — product call, non-blocking",
    "First live endpoint: LND bridge vs Alby Hub-on-LND (docs/test order only; app is NWC-identical) — James supplies endpoint at implementation start",
    "NIP-05 zap fallback: include convention (recommended) or strict-spec-only"
  ]
}
```
