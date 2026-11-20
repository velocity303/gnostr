# src/gnostr/trust_shield.py
"""trust-shield-v1 — WoT spam collapse protocol core (spec trust-shield-v1).

Pure protocol logic, NO GTK: rank-event validation (R1-R7), staleness,
policy decisions, relay-hint validation, kind-10040 discovery parsing and
REQ construction. The client owns the fetch orchestration; the UI owns
rendering. Golden acceptance floor: tests/test_trust_shield.py against
tests/fixtures/trust_shield_test_vectors.json.

Everything here is inert unless GSettings `trust-shield` is true (default
false) — flag-off must be byte-for-byte the pre-feature behavior.
"""

import re
from urllib.parse import urlsplit

from gnostr.zaps.request import verify_event_sig

RANK_KIND = 30382  # NIP-85 rank event (addressable, d-tag = subject pubkey)
DISCOVERY_KIND = 10040  # NIP-85 rank-provider discovery
RANK_TAG = "30382:rank"  # kind-10040 tag naming a rank provider

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# decision outcomes
NORMAL = "normal"
COLLAPSE = "collapse"


def parse_rank_event(ev, provider_pk):
    """Validate one kind-30382 rank event against spec rules R1-R5.

    Returns {"subject", "rank", "created_at", "event_id", "extra_tags"} or
    None (drop the EVENT, never the subject — R7: an absent subject is
    'no rank', which is NOT rank 0).

    R1 kind == 30382 and signer == the configured provider pubkey.
    R2 gnostr's own id-recompute + schnorr gate (verify_event_sig) — the
       spec's NIP-85 'signed by provider' wording, enforced locally.
    R3 exactly one d tag, 64-char lowercase hex (the subject).
    R4 exactly one rank tag, integer 0..100.
    R5 created_at present (kept as rank_updated).
    """
    try:
        if ev.get("kind") != RANK_KIND or ev.get("pubkey") != provider_pk:
            return None  # R1
        tags = ev.get("tags") or []
        created_at = ev.get("created_at")
    except AttributeError:
        return None
    if not isinstance(created_at, int):
        return None  # R5
    if not verify_event_sig(ev):
        return None  # R2
    ds = [
        t[1]
        for t in tags
        if isinstance(t, (list, tuple)) and len(t) >= 2 and t[0] == "d"
    ]
    if len(ds) != 1 or not _HEX64.match(ds[0]):
        return None  # R3
    rs = [
        t[1]
        for t in tags
        if isinstance(t, (list, tuple)) and len(t) >= 2 and t[0] == "rank"
    ]
    if len(rs) != 1:
        return None  # R4
    try:
        rank = int(rs[0])
    except (TypeError, ValueError):
        return None
    if not 0 <= rank <= 100:
        return None  # R4
    return {
        "subject": ds[0],
        "rank": rank,
        "created_at": created_at,
        "event_id": ev.get("id"),
        "extra_tags": [
            t
            for t in tags
            if isinstance(t, (list, tuple)) and t and t[0] not in ("d", "rank")
        ],
    }


def select_rank(rows):
    """R6: highest rank_updated wins; ties broken by lexicographically
    largest event id (determinism). `rows` = dicts with rank,
    rank_updated, event_id. Returns int rank or None."""
    best = None
    for r in rows:
        key = (r["rank_updated"], r.get("event_id") or "")
        if best is None or key > best[0]:
            best = (key, r["rank"])
    return best[1] if best else None


def is_stale(rank_updated, max_age_days, now):
    """§2 staleness: a row older than max-age-days is refetch-eligible;
    until the refetch lands the stale rank keeps deciding (no flicker)."""
    return now - rank_updated > max_age_days * 86400


class TrustShieldPolicy:
    """§3.3 decision ladder, pure. E1 self, E2 follows, E3 no-rank,
    E4 rank >= threshold -> normal; otherwise collapse."""

    def __init__(self, threshold, follows, self_pk):
        self.threshold = threshold
        self.follows = follows
        self.self_pk = self_pk

    def decide(self, author_pk, rank):
        if author_pk == self.self_pk:
            return NORMAL  # E1
        if author_pk in self.follows:
            return NORMAL  # E2
        if rank is None:
            return NORMAL  # E3 (absent != 0)
        if rank >= self.threshold:
            return NORMAL  # E4
        return COLLAPSE


def normalize_relay_hint(hint):
    """§1.2: validate a kind-10040 relay hint. ws:// or wss:// scheme, a
    parseable hostname containing a dot (rejects 'ws://tapestry/relay'),
    one trailing '/' stripped. Returns the normalized URL or None (the
    hint is then simply absent)."""
    if not isinstance(hint, str):
        return None
    hint = hint.strip()
    try:
        parts = urlsplit(hint)
        host = parts.hostname or ""
    except ValueError:
        return None
    if parts.scheme not in ("ws", "wss") or "." not in host:
        return None
    if not parts.netloc:
        return None
    out = hint.rstrip("/")
    return out or None


def parse_discovery_entries(ev):
    """kind-10040 -> list of (provider_pk, relay_hint_or_None). Only the
    exact tag '30382:rank' counts (§1.2); other tags are ignored. The
    optional third element is the answer-relay hint (validated)."""
    out = []
    try:
        tags = ev.get("tags") or []
    except AttributeError:
        return out
    for t in tags:
        if not isinstance(t, (list, tuple)) or len(t) < 2:
            continue
        if t[0] != RANK_TAG:
            continue
        provider = t[1]
        if not _HEX64.match(provider or ""):
            continue
        hint = normalize_relay_hint(t[2]) if len(t) >= 3 else None
        out.append((provider, hint))
    return out


def rank_candidates(subject_entries):
    """Aggregate discovery entries into provider candidates (§1.2 step 3).

    `subject_entries`: {subject_pk: [(provider_pk, relay_or_None), ...]}.
    Returns a list sorted by number of distinct subjects listing the
    provider (desc, then provider pk for determinism), each:
    {"provider", "listed_by": set, "relay": most-common-valid-hint|None}.
    """
    providers = {}
    for subject, entries in subject_entries.items():
        seen = set()
        for provider, relay in entries:
            if provider in seen:
                continue  # one listing per subject per provider
            seen.add(provider)
            slot = providers.setdefault(
                provider, {"provider": provider, "listed_by": set(), "relays": {}}
            )
            slot["listed_by"].add(subject)
            if relay:
                slot["relays"][relay] = slot["relays"].get(relay, 0) + 1
    out = sorted(
        providers.values(), key=lambda p: (-len(p["listed_by"]), p["provider"])
    )
    for p in out:
        ranked = sorted(p["relays"].items(), key=lambda kv: (-kv[1], kv[0]))
        p["relay"] = ranked[0][0] if ranked else None
    return out


def rank_req_filters(provider, subjects):
    """§4 REQ filter for a rank batch: authors=[provider], kinds=[30382],
    #d = subjects (max 50 per batch — caller chunks). Byte-shape is
    pinned by the golden expected_rank_fetch_req_example."""
    return {"authors": [provider], "kinds": [RANK_KIND], "#d": list(subjects)[:50]}
