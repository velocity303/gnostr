"""NIPW P1 long-form data layer (spec nipw2026-11-longform-v1) — kind-30023
articles, kind-1111 comments, kind-31234 draft wraps.

GTK-free, network-free parsing + REQ-filter builders + client-side matching.
The client wires these into opt-in section fetches (never the main kind-1
feed — agentic-flood posture); the follow-on UI card consumes ArticleCard /
CommentInfo / DraftEntry rows.

Probe audit 2026-11-23 (live `nak` REQ sweep, 4 relays: nos.lol,
relay.snort.social, nostr.wine, relay.primal.net — raw evidence stored with
the P1 card) answers the spec's three open questions and corrects two
filter choices the spec could not know before the audit:

1. #K filter support: relays accept the literal key "K" in a REQ filter but
   resolve it against the *kind slot* of K tags (khatru semantics); a
   #K:[64-hex event id] returns 0 events on 4/4 relays. Live 1111 comments
   carry K holding the parent KIND ("30023"), not the article id. Primary
   comment REQ therefore uses #E:[article_id] + #A:[article_address] — both
   live-verified 4/4 — instead of the spec's #K:[article_id].
2. `authors` matches the event AUTHOR, not the comment target: 46/46
   comments returned by authors=[pubkey...] were authored by the queried
   pubkeys. The spec-literal fallback authors=[article author] therefore
   recovers only the author's own replies; the fallback REQ carries that
   filter AND a broad recent-1111 filter in the same REQ so the spec's
   client-side K-tag matching can actually recover comments *targeting*
   the article on a relay with broken tag filtering.
3. Image policy: 100% of sampled live 30023 covers were https URLs (0
   http/relative/other) — the https-only policy (drop cover otherwise)
   cannot regress a real feed.
4. Legacy 30024 drafts: 7 live on nos.lol (other users'), 0 on our own
   test account; they are plaintext, unwrapped drafts. Own drafts in the
   wild are 31234 wraps, so v1 reads only 31234 — 30024 is defined for
   documentation but not queried (spec's "not worth reading" leaning,
   confirmed).

NIP-22/NIP-26 case discipline (spec requirement): uppercase K/E/A/P are
ROOT pointers (the article), lowercase k/e/a/p are PARENT pointers (the
immediately preceding comment). Both are kept distinct in CommentInfo;
article matching considers every candidate binding value of both sets.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import nip44, nostr_utils

# --- kinds ---------------------------------------------------------------

KIND_COMMENT = 1111  # NIP-22 article comments
KIND_ARTICLE = 30023  # long-form articles (addressable, replaceable on d)
KIND_DRAFT_LEGACY = 30024  # old plaintext drafts — documented, NOT queried
KIND_DRAFT_WRAP = 31234  # NIP-37 private draft wraps

# --- fetch budgets --------------------------------------------------------

ARTICLE_FEED_LIMIT = 30  # opt-in section: 30 cards first (spec)
COMMENT_FETCH_LIMIT = 100  # per relay per phase
COMMENT_FALLBACK_LIMIT = 200  # broad recent-1111 half of the fallback REQ
DRAFT_FETCH_LIMIT = 100  # {"kinds":[31234],"authors":[self],"limit":100}

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


# --- articles ---------------------------------------------------------------


@dataclass
class ArticleCard:
    """A parsed kind-30023 article. `d` is REQUIRED (NIP-33 coordinate —
    without it the event is not addressable and is dropped). title/image/
    summary are optional; image honors the https-only policy."""

    id: str
    pubkey: str
    d: str
    title: str | None
    image: str | None  # https-only cover; anything else dropped
    summary: str | None
    markdown: str
    published_at: int | None  # ["published_at", unix]; None -> created_at
    created_at: int

    @property
    def address(self) -> str:
        return f"{KIND_ARTICLE}:{self.pubkey}:{self.d}"


def parse_article_event(ev) -> ArticleCard | None:
    """Parse a kind-30023 event into an ArticleCard. Returns None for a
    non-article, a d-less event (not addressable), or a malformed one —
    never raises on live junk."""
    try:
        if not isinstance(ev, dict) or ev.get("kind") != KIND_ARTICLE:
            return None
        tags = ev.get("tags") or []
        d = _tag_value(tags, "d")
        if not d:
            return None
        pubkey = str(ev.get("pubkey") or "")
        if not _HEX64.match(pubkey):
            return None
        eid = str(ev.get("id") or "")
        if not _HEX64.match(eid):
            eid = nostr_utils.compute_event_id(ev)
        image = _tag_value(tags, "image")
        if not image.startswith("https://"):
            image = None  # https-only policy (probe: live covers 100% https)
        published_at = _tag_int(tags, "published_at")
        created_at = int(ev.get("created_at") or 0)
        content = ev.get("content")
        return ArticleCard(
            id=eid,
            pubkey=pubkey,
            d=d,
            title=_tag_value(tags, "title") or None,
            image=image,
            summary=_tag_value(tags, "summary") or None,
            markdown=content if isinstance(content, str) else "",
            published_at=published_at,
            created_at=created_at,
        )
    except (KeyError, TypeError, ValueError):
        return None


def article_address(ev) -> str | None:
    """The NIP-33 address (30023:pubkey:d) of a raw 30023 event, or None."""
    try:
        if not isinstance(ev, dict) or ev.get("kind") != KIND_ARTICLE:
            return None
        d = _tag_value(ev.get("tags") or [], "d")
        if not d:
            return None
        return f"{KIND_ARTICLE}:{ev['pubkey']}:{d}"
    except (KeyError, TypeError):
        return None


def article_feed_filter(limit: int = ARTICLE_FEED_LIMIT) -> dict:
    """Opt-in article-section subscription filter (spec: 30 first). The
    caller opens this as its OWN live REQ — the main kind-1 feed is never
    broadened to 30023 (agentic-flood posture)."""
    return {"kinds": [KIND_ARTICLE], "limit": limit}


def dedupe_articles(cards):
    """Edit-replacement for article LISTS: newest-only per (pubkey, d)
    collision (get_event_by_a is the point-query twin of this rule).
    `published_at` wins over `created_at` for recency (an edit that fixes
    published_at must still rank by it); newest-first output."""
    out = {}

    def rank(c):
        return (c.published_at or c.created_at, c.id)

    for c in cards:
        key = (c.pubkey, c.d)
        cur = out.get(key)
        if cur is None or rank(c) > rank(cur):
            out[key] = c
    return sorted(out.values(), key=rank, reverse=True)


# --- comments (NIP-22) ------------------------------------------------------


@dataclass
class CommentInfo:
    """A parsed kind-1111 comment with ROOT and PARENT pointers kept in
    their native case (uppercase = root/article, lowercase = immediate
    parent). `target_refs` is the union of every candidate binding value
    (uppercase E/A/K values + lowercase e/a values) — article matching is
    `target_refs ∩ {article_id, address} != ∅`, which is the spec's
    client-side fallback filter expressed once."""

    id: str
    pubkey: str
    created_at: int
    content: str
    # Uppercase root pointers (NIP-26 style)
    root_id: str | None = None  # uppercase E, or 64-hex uppercase K (vector form)
    root_address: str | None = None  # uppercase A, or addr-valued uppercase K (live form)
    root_kind: str | None = None  # uppercase K holding the parent kind (live form)
    root_pubkey: str | None = None  # uppercase P
    # Lowercase parent pointers (NIP-22)
    parent_id: str | None = None  # lowercase e (comment id, or article id on top-level)
    parent_address: str | None = None  # lowercase a (on top-level comments under an article)
    parent_kind: str | None = None  # lowercase k
    parent_pubkey: str | None = None  # lowercase p
    target_refs: frozenset = field(default_factory=frozenset)


def parse_comment_event(ev) -> CommentInfo | None:
    """Parse a kind-1111 event. Returns None for non-comments and
    malformed events — never raises on live junk."""
    try:
        if not isinstance(ev, dict) or ev.get("kind") != KIND_COMMENT:
            return None
        pubkey = str(ev.get("pubkey") or "")
        if not _HEX64.match(pubkey):
            return None
        tags = ev.get("tags") or []
        root_id = _tag_value(tags, "E") or None
        root_address = _tag_value(tags, "A") or None
        root_kind = None
        k_val = _tag_value(tags, "K")
        if k_val:
            if _HEX64.match(k_val):
                root_id = root_id or k_val  # spec-vector form: K holds the id
            elif ":" in k_val:
                root_address = root_address or k_val  # live form: K holds the addr
            else:
                root_kind = k_val  # live form: K holds the parent KIND
        parent_id = _tag_value(tags, "e") or None
        parent_address = _tag_value(tags, "a") or None
        refs = {
            v
            for v in (
                root_id,
                root_address,
                k_val,
                parent_id,
                parent_address,
                _tag_value(tags, "E"),
                _tag_value(tags, "A"),
            )
            if v
        }
        return CommentInfo(
            id=ev.get("id") or "",
            pubkey=pubkey,
            created_at=int(ev.get("created_at") or 0),
            content=ev.get("content") if isinstance(ev.get("content"), str) else "",
            root_id=root_id,
            root_address=root_address,
            root_kind=root_kind,
            root_pubkey=_tag_value(tags, "P") or None,
            parent_id=parent_id,
            parent_address=parent_address,
            parent_kind=_tag_value(tags, "k") or None,
            parent_pubkey=_tag_value(tags, "p") or None,
            target_refs=frozenset(refs),
        )
    except (KeyError, TypeError, ValueError):
        return None


def comment_matches(comment, article_id: str, address: str | None = None) -> bool:
    """True when a CommentInfo (or raw 1111 event) binds to the article by
    id (E/K/e tags) or address (A/a tags). Accepts raw events for the cheap
    hot path in _handle_event."""
    if isinstance(comment, CommentInfo):
        refs = comment.target_refs
    else:
        info = parse_comment_event(comment)
        if info is None:
            return False
        refs = info.target_refs
    keys = {article_id}
    if address:
        keys.add(address)
    return bool(refs & keys)


def comments_for_article(events, article_id: str, address: str | None = None):
    """Client-side K-tag filter (spec fallback, used in BOTH phases): parse,
    keep comments bound to the article, dedup by id, newest first."""
    out = {}
    for ev in events:
        info = ev if isinstance(ev, CommentInfo) else parse_comment_event(ev)
        if info is None or not info.id:
            continue
        if comment_matches(info, article_id, address):
            out[info.id] = info
    return sorted(out.values(), key=lambda c: (c.created_at, c.id), reverse=True)


def build_comment_event(pubkey, article, content, created_at=None):
    """Unsigned kind-1111 template for a top-level comment on an article
    (NIP-22). `article` is an ArticleCard or a raw 30023 event dict.

    Tags: lowercase parent pointers (a top-level comment's parent IS the
    article) + the uppercase root pointers E/A/K/P — the live-probed khatru
    shape (2026-11-23: relays index #E/#A on those tags; #K matches the
    parent KIND slot, never an event id). This makes the post round-trip
    findable by comments_primary_filters + comment_matches.

    House unsigned-template pattern (groups.build_join_event convention):
    no id/sig — the downstream signer fills both, and
    compute_event_id(template) == id(signed), so an optimistic UI row keyed
    on the template id dedups against the relay echo. Returns None when the
    pubkey or article is malformed (a comment must bind a real addressable
    article — no address, no post).
    """
    try:
        if isinstance(article, dict):
            article_id = str(article.get("id") or "")
            address = article_address(article)
            author_pk = str(article.get("pubkey") or "")
        else:
            article_id = str(getattr(article, "id", "") or "")
            address = getattr(article, "address", None)
            author_pk = str(getattr(article, "pubkey", "") or "")
        if not _HEX64.match(article_id) or not _HEX64.match(str(pubkey or "")):
            return None
        if not address or not author_pk:
            return None
        return nostr_utils.build_event(
            str(pubkey),
            KIND_COMMENT,
            str(content),
            [
                ["e", article_id],
                ["a", address],
                ["k", str(KIND_ARTICLE)],
                ["p", author_pk],
                ["E", article_id],
                ["A", address],
                ["K", str(KIND_ARTICLE)],
                ["P", author_pk],
            ],
            created_at=created_at,
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def comments_primary_filters(
    article_id: str, address: str | None, limit: int = COMMENT_FETCH_LIMIT
):
    """One REQ, two filters: #E:[article_id] and #A:[address]. Live-verified
    4/4 relays 2026-11-23. (The spec's #K:[article_id] was probe-killed:
    khatru relays resolve #K against the K-tag KIND slot, never an event id
    — 0 results 4/4; see module docstring.)"""
    filters = [{"kinds": [KIND_COMMENT], "#E": [article_id], "limit": limit}]
    if address:
        filters.append({"kinds": [KIND_COMMENT], "#A": [address], "limit": limit})
    return filters


def comments_fallback_filters(
    article_pubkey: str, limit: int = COMMENT_FETCH_LIMIT
):
    """Fallback REQ for relays with broken tag filtering (spec's client-side
    K-tag filtering is the recovery): authors=[article author] recovers the
    author's own replies (spec-literal), and the broad recent-1111 filter
    rides the same REQ so comments *targeting* the article are recoverable
    client-side (authors matches the event author — proven 46/46 in the
    probe, so spec-literal alone cannot find third-party comments)."""
    return [
        {"kinds": [KIND_COMMENT], "authors": [article_pubkey], "limit": limit},
        {"kinds": [KIND_COMMENT], "limit": COMMENT_FALLBACK_LIMIT},
    ]


# --- drafts (NIP-37) --------------------------------------------------------


@dataclass
class DraftEntry:
    """A read-only draft row from a kind-31234 wrap. `nevent_id` is the id
    of the wrapped draft event (decrypted when a key is available, decoded
    from a nevent pointer otherwise, None when undecryptable). target_kind
    comes from the wrap's k tag; d prefers the inner draft's d, falling back
    to the wrap's d."""

    wrap_id: str
    nevent_id: str | None
    target_kind: int | None
    d: str | None
    created_at: int


def drafts_filter(self_pubkey: str, limit: int = DRAFT_FETCH_LIMIT) -> dict:
    return {"kinds": [KIND_DRAFT_WRAP], "authors": [self_pubkey], "limit": limit}


def parse_draft_event(ev, privkey_hex=None, self_pubkey_hex=None) -> DraftEntry | None:
    """Parse a kind-31234 draft wrap. NIP-37: an empty content is the
    deleted-draft signal (dropped); an expired `expiration` tag is dropped.
    With `privkey_hex` (own key) the content is NIP-44 decrypted with the
    self↔self conversation key — wrapper pubkey == author == self."""
    try:
        if not isinstance(ev, dict) or ev.get("kind") != KIND_DRAFT_WRAP:
            return None
        tags = ev.get("tags") or []
        expiration = _tag_int(tags, "expiration")
        if expiration is not None and expiration < int(time_now()):
            return None
        content = ev.get("content")
        if content == "" or content is None:
            return None  # NIP-37: empty content signals a deleted draft
        d = _tag_value(tags, "d") or None
        k_val = _tag_value(tags, "k")
        target_kind = int(k_val) if k_val and k_val.isdigit() else None
        wrap_id = ev.get("id") or ""
        created_at = int(ev.get("created_at") or 0)
        nevent_id = None
        if isinstance(content, str) and content.startswith("nevent1"):
            decoded = nostr_utils.decode_nevent_full(content)
            if decoded:
                nevent_id = decoded[0]
        elif isinstance(content, str) and privkey_hex:
            author = ev.get("pubkey") or self_pubkey_hex or ""
            try:
                conv = nip44.get_conversation_key(
                    bytes.fromhex(privkey_hex), bytes.fromhex(author)
                )
                draft = json.loads(nip44.decrypt(content, conv))
                if isinstance(draft, dict):
                    inner_id = draft.get("id")
                    if _HEX64.match(inner_id or ""):
                        nevent_id = inner_id
                    else:
                        nevent_id = nostr_utils.compute_event_id(draft)
                    inner_d = _tag_value(draft.get("tags") or [], "d")
                    if inner_d:
                        d = inner_d
                    if target_kind is None:
                        inner_k = draft.get("kind")
                        if isinstance(inner_k, int):
                            target_kind = inner_k
            except Exception:
                pass  # undecryptable content: row still listed (d + kind known)
        return DraftEntry(
            wrap_id=wrap_id,
            nevent_id=nevent_id,
            target_kind=target_kind,
            d=d,
            created_at=created_at,
        )
    except (KeyError, TypeError, ValueError):
        return None


# --- sub ids (client fan-in keys; NIP-01 legal, <= 64 chars) ---------------


def article_sub_id() -> str:
    return "longform_articles"


def comments_sub_id(article_id: str, fallback: bool = False) -> str:
    return f"longform_{'cf' if fallback else 'c'}_{article_id[:16]}"


def drafts_sub_id() -> str:
    return "longform_drafts"


# --- shared tag helpers ------------------------------------------------------


def _tag_value(tags, key) -> str:
    for t in tags:
        if isinstance(t, (list, tuple)) and len(t) >= 2 and t[0] == key:
            return str(t[1])
    return ""


def _tag_int(tags, key) -> int | None:
    v = _tag_value(tags, key)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def time_now() -> int:
    import time

    return int(time.time())
