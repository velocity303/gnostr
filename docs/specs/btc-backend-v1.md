# gnostr-btc-backend-v1 — Bitcoin/Lightning backend survey & recommendation

**Card:** t_efbb2ccb · **Author:** satoshi (protocol researcher) · **Date:** 2026-10-21
**Companion to:** `gnostr-zaps-payments-v1` (miku owns the nostr protocol contract; this doc owns the bitcoin/lightning side)
**Status:** scoping survey for James' review — no implementation.

---

## 0. TL;DR recommendation

**Confirmed: NWC (NIP-47) as the app-side payment protocol, with self-hosted Alby Hub as the
primary recommended endpoint and benthecarman's `nostr-wallet-connect-lnd` bridge as the
secondary for existing LND users.** Direct LND/CLN REST is deferred to a later power-user
fallback.

The decisive argument specific to gnostr: gnostr **already speaks nostr relays and already
implements NIP-44** (from the G3 DM work). NWC is just NIP-44-encrypted kind 23194/23195
events over the relay pool the app already maintains. The app-side NWC client is ~200 lines
of stdlib Python on top of code that exists today — zero new pip dependencies, zero new
Flatpak permissions, and the node never opens an inbound port.

---

## 1. Backend options compared

| Backend | Auth model | Minimum-privilege story | Python client maturity | Flatpak viability | Maintenance (Oct 2026) |
|---|---|---|---|---|---|
| **Alby Hub** (getAlby/hub, native NWC) | NWC connection URI: per-connection 32-byte secret key; wallet-service-side budgets, expiry, read-only flags | Best-in-class: per-app connections, each independently budgeted/expiring/revocable from the Hub UI; node keys never leave the Hub | No Python client needed — NWC client is app-side (see §4); Hub also exposes a JWT admin API (not needed) | Perfect: app only opens wss:// to relays (network-only, same as existing nostr traffic) | Very active. v1.24.3 released 2026-10-16. Note: critical vuln in v1.7.0–v1.18.5 (publicly-exposed admin UI) disclosed 2026-09, fixed in v1.24.0 — healthy security response, but see §3 |
| **LND + `nostr-wallet-connect-lnd`** (benthecarman bridge) | Same NWC URI on the app side; bridge authenticates to LND with a URI-scoped baked macaroon | Bridge README bakes a 6-URI macaroon (GetInfo, AddInvoice, LookupInvoice, ChannelBalance, SendPaymentV2, TrackPaymentV2); receive-only mode possible | Bridge is Rust, runs on the node — app side is plain NWC again | Perfect (same as Alby Hub row) | Active: commits 2026-10-07 incl. NWC-321 BIP-321 support. Small project (24★, effectively 1 maintainer) — acceptable because it's ~small Rust surface and MIT |
| **LND direct** (gRPC/REST + macaroons) | TLS + macaroons (HMAC bearer tokens) | Bake URI-scoped macaroon (same 6 URIs as bridge). **Never admin.macaroon.** Weakness: macaroons are stateless — **no per-token revocation**; rotation = re-key the node | `lnd-grpc-client` / `lnbits`-adjacent libs exist but are third-party; REST via plain `requests` is honestly fine | Network-only OK (HTTPS to node LAN IP); self-signed TLS needs explicit pinning UI; gRPC mTLS client certs need file access — REST is the Flatpak-friendly path | LND itself very active (v0.21.x line). Third-party Python gRPC wrappers: mediocre |
| **Core Lightning** (`pyln-client` or clnrest + runes) | Unix-socket JSON-RPC (pyln) or clnrest HTTPS with `Rune:` header | **Runes are the best scoped-credential design in Lightning**: per-method, per-amount, per-time restrictions, and deactivatable (`forlornrune`) | `pyln-client` is first-party, PyPI, maintained by CLN team — best Python client of any backend | pyln needs the **unix socket** → not reachable from Flatpak sandbox without host filesystem access. clnrest (built-in since v23.08) is TCP/HTTPS → sandbox-friendly; websocket notifications included | Very active (CLN v24.x+; clnrest docs updated 2026-09). `c-lightning-REST` (RTL) is deprecated upstream → use clnrest |
| **Eclair** (ACINQ) | HTTP JSON API, single API password / cookie file | Coarse: one shared API secret, no per-app scoping | No official Python client; plain `requests` against the documented REST API | Network-only OK (HTTP/HTTPS + Basic auth) | Active (ACINQ, production node operator) |
| **LDK Node** (embedded node in-app) | App *is* the node; local admin API; needs chain source (Esplora) + usually a peer/LSP for inbound liquidity | No credential delegation story at all — the app holds the seed | UniFFI Python bindings exist but low-level; Rust binary wheel → must match Flatpak SDK arch incl. aarch64 for Librem 5 | Poor fit: embedded node needs persistent state dir + chain backend + channel management UX inside a chat app | LDK itself very active; Python bindings are the least-traveled path |
| **Breez SDK** (embedded node + LSP) | App holds seed; node state runs on **Greenlight** (Breez infra, client-side key derivation = non-custodial but service-dependent); LSP required for inbound liquidity | Non-custodial ≠ own-node: funds depend on Breez Greenlight + LSP availability. **Assessment: violates the own-node preference** — flag for James, do not adopt | `breez-sdk` / `breez-sdk-spark` on PyPI (uniffi wheels); aarch64 wheel availability for Flatpak/Librem 5 must be verified before considering | Needs network to Breez services + native wheels in the Flatpak runtime | Active |

**Verdict on the "embedded wallet" question (breez/LDK):** both make the *app* the wallet.
That is the "from-scratch HD wallet in-app" outcome James explicitly excluded, just with the
node logic in a Rust library instead of hand-written. Breez additionally adds a hard service
dependency (Greenlight + LSP). Neither belongs in gnostr v1.

---

## 2. Recommendation (confirm/challenge of the expected shape)

**Confirm** the expected shape, with two sharpenings:

1. **NWC is not just "an option that works" — for gnostr it is nearly free.** The transport
   (nostr relays), the encryption (NIP-44, shipped in G3), and the event plumbing are already
   in the codebase. A native NWC client module (`nwc.py`: parse `nostr+walletconnect://` URI,
   publish kind 13194 lookup, send kind 23194 requests, await kind 23195 responses) reuses
   all of it. Alternative: `nostr-sdk` PyPI (rust-nostr bindings, includes an NWC client) —
   but that adds a Rust binary wheel to a stdlib-heavy project and must ship aarch64 wheels
   for the Librem 5 Flatpak. **Recommend the native module; keep nostr-sdk as fallback if
   NWC extensions grow beyond what we want to hand-roll.**
2. **Endpoint priority:** Alby Hub first (purpose-built NWC wallet service, best budget/
   revocation UX, active security posture), `nostr-wallet-connect-lnd` second (lets James and
   other LND users reuse an existing node). Direct LND/CLN REST: explicitly out of scope for
   v1; revisit only if users demand features NWC doesn't cover (channel management, on-chain).

One dependency to flag for miku's card: NIP-47 now has **optional extensions** (info-event
`extensions` tag, e.g. `02 03 04`) and **NWC-321** (`pay`/`receive` BIP-321 methods, already
supported by the LND bridge). The zap contract should pin which core methods are required:
`pay_invoice`, `make_invoice`, `get_balance`, `lookup_invoice`, `get_info`, `list_transactions`.

---

## 3. Security model

### 3.1 Credential scoping per backend

| Backend | Credential the app holds | Minimum scope for v1 panel | Revocation |
|---|---|---|---|
| Alby Hub | NWC connection URI (wallet svc pubkey + relay + 32-byte client secret) | Per-connection budget (e.g. 100k sat/mo for zaps), optional expiry, read-only flag for balance-only mode | Delete connection in Hub UI → wallet service ignores the key. Instant, per-app |
| LND via bridge | Same NWC URI | Bridge enforces NIP-47 methods; bridge's own macaroon is URI-scoped (6 URIs, see §1) | Revoke NWC connection in bridge config; macaroon itself not individually revocable |
| LND direct | Baked macaroon (hex/base64) | `uri:/lnrpc.Lightning/{GetInfo,AddInvoice,LookupInvoice,ChannelBalance}` + `uri:/routerrpc.Router/{SendPaymentV2,TrackPaymentV2}` (coarse alternative: `invoice:write`, `offchain:read/write`, `onchain:read`) | **None per-token** — macaroons are stateless HMACs; rotation invalidates *all* macaroons. This is the main security argument against direct LND |
| CLN direct | Rune (base64, `Rune:` header to clnrest) | Rune restricted `method∈{getinfo,invoice,lookupinvoice,pay,listinvoices,listpays}` + `rate`/`time` restrictions | `forlornrune` deactivates a single rune — better than LND, worse UX than NWC |
| Eclair | Shared API password | No per-app scoping exists | Rotate password + restart |

### 3.2 Where credentials live

The NWC connection URI **is** a spending credential (it carries the client secret key).
Store it in **libsecret via the app's existing KeyManager** — never in config JSON, never in
GSettings plaintext, never in the Flatpak app dir. Same treatment as the DM NIP-44 private
key already handled there.

### 3.3 Leak blast radius

- **NWC URI leaked:** attacker can pay within the connection's budget/method restrictions
  until revoked. Mitigate at mint time: separate connection per purpose (one "zaps"
  connection with a small monthly budget + `pay_invoice` only; a second read-only
  connection for the balance panel if desired). Budget is enforced by the wallet service,
  not the app — the app cannot overspend what it was given.
- **LND macaroon leaked:** same scope but **no revocation** — worst case is a permanent
  drain channel until node re-key.
- **Why the app must never hold an on-chain xprv or admin macaroon:** gnostr is a networked
  GUI that renders untrusted nostr content inside a Flatpak, with third-party Python deps.
  A compromised render path or dependency must at worst lose a budgeted zap allowance, not
  the node. Payment authority should be scoped, budgeted, revocable, and held by a different
  machine (the node) by default. This is exactly the layering Alby Hub's per-connection
  model exists to provide.
- **Alby Hub operational note:** the 2026-09 disclosure (critical, v1.7.0–v1.18.5, admin UI
  takeover when publicly exposed; fixed v1.24.0) is an argument for the docs we ship to say:
  keep the Hub admin UI LAN/Tailscale-only, always-on VPN or tailnet, never port-forward the
  management port. NWC itself is outbound-relay and unaffected.

---

## 4. Minimal v1 wallet panel → smallest API surface

Panel scope: **balance, create invoice, pay invoice, transaction list, incoming-payment
notification.** Nothing else (no channel mgmt, no on-chain addresses).

| Feature | NWC method | LND equivalent | CLN equivalent | Eclair equivalent |
|---|---|---|---|---|
| Balance | `get_balance` | `ChannelBalance` | `getinfo`/`getbalance` via clnrest | `globalbalance` |
| Create invoice | `make_invoice` | `AddInvoice` | `invoice` | `receive` (invoice) |
| Pay invoice | `pay_invoice` | `SendPaymentV2` | `pay` | `payinvoice` |
| Tx list | `list_transactions` (+`lookup_invoice`) | `ListInvoices`/`ListPayments` | `listinvoices`/`listpays` | `listinvoices` |
| Incoming notification | kind 23195 response + wallet-event extension; fallback: poll `lookup_invoice` | `SubscribeInvoices` (stream) | `waitanyinvoice` (long-poll — ideal) | websocket/ZMQ events |
| Capability discovery | kind 13194 info event (`content` = space-separated methods, `encryption` tag) | n/a | n/a | n/a |

NWC encryption: **NIP-44 v2 required** (NIP-04 deprecated; absence of `encryption` tag in
the info event ⇒ assume nip04 legacy). gnostr already has NIP-44 — reuse it verbatim.

---

## 5. Test strategy

**Key insight: NWC decouples app tests from lightning infrastructure.** The app talks only to
relays with NIP-44 events, so CI needs no bitcoind/LND for the panel logic:

1. **Unit (CI, no infra):** in-process fake NWC wallet service — a nostr relay stub (or the
   existing relay test harness) + a fixture wallet service that answers kind 23194 with
   canned kind 23195 responses. Assert request/response round-trip, error codes
   (`INSUFFICIENT_BALANCE`, `QUOTA_EXCEEDED`), timeout handling.
2. **BOLT-11 decode vector (CI, stdlib-only):** decode must match — canonical BOLT-11 spec
   invoice (verified by hand with a stdlib bech32 decoder on 2026-10-21):
   `lnbc2500u1pvjluezpp5qqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqypqdq5xysxxatsyp3k7enxv4jsxqzpuaztrnwngzn3kdzw5hydlzf03qdgm2hdq27cqv3agm2awhz5se903vruatfhq77w3ls4evs3ch9zw97j25emudupq63nyw24cg27h2rspfj9srp`
   → network `mainnet`, amount **250000 sat**, timestamp **1496314658** (2017-06-01T10:57:38Z),
   payment_hash `0001020304050607080900010203040506070809000102030405060708090102`,
   description `1 cup coffee`.
3. **Integration (nightly/manual, Polar):** Polar (jamaljsr/polar) spins regtest Bitcoin
   Core + LND + CLN + Eclair in one click; run Alby Hub (Docker) or the LND bridge against
   it, mint a real NWC connection, and run the panel end-to-end. Polar's docker-compose
   export is the seed for a CI nightly job.
4. **Librem 5 smoke:** panel over the same relay path as DMs — no new network behavior to
   validate beyond relay reachability.

---

## 6. Open questions for James

1. **Which node do you actually run at home today** — LND, CLN, Alby Hub, Umbrel/StartOS
   bundle, or nothing yet? This picks the first endpoint we validate against (and whether we
   need the LND bridge or Alby Hub docs first).
2. **Lightning-only for v1?** Recommendation: yes — on-chain features (addresses, on-chain
   balance) stay out of the panel; NWC doesn't cover them and it keeps the credential story clean.
3. **New pip dependency tolerance:** recommended answer is **zero new deps** (native
   `nwc.py` on existing NIP-44 + websocket code). Candidates if we change our mind:
   `nostr-sdk` (rust-nostr bindings, has NWC client; check aarch64 wheels for the Librem 5
   Flatpak), `pyln-client` (only if CLN-direct ever ships).
4. **Relay choice for NWC traffic:** reuse the user's configured relay set, or a dedicated
   NWC relay (privacy: payment metadata stays off the social relays)? NIP-47 explicitly
   suggests a dedicated relay where feasible.
5. **Zap budget defaults:** what monthly cap should the "zaps" connection be minted with
   (e.g. 100k sat/mo), and should auto-zaps be hard-capped below interactive zaps?
6. **Alby Hub hosting:** is James willing to run Alby Hub on the home server/Legion, or is
   the expectation strictly "connect to whatever node already exists"?

---

## 7. Machine-readable handoff (for miku / code-cat)

```json
{
  "spec_id": "gnostr-btc-backend-v1",
  "domain": "bitcoin",
  "claim": "NIP-47 NWC over the app's existing relay+NIP-44 stack is the correct gnostr payment transport; self-hosted Alby Hub or nostr-wallet-connect-lnd are the recommended wallet services; the app never holds node admin credentials or any xprv.",
  "citations": [
    "NIP-47 (nips.nostr.com/47): kinds 13194/23194/23195, nip44_v2 required, connection URI format, error codes",
    "github.com/getAlby/hub v1.24.3 (2026-10-16); vuln advisory v1.7.0-v1.18.5 fixed v1.24.0",
    "github.com/benthecarman/nostr-wallet-connect-lnd README (2026-10-07): minimum macaroon URIs, NWC-321",
    "docs.corelightning.org/docs/rest: clnrest Rune header, websocket notifications; runes via createrune/forlornrune",
    "BOLT-11 spec test vectors (lightning/bolts)"
  ],
  "code_contract": {
    "api_or_abi": "nwc.py: NWCClient(uri: str) with .get_info() .get_balance() .make_invoice(amount_msat, desc) .pay_invoice(bolt11) .lookup_invoice(payment_hash) .list_transactions(); parses nostr+walletconnect://<wallet_pubkey>?relay=<r>&secret=<hex32>; requests = NIP-44-encrypted kind 23194 to wallet pubkey, responses = kind 23195 matched by e-tag; capability gate on kind 13194 info event content",
    "test_vectors": [
      "BOLT11 decode: lnbc2500u1pvjluezpp5qqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqypqdq5xysxxatsyp3k7enxv4jsxqzpuaztrnwngzn3kdzw5hydlzf03qdgm2hdq27cqv3agm2awhz5se903vruatfhq77w3ls4evs3ch9zw97j25emudupq63nyw24cg27h2rspfj9srp => {network: mainnet, amount_sat: 250000, timestamp: 1496314658, payment_hash: 0001020304050607080900010203040506070809000102030405060708090102, description: '1 cup coffee'} (verified with stdlib bech32 decode, 2026-10-21)",
      "NWC round-trip against fake wallet service: get_balance request kind 23194 with encryption tag nip44_v2 => response kind 23195, e-tag == request id, result_type == 'get_balance', encrypted content {balance: <msat int>}"
    ],
    "standardness_vs_consensus": "Entire surface is wallet-service API + nostr relay policy; no consensus-rule interaction. Budget/permission enforcement lives at the wallet service (Alby Hub connection settings), NOT in the app — app must treat budget errors (QUOTA_EXCEEDED) as first-class UX, not retry loops."
  },
  "open_questions": [
    "Which node James runs at home (picks first validated endpoint)",
    "Lightning-only v1 (recommended yes) vs on-chain ever",
    "Native nwc.py (recommended, zero deps) vs nostr-sdk pip wheel + aarch64/Librem5 wheel check",
    "Dedicated NWC relay vs reuse social relay set",
    "Default zap budget cap per connection"
  ]
}
```
