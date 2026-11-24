"""NIPW P1 longform data layer (spec nipw2026-11-longform-v1).

Gates from the card, all deterministic:
- canonical article/comment event ids byte-exact via nostr_utils
  .compute_event_id (verified against the spec's exact events AND against
  90/90 live relay-asserted 30023 ids from nos.lol/snort/primal — the
  helper's serialization order matches NIP-01; no event-id finding here)
- ArticleCard parse (d required, title/image/summary optional, https-only
  image policy), CommentInfo parse with NIP-26 case discipline (uppercase
  root vs lowercase parent kept distinct)
- client-side comment matching (uppercase E/K/A roots AND lowercase e/a
  parents all bind an article; foreign events never match)
- edit-replacement: db.get_event_by_a newest-only on d collision (real
  Database, tier-2) + dedupe_articles list twin
- 'articles' FeedSource/backfill filter (30023-only, kind-1 never accepted)
- get_article_comments / get_self_drafts / get_feed_kind DB queries
- draft wraps: nevent decode, NIP-44 self-decrypt, deleted/expired drops
- client fetch fan-in (tier-1 ast pattern, house style from
  test_relay_usage_economy.py): primary REQ shape (#E + #A — the probe-
  corrected filter; #K-by-id is dead on khatru relays, see longform.py),
  empty-primary -> fallback REQ (authors + broad window), all-client-side
  matching, join-on-inflight, CLOSE on section close.
"""

import ast
import json
import os
import time
import types
from unittest.mock import MagicMock  # noqa: F401 - kept for fixture symmetry

import pytest

from gnostr import longform
from gnostr import nip44, nostr_utils
from gnostr.database import Database
from gnostr.service.feed_pagination import backfill_filter, make_source
from gnostr.zaps.request import verify_event_sig  # C3 publish signature proof

_HERE = os.path.dirname(os.path.abspath(__file__))
_CLIENT_PATH = os.path.normpath(
    os.path.join(_HERE, "..", "src", "gnostr", "client.py")
)

# spec vector constants (nipw2026-11-longform-v1 §test_vectors)
A_PUB = "f6408d34bc5dfb8e5f3e9ae7033450dc9d904f8359da2fae12f1fbdc96d5d812"
B_PUB = "b" * 64
ART_ID = "559359243e57a70771f35d31e3bacda0a07be855d8a467c7d5cbd67d522c5616"
COM_ID = "c283978aa351baab255058fa587e119bd8919767c8a4e011408b6597eae033d7"
ART_ADDR = f"30023:{A_PUB}:gnostr-spec-vector"

CANON_ART = {
    "id": ART_ID,
    "pubkey": A_PUB,
    "kind": 30023,
    "created_at": 1780000000,
    "tags": [
        ["d", "gnostr-spec-vector"],
        ["title", "Gnostr NIPW vector"],
        ["t", "gnostr"],
        ["summary", "deterministic test vector"],
    ],
    "content": (
        "## Heading\n\nThis paragraph must **not** hard-wrap. "
        "Inline link: [note](nostr:nevent1...)"
    ),
}
CANON_COM = {
    "id": COM_ID,
    "pubkey": A_PUB,
    "kind": 1111,
    "created_at": 1780000000,
    "tags": [
        ["K", ART_ID],
        ["k", "30023"],
        ["p", A_PUB],
    ],
    "content": "Re: Gnostr NIPW vector",
}


def _canonical_id(event):
    """House event-id helper (NIP-01 serialization, matches live relays —
    kept as a named indirection so the vector tests read as spec language)."""
    return nostr_utils.compute_event_id(event)


def _mk_comment(comment_id, created_at, tags, content="body", pubkey=B_PUB):
    return {
        "id": comment_id,
        "pubkey": pubkey,
        "kind": 1111,
        "created_at": created_at,
        "tags": tags,
        "content": content,
        "sig": "0" * 128,
    }


def _db_ready(ev):
    """save_event requires id + sig keys present (schema check, validity not
    checked — same convention as test_nostr_types.get_event_by_a tests)."""
    return {**ev, "sig": ev.get("sig", "0" * 128)}


# --- tier-2 real Database helpers ------------------------------------------


@pytest.fixture
def real_db(monkeypatch, tmp_path):
    """Real Database (tier-2, as in test_nostr_types.py): point GLib's
    data dir at tmp_path so no user data is touched."""
    from gi.repository import GLib  # conftest mock
    monkeypatch.setattr(GLib, "get_user_data_dir", lambda: str(tmp_path / "d"))
    db = Database()
    return db


# --- canonical vectors -------------------------------------------------------


class TestCanonicalVectors:
    def test_article_vector_id(self):
        assert _canonical_id(CANON_ART) == ART_ID

    def test_comment_vector_id(self):
        assert _canonical_id(CANON_COM) == COM_ID

    def test_article_card_parse(self):
        card = longform.parse_article_event(CANON_ART)
        assert card is not None
        assert card.id == ART_ID
        assert card.pubkey == A_PUB
        assert card.d == "gnostr-spec-vector"
        assert card.title == "Gnostr NIPW vector"
        assert card.summary == "deterministic test vector"
        # the spec vector carries no image/published_at tags — both optional
        assert card.image is None
        assert card.published_at is None
        assert card.created_at == 1780000000
        assert card.markdown == CANON_ART["content"]
        assert card.address == ART_ADDR

    def test_article_image_policy_https_only(self):
        bad = dict(CANON_ART)
        bad["tags"] = [
            ["d", "x"],
            ["image", "http://example.test/cover.png"],
        ]
        card = longform.parse_article_event(bad)
        assert card is not None and card.image is None
        bad["tags"] = [["d", "x"], ["image", "/relative/cover.png"]]
        assert longform.parse_article_event(bad).image is None
        bad["tags"] = [["d", "x"]]
        assert longform.parse_article_event(bad).image is None

    def test_article_d_required(self):
        nod = dict(CANON_ART)
        nod["tags"] = [["title", "no d tag"]]
        assert longform.parse_article_event(nod) is None
        wrong_kind = dict(CANON_ART, kind=1)
        assert longform.parse_article_event(wrong_kind) is None
        assert longform.parse_article_event("not-a-dict") is None
        assert longform.parse_article_event({"kind": 30023}) is None

    def test_article_feed_filter(self):
        assert longform.article_feed_filter() == {"kinds": [30023], "limit": 30}
        assert longform.article_feed_filter(10) == {"kinds": [30023], "limit": 10}


# --- comments: parsing, case discipline, matching ----------------------------


class TestCommentParsingAndMatching:
    def test_canonical_comment_info(self):
        info = longform.parse_comment_event(CANON_COM)
        assert info is not None
        assert info.id == COM_ID
        # hex-valued K (spec-vector form) binds as ROOT id
        assert info.root_id == ART_ID
        # lowercase parent pointers kept DISTINCT from roots (NIP-22 case rule)
        assert info.parent_kind == "30023"
        assert info.parent_pubkey == A_PUB
        assert info.parent_address is None
        assert info.parent_id is None
        assert info.root_address is None
        assert ART_ID in info.target_refs
        # uppercase K ≠ lowercase k: the kind string never binds as a root
        assert "30023" not in info.target_refs

    def test_live_shape_k_holds_kind(self):
        # nos.lol live 1111: K holds the parent KIND, uppercase A/E bind
        # the root article, lowercase e/a/k/p the immediate parent.
        ev = _mk_comment("c" * 64, 1750000200, [
            ["E", ART_ID],
            ["A", ART_ADDR],
            ["K", "30023"],
            ["P", A_PUB],
            ["e", "c" * 64],
            ["a", ART_ADDR],
            ["k", "30023"],
            ["p", A_PUB],
        ])
        info = longform.parse_comment_event(ev)
        assert info.root_id == ART_ID
        assert info.root_address == ART_ADDR
        assert info.root_kind == "30023"
        assert info.root_pubkey == A_PUB
        # self-parent via lowercase e (top-level under article)
        assert info.parent_id == "c" * 64
        assert info.target_refs >= {ART_ID, ART_ADDR}

    def test_match_matrix(self):
        # every binding form matches the article
        forms = [
            [["E", ART_ID]],
            [["K", ART_ID]],
            [["A", ART_ADDR]],
            [["a", ART_ADDR]],  # lowercase-only (NIP-22 top-level)
            [["e", ART_ID], ["k", "30023"]],  # lowercase parent = article id
            [["E", ART_ID], ["e", "f" * 64]],  # nested comment: root binds
        ]
        for i, tags in enumerate(forms):
            ev = _mk_comment(f"{i:0>64x}"[:64], 1750000000 + i, tags)
            assert longform.comment_matches(ev, ART_ID, ART_ADDR), tags
            assert longform.comment_matches(
                longform.parse_comment_event(ev), ART_ID, ART_ADDR
            )

    def test_foreign_events_never_match(self):
        other = _mk_comment("1" * 64, 1750000000, [["E", "d" * 64]])
        assert not longform.comment_matches(other, ART_ID, ART_ADDR)
        plain = {"id": "2" * 64, "pubkey": B_PUB, "kind": 1,
                 "created_at": 1, "tags": [["e", ART_ID]], "content": "x"}
        assert not longform.comment_matches(plain, ART_ID, ART_ADDR)
        assert not longform.comment_matches({"kind": 1111}, ART_ID, ART_ADDR)

    def test_comments_for_article_dedup_newest_first(self):
        c1 = _mk_comment("1" * 64, 100, [["E", ART_ID]])
        c2 = _mk_comment("2" * 64, 300, [["A", ART_ADDR]])
        c3 = _mk_comment("3" * 64, 200, [["e", "x" * 64], ["a", ART_ADDR]])
        foreign = _mk_comment("4" * 64, 250, [["E", "d" * 64]])
        out = longform.comments_for_article(
            [c1, c2, c3, foreign, c2], ART_ID, ART_ADDR
        )
        assert [c.id for c in out] == ["2" * 64, "3" * 64, "1" * 64]

    def test_comment_filter_shapes(self):
        primary = longform.comments_primary_filters(ART_ID, ART_ADDR)
        assert primary[0]["#E"] == [ART_ID] and primary[0]["kinds"] == [1111]
        assert primary[1]["#A"] == [ART_ADDR]
        # the probe-killed #K-by-id must NOT appear as a primary filter
        assert "#K" not in primary[0] and "#K" not in primary[1]
        fb = longform.comments_fallback_filters(A_PUB)
        assert fb[0]["authors"] == [A_PUB] and fb[0]["kinds"] == [1111]
        assert "authors" not in fb[1] and fb[1]["kinds"] == [1111]

    def test_sub_ids_are_nip01_legal(self):
        for s in (
            longform.article_sub_id(),
            longform.comments_sub_id(ART_ID),
            longform.comments_sub_id(ART_ID, fallback=True),
            longform.drafts_sub_id(),
        ):
            assert 0 < len(s) <= 64
            assert s == s.strip() and " " not in s


# --- edit-replacement (get_event_by_a + dedupe_articles) --------------------


class TestEditReplacement:
    def test_get_event_by_a_newest_only(self, real_db):
        art1 = dict(CANON_ART)
        art2 = dict(art1, created_at=1780000500, content="v2", id="e" * 64)
        art2["id"] = _canonical_id(art2)
        other = dict(art1, tags=[["d", "other-slug"]], id="f" * 64)
        other["id"] = _canonical_id(other)
        real_db.save_event(_db_ready(art1))
        real_db.save_event(_db_ready(art2))
        real_db.save_event(_db_ready(other))
        newest = real_db.get_event_by_a(30023, A_PUB, "gnostr-spec-vector")
        assert newest is not None and newest["id"] == art2["id"]
        # independent d unaffected
        assert real_db.get_event_by_a(30023, A_PUB, "other-slug")["id"] == other["id"]

    def test_dedupe_articles_newest_only(self):
        base = {k: v for k, v in CANON_ART.items() if k != "id"}
        art1 = longform.parse_article_event(base)
        art2 = longform.parse_article_event(
            dict(base, created_at=1780000500, content="v2", id="e" * 64)
        )
        out = longform.dedupe_articles([art1, art2])
        assert [c.id for c in out] == [art2.id]
        both_d = longform.dedupe_articles(
            [art1, art2, longform.parse_article_event(
                dict(base, tags=[["d", "other"]], id="f" * 64))]
        )
        assert len(both_d) == 2
        assert both_d[0].id == art2.id  # newest-first ordering


# --- articles FeedSource + DB kind query -------------------------------------


class TestArticlesFeed:
    def test_source_pages_30023_only(self, real_db):
        art = dict(CANON_ART)
        post = {"id": "9" * 64, "pubkey": A_PUB, "kind": 1,
                "created_at": 1750000600, "tags": [], "content": "feed post"}
        real_db.save_event(_db_ready(art))
        real_db.save_event(_db_ready(post))
        src = make_source("articles", real_db, None)
        rows = src.page(50)
        assert [r["kind"] for r in rows] == [30023]
        assert src.accepts(CANON_ART) and not src.accepts(post)

    def test_backfill_filter_is_30023_scoped(self):
        filt = backfill_filter("articles", None, (1750000000, ART_ID), 20,
                               MagicMock())
        assert filt == {"kinds": [30023], "until": 1750000000, "limit": 20}
        # the kind-1 base never leaks into an article backfill
        assert 1 not in filt["kinds"]

    def test_get_feed_kind_keyset_pages(self, real_db):
        for i in range(5):
            real_db.save_event({
                "id": f"{i:0>64x}"[:64], "pubkey": A_PUB, "kind": 30023,
                "created_at": 1000 + i, "tags": [["d", f"slug-{i}"]],
                "content": "x", "sig": "0" * 128,
            })
        page1 = real_db.get_feed_kind(30023, limit=3)
        assert [e["created_at"] for e in page1] == [1004, 1003, 1002]
        page2 = real_db.get_feed_kind(
            30023, limit=3, before=(1002, page1[-1]["id"])
        )
        assert [e["created_at"] for e in page2] == [1001, 1000]
        # kind isolation: kind-1 rows never appear
        real_db.save_event({"id": "a1" + "0" * 62, "pubkey": A_PUB, "kind": 1,
                            "created_at": 9999, "tags": [], "content": "y",
                            "sig": "0" * 128})
        assert all(e["kind"] == 30023
                   for e in real_db.get_feed_kind(30023, limit=10))


# --- comment/draft DB queries -------------------------------------------------


class TestLongformQueries:
    def test_get_article_comments_precise(self, real_db):
        matching = [
            _mk_comment(COM_ID, 1750000100, CANON_COM["tags"]),
            _mk_comment("c1" + "0" * 62, 1750000150, [["A", ART_ADDR]]),
        ]
        foreign = _mk_comment("c2" + "0" * 62, 1750000160, [["E", "d" * 64]])
        noise = {"id": "c3" + "0" * 62, "pubkey": B_PUB, "kind": 1,
                 "created_at": 1750000170, "tags": [], "content": "kind1",
                 "sig": "0" * 128}
        for ev in matching + [foreign, noise]:
            real_db.save_event(ev)
        rows = real_db.get_article_comments(ART_ID, ART_ADDR)
        assert {r["id"] for r in rows} == {COM_ID, "c1" + "0" * 62}
        assert [r["created_at"] for r in rows] == [1750000150, 1750000100]

    def test_get_article_comments_like_does_not_cut_before_filter(self, real_db):
        # house rule (get_replies): the LIKE narrows BEFORE LIMIT so a new
        # unrelated 1111 in the window cannot push our oldest match out
        newest_noise = _mk_comment("c4" + "0" * 62, 1750009999, [["E", "d" * 64]])
        old_match = _mk_comment(COM_ID, 1750000100, CANON_COM["tags"])
        real_db.save_event(newest_noise)
        real_db.save_event(old_match)
        rows = real_db.get_article_comments(ART_ID, ART_ADDR, limit=1)
        assert [r["id"] for r in rows] == [COM_ID]

    def test_get_self_drafts_author_scoped(self, real_db):
        mine = {"id": "d1" + "0" * 62, "pubkey": A_PUB, "kind": 31234,
                "created_at": 100, "tags": [["k", "23"], ["d", "post-1"]],
                "content": "nevent-something", "sig": "0" * 128}
        theirs = {"id": "d2" + "0" * 62, "pubkey": B_PUB, "kind": 31234,
                  "created_at": 200, "tags": [["k", "23"], ["d", "x"]],
                  "content": "other", "sig": "0" * 128}
        real_db.save_event(mine)
        real_db.save_event(theirs)
        rows = real_db.get_self_drafts(A_PUB)
        assert [r["id"] for r in rows] == ["d1" + "0" * 62]


# --- draft wrap parsing --------------------------------------------------------


def _encode_nevent(event_id_hex, relays=(), author_hex=None, kind=None):
    """NIP-19 nevent: byte TLV (type,len,value…) then 8->5 bech32 — mirrors
    nostr_utils.decode_nevent_full's _decode_tlv framing."""
    tlv = bytearray()
    idb = bytes.fromhex(event_id_hex)
    tlv += bytes([0, len(idb)]) + idb
    for r in relays:
        rb = r.encode("utf-8")
        tlv += bytes([1, len(rb)]) + rb
    if author_hex:
        ab = bytes.fromhex(author_hex)
        tlv += bytes([2, len(ab)]) + ab
    if kind is not None:
        kb = kind.to_bytes(4, "big")
        tlv += bytes([3, len(kb)]) + kb
    data = nostr_utils.convertbits(bytes(tlv), 8, 5)
    # bech32_encode appends the checksum itself (same shape as
    # groups.encode_naddr) — do NOT add one manually.
    return nostr_utils.bech32_encode("nevent", data)


class TestDraftParsing:
    WRAP_ID = "d" * 64

    def _wrap(self, content, tags=None, created_at=1750000000, pubkey=A_PUB):
        return {
            "id": self.WRAP_ID,
            "pubkey": pubkey,
            "kind": 31234,
            "created_at": created_at,
            "tags": tags if tags is not None else [["k", "23"], ["d", "draft-1"]],
            "content": content,
        }

    def test_nevent_pointer(self):
        nevent = _encode_nevent("e" * 64, relays=("wss://relay.example",),
                                author_hex=A_PUB, kind=23)
        assert nostr_utils.decode_nevent_full(nevent)[0] == "e" * 64
        entry = longform.parse_draft_event(self._wrap(nevent))
        assert entry is not None
        assert entry.wrap_id == self.WRAP_ID
        assert entry.nevent_id == "e" * 64
        assert entry.target_kind == 23
        assert entry.d == "draft-1"

    def test_nip44_self_decrypt(self):
        priv = "11" * 32
        pub = nostr_utils.get_public_key(priv)
        draft = {
            "pubkey": pub, "kind": 23, "created_at": 1750000500,
            "content": "secret draft body",
            "tags": [["d", "inner-d"], ["title", "Private post"]],
        }
        draft["id"] = _canonical_id(draft)
        conv = nip44.get_conversation_key(bytes.fromhex(priv), bytes.fromhex(pub))
        payload = nip44.encrypt(json.dumps(draft), conv)
        wrap = self._wrap(payload, pubkey=pub,
                          tags=[["k", "23"], ["d", "wrap-d"]])
        entry = longform.parse_draft_event(
            wrap, privkey_hex=priv, self_pubkey_hex=pub
        )
        assert entry is not None
        assert entry.nevent_id == draft["id"]
        # inner d preferred over wrap d
        assert entry.d == "inner-d"
        assert entry.target_kind == 23

    def test_undecryptable_still_lists(self):
        entry = longform.parse_draft_event(self._wrap("bm90LWEtbmV2ZW50"))
        assert entry is not None and entry.nevent_id is None
        assert entry.d == "draft-1" and entry.target_kind == 23

    def test_deleted_and_expired_dropped(self):
        assert longform.parse_draft_event(self._wrap("")) is None
        assert longform.parse_draft_event(self._wrap(None)) is None
        expired = self._wrap(
            "whatever", tags=[["k", "23"], ["d", "x"],
                              ["expiration", str(int(time.time()) - 10)]]
        )
        assert longform.parse_draft_event(expired) is None

    def test_drafts_filter_shape(self):
        # C4 (card t_deee2a81): legacy 30024 joined the REQ so relays that
        # carry the user's old plaintext drafts answer; rows get the
        # 'legacy' label (probe found none in practice: 31234 dominates).
        assert longform.drafts_filter(A_PUB) == {
            "kinds": [31234, 30024], "authors": [A_PUB], "limit": 100
        }
        assert longform.KIND_DRAFT_LEGACY == 30024

    # --- C4 (card t_deee2a81): display payload + publish/delete builders ---

    def test_decrypt_populates_title_body_words_tags(self):
        priv = "11" * 32
        pub = nostr_utils.get_public_key(priv)
        draft = {
            "pubkey": pub, "kind": 23, "created_at": 1750000500,
            "content": "alpha beta gamma delta",
            "tags": [["d", "inner-d"], ["title", "Private post"],
                     ["t", "linux"]],
        }
        draft["id"] = _canonical_id(draft)
        conv = nip44.get_conversation_key(bytes.fromhex(priv), bytes.fromhex(pub))
        payload = nip44.encrypt(json.dumps(draft), conv)
        wrap = self._wrap(payload, pubkey=pub,
                          tags=[["k", "23"], ["d", "wrap-d"]])
        entry = longform.parse_draft_event(
            wrap, privkey_hex=priv, self_pubkey_hex=pub
        )
        assert entry is not None
        assert entry.title == "Private post"
        assert entry.content == "alpha beta gamma delta"
        assert entry.word_count == 4
        assert entry.legacy is False
        # draft tags ride through MINUS the d/title the rebuild re-adds itself
        assert entry.extra_tags == [["t", "linux"]]

    def test_legacy_30024_parses_labeled(self):
        ev = {
            "id": "c" * 64, "pubkey": A_PUB, "kind": 30024,
            "created_at": 1740000000,
            "tags": [["d", "old"], ["title", "Old boy"], ["t", "legacy"]],
            "content": "one two three four",
        }
        entry = longform.parse_draft_event(ev)
        assert entry is not None
        assert entry.legacy is True
        assert entry.target_kind == 30024
        assert entry.nevent_id == "c" * 64  # the draft IS its own event
        assert entry.title == "Old boy"
        assert entry.content == "one two three four"
        assert entry.word_count == 4
        assert entry.extra_tags == [["t", "legacy"]]
        # NIP-37 empty-content deletion signal is WRAP-ONLY semantics:
        # a legacy 30024 stays a row (0 words).
        empty = dict(ev, content="")
        kept = longform.parse_draft_event(empty)
        assert kept is not None and kept.word_count == 0

    def test_build_article_from_draft_unsigned_shape(self):
        priv = "11" * 32
        pub = nostr_utils.get_public_key(priv)
        draft = {
            "pubkey": pub, "kind": 23, "created_at": 1750000500,
            "content": "the real body words",
            "tags": [["d", "my-post"], ["title", "My Draft"],
                     ["t", "nostr"], ["t", "linux"]],
        }
        draft["id"] = _canonical_id(draft)
        conv = nip44.get_conversation_key(bytes.fromhex(priv), bytes.fromhex(pub))
        wrap = self._wrap(nip44.encrypt(json.dumps(draft), conv), pubkey=pub,
                          tags=[["k", "23"], ["d", "wrap-d"]])
        entry = longform.parse_draft_event(
            wrap, privkey_hex=priv, self_pubkey_hex=pub
        )
        tmpl = longform.build_article_from_draft(entry, pub)
        assert tmpl is not None
        assert tmpl["kind"] == 30023
        assert tmpl["pubkey"] == pub
        assert tmpl["content"] == "the real body words"
        tag_map = {t[0]: t[1] for t in tmpl["tags"]}
        assert tag_map["d"] == "my-post"  # inner d wins (same preference as row)
        assert tag_map["title"] == "My Draft"
        assert int(tag_map["published_at"]) >= int(time.time()) - 5
        # draft's own t tags preserved (card: publish builds 30023 WITH tags)
        assert [t for t in tmpl["tags"] if t[0] == "t"] == [
            ["t", "nostr"], ["t", "linux"]]
        # unsigned-then-client-signs boundary (C3 convention): template has
        # NO id/sig; canonical id still computes over it.
        assert "id" not in tmpl and "sig" not in tmpl
        nostr_utils.compute_event_id(tmpl)

    def test_build_article_d_generated_and_blocked_cases(self):
        entry = longform.DraftEntry(
            wrap_id="a" * 64, nevent_id=None, target_kind=23, d=None,
            created_at=1, content="words words", word_count=2,
        )
        tmpl = longform.build_article_from_draft(entry, A_PUB)
        assert tmpl is not None and tmpl["tags"][0] == ["d", "draft-aaaaaaaa"]
        # deterministic across calls (republish keeps the same address)
        assert longform.build_article_from_draft(entry, A_PUB)["tags"][0] == \
            tmpl["tags"][0]
        # unreadable body (nevent-only / undecryptable wrap) -> not publishable
        blank = longform.DraftEntry(
            wrap_id="b" * 64, nevent_id="e" * 64, target_kind=23, d=None,
            created_at=1, content="",
        )
        assert longform.build_article_from_draft(blank, A_PUB) is None
        assert longform.build_article_from_draft(entry, "") is None

    def test_build_draft_deletion_unsigned_targets_wrap(self):
        ev = longform.build_draft_deletion("a" * 64, A_PUB)
        assert ev is not None
        assert ev["kind"] == 5
        assert ev["pubkey"] == A_PUB
        assert ev["content"] == ""
        assert ev["tags"] == [["e", "a" * 64]]
        assert "id" not in ev and "sig" not in ev
        nostr_utils.compute_event_id(ev)  # signable canonical form
        # client-side call with no pubkey yet: _sign fills it in (C3 funnel)
        bare = longform.build_draft_deletion("a" * 64)
        assert bare is not None and "pubkey" not in bare
        assert longform.build_draft_deletion("") is None
        assert longform.build_draft_deletion("nothex") is None

    def test_draft_card_view_model(self):
        entry = longform.DraftEntry(
            wrap_id="a" * 64, nevent_id="n" * 64, target_kind=23, d="my-post",
            created_at=1750000000, title="My Draft", content="body text",
            word_count=2,
        )
        card = longform.draft_card(entry, A_PUB)
        assert card.id == "a" * 64  # wrap id keys the synthetic card
        assert card.pubkey == A_PUB
        assert card.d == "my-post"
        assert card.title == "My Draft"
        assert card.markdown == "body text"
        assert card.address == f"30023:{A_PUB}:my-post"


# --- client fetch fan-in (tier-1 ast pattern) ---------------------------------


def _client_ns():
    src = open(_CLIENT_PATH, encoding="utf-8").read()
    ns = {"__name__": "gnostr.client", "__package__": "gnostr"}
    exec(compile(ast.parse(src), _CLIENT_PATH, "exec"), ns)
    ns["GLib"] = types.SimpleNamespace(
        idle_add=lambda fn, *a: fn(*a),
        timeout_add=lambda ms, fn, *a: None,
        timeout_add_seconds=lambda s, fn, *a: 1,
        source_remove=lambda t: None,
        get_user_config_dir=lambda: "/tmp/gnostr-test",
    )
    return ns


_NS = _client_ns()


def _bound(method_name):
    cls = next(
        n
        for n in ast.parse(open(_CLIENT_PATH, encoding="utf-8").read()).body
        if isinstance(n, ast.ClassDef) and n.name == "NostrClient"
    )
    fns = [
        n for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == method_name
    ]
    if not fns:
        raise AssertionError(f"method {method_name} not found in NostrClient")
    ns = dict(_NS)
    exec(compile(ast.Module(body=fns, type_ignores=[]), _CLIENT_PATH, "exec"), ns)
    return ns[method_name]


class _FakeRecRelay:
    """Records every subscribe/request_once/close_sub the client makes."""

    def __init__(self, url):
        self.url = url
        self.subs = []       # (sub_id, filters, snapshot, exclusive)
        self.reqs = []       # (sub_id, filters)
        self.closes = []
        self.published = []  # events handed to relay.publish

    def subscribe(self, sub_id, filters, snapshot=False, exclusive=True):
        self.subs.append((sub_id, filters, snapshot, exclusive))

    def request_once(self, sub_id, filters):
        self.reqs.append((sub_id, filters))

    def publish(self, event):
        self.published.append(event)
        return True  # real RelayClient.publish returns bool ack accepted

    def close_sub(self, sub_id):
        self.closes.append(sub_id)


class FakeLongformClient:
    """Fake self carrying the real longform fan-in methods (house tier-1
    pattern — conftest's gi Mock makes the real class subclassable only
    under a real GObject, so methods are ast-extracted and bound here)."""

    def __init__(self, n_relays=2):
        urls = [f"wss://r{i}.example" for i in range(n_relays)]
        self.active_relays = {u: _FakeRecRelay(u) for u in urls}
        self._longform_fetches = {}
        self._longform_section_subs = set()
        self._longform_article_relays = {}  # C3 attribution store
        self._longform_draft_relays = {}    # C4 attribution store (wrap -> urls)
        self.my_keys = {"pubkey": A_PUB, "privatekey": "11" * 32}
        self.closed = []

    # Real NostrClient methods, ast-extracted and attached as CLASS
    # attributes so Python's descriptor rule binds `self` to FakeLongformClient
    # (house pattern from test_relay_usage_economy.py — instance attributes
    # would NOT bind self and the calls lose a positional arg).
    request_once = _bound("request_once")
    subscribe = _bound("subscribe")
    open_article_section = _bound("open_article_section")
    close_article_section = _bound("close_article_section")
    fetch_article_comments = _bound("fetch_article_comments")
    fetch_drafts = _bound("fetch_drafts")
    _longform_note = _bound("_longform_note")
    _longform_eose = _bound("_longform_eose")
    _settle_longform_fetch = _bound("_settle_longform_fetch")
    _arm_longform_timeout = _bound("_arm_longform_timeout")
    _longform_timeout = _bound("_longform_timeout")
    # C3 (card): relay attribution + comment publish path
    _note_article_relay = _bound("_note_article_relay")
    comment_target_relays = _bound("comment_target_relays")

    def _read_relays(self):
        return dict(self.active_relays)

    def close_sub(self, sub_id):
        self.closed.append(sub_id)

    def all_reqs(self):
        out = []
        for r in self.active_relays.values():
            out += r.reqs
        return out

    def eose_all(self, sub_id):
        for url in self.active_relays:
            self._longform_eose(sub_id, url)

    def reqs_for(self, sub_id):
        return [f for (_, f) in self.all_reqs() if _ == sub_id]


class TestClientArticleSection:
    def test_open_uses_optin_filter_and_exclusive_false(self):
        c = FakeLongformClient()
        c.open_article_section()
        for r in c.active_relays.values():
            assert r.subs == [
                ("longform_articles", {"kinds": [30023], "limit": 30},
                 False, False)
            ]

    def test_close_closes_recorded_sub(self):
        c = FakeLongformClient()
        c.open_article_section(limit=12)
        c.close_article_section()
        # production shape is per-relay r.close_sub (NostrClient has no
        # client-wide close_sub) — every read relay that opened it closes.
        for r in c.active_relays.values():
            assert r.closes == ["longform_articles"]
        assert c._longform_section_subs == set()


class TestClientCommentFetch:
    def _fetch(self, c, calls):
        c.fetch_article_comments(CANON_ART, lambda evs, meta: calls.append((evs, meta)))
        return c.reqs_for("longform_c_" + ART_ID[:16])[0]

    def test_primary_req_shape(self):
        c = FakeLongformClient()
        calls = []
        filt = self._fetch(c, calls)
        assert filt[0]["#E"] == [ART_ID]
        assert filt[1]["#A"] == [ART_ADDR]
        assert all(f["kinds"] == [1111] for f in filt)

    def test_settles_on_eose_fanin_with_client_matched_events(self):
        c = FakeLongformClient()
        calls = []
        self._fetch(c, calls)
        sub_id = "longform_c_" + ART_ID[:16]
        # a relay ignores #E filtering and returns junk + our match:
        # client-side matching must keep only the bound comment.
        c._longform_note(CANON_COM)
        c._longform_note(_mk_comment("5" * 64, 999, [["E", "d" * 64]]))
        c.eose_all(sub_id)
        assert len(calls) == 1
        evs, meta = calls[0]
        assert [e["id"] for e in evs] == [COM_ID]
        assert meta == {"fallback_used": False}
        # no fallback REQ was ever sent (primary produced a match)
        assert not any(f == "longform_cf_" + ART_ID[:16]
                       for (f, _) in c.all_reqs())

    def test_empty_primary_fires_fallback_req_then_settles(self):
        c = FakeLongformClient()
        calls = []
        self._fetch(c, calls)
        c.eose_all("longform_c_" + ART_ID[:16])
        fb_sub = "longform_cf_" + ART_ID[:16]
        fb = c.reqs_for(fb_sub)[0]
        assert fb[0]["authors"] == [A_PUB]
        assert "authors" not in fb[1]
        # fallback-phase comment recovered purely client-side
        c._longform_note(CANON_COM)
        c.eose_all(fb_sub)
        assert len(calls) == 1
        evs, meta = calls[0]
        assert [e["id"] for e in evs] == [COM_ID]
        assert meta == {"fallback_used": True}

    def test_stale_primary_eose_ignored_after_fallback(self):
        c = FakeLongformClient()
        calls = []
        self._fetch(c, calls)
        c.eose_all("longform_c_" + ART_ID[:16])  # -> fallback armed
        c._longform_eose("longform_c_" + ART_ID[:16], "wss://late.example")
        assert not calls  # old-phase EOSE must not settle

    def test_concurrent_fetch_joins_no_second_req(self):
        c = FakeLongformClient()
        calls = []
        self._fetch(c, calls)
        c.fetch_article_comments(
            CANON_ART, lambda evs, meta: calls.append((evs, meta))
        )
        assert len(c.all_reqs()) == 2  # 2 relays x 1 REQ only
        c._longform_note(CANON_COM)
        c.eose_all("longform_c_" + ART_ID[:16])
        assert len(calls) == 2
        assert all(meta["fallback_used"] is False for (_, meta) in calls)

    def test_duplicate_note_captured_once(self):
        c = FakeLongformClient()
        calls = []
        self._fetch(c, calls)
        c._longform_note(CANON_COM)
        c._longform_note(CANON_COM)
        c.eose_all("longform_c_" + ART_ID[:16])
        assert [e["id"] for e in calls[0][0]] == [COM_ID]

    def test_article_card_object_accepted(self):
        c = FakeLongformClient()
        calls = []
        card = longform.parse_article_event(CANON_ART)
        c.fetch_article_comments(card, lambda evs, meta: calls.append((evs, meta)))
        filt = c.reqs_for("longform_c_" + ART_ID[:16])[0]
        assert filt[1]["#A"] == [ART_ADDR]

    def test_malformed_article_settles_empty(self):
        c = FakeLongformClient()
        calls = []
        c.fetch_article_comments({"kind": 30023},
                                 lambda evs, meta: calls.append((evs, meta)))
        assert calls == [([], {"fallback_used": False})]

    def test_timeout_settles_when_relays_silent(self):
        c = FakeLongformClient()
        calls = []
        self._fetch(c, calls)
        c._longform_timeout("longform_c_" + ART_ID[:16])
        # primary empty -> fallback REQ armed, fetch not settled yet
        assert not calls
        c._longform_timeout("longform_cf_" + ART_ID[:16])
        assert calls and calls[0][1] == {"fallback_used": True}


class TestClientDraftFetch:
    def test_req_shape_and_author_filtering(self):
        c = FakeLongformClient()
        calls = []
        c.fetch_drafts(lambda rows, meta: calls.append((rows, meta)))
        filt = c.reqs_for("longform_drafts")[0]
        # C4: legacy 30024 joined the REQ (labelled legacy rows when found)
        assert filt == {"kinds": [31234, 30024], "authors": [A_PUB],
                        "limit": 100}
        mine = {"id": "d1" + "0" * 62, "pubkey": A_PUB, "kind": 31234,
                "created_at": 100, "tags": [["k", "23"], ["d", "mine"]],
                "content": "x"}
        theirs = {"id": "d2" + "0" * 62, "pubkey": B_PUB, "kind": 31234,
                  "created_at": 200, "tags": [["k", "23"], ["d", "x"]],
                  "content": "y"}
        c._longform_note(mine)
        c._longform_note(theirs)  # relay misbehavior: never trust authors
        c.eose_all("longform_drafts")
        rows, meta = calls[0]
        assert [r.d for r in rows] == ["mine"]
        assert rows[0].target_kind == 23

    def test_no_keys_settles_immediately(self):
        c = FakeLongformClient()
        c.my_keys = {}
        calls = []
        c.fetch_drafts(lambda rows, meta: calls.append((rows, meta)))
        assert calls == [([], {"settled_by": "no-key"})]
        assert c.all_reqs() == []

    def test_nip44_wrap_decrypted_at_settle(self):
        c = FakeLongformClient()
        priv = "11" * 32
        pub = nostr_utils.get_public_key(priv)
        c.my_keys = {"pubkey": pub, "privatekey": priv}
        draft = {"pubkey": pub, "kind": 23, "created_at": 1750000500,
                 "content": "body", "tags": [["d", "inner"]]}
        draft["id"] = _canonical_id(draft)
        conv = nip44.get_conversation_key(bytes.fromhex(priv), bytes.fromhex(pub))
        wrap = {"id": "d3" + "0" * 62, "pubkey": pub, "kind": 31234,
                "created_at": 100, "tags": [["k", "23"], ["d", "wrap"]],
                "content": nip44.encrypt(json.dumps(draft), conv)}
        calls = []
        c.fetch_drafts(lambda rows, meta: calls.append((rows, meta)))
        c._longform_note(wrap)
        c.eose_all("longform_drafts")
        rows, _meta = calls[0]
        assert rows[0].nevent_id == draft["id"] and rows[0].d == "inner"


# ---------------------------------------------------------------------------
# Live gate (card): "30 live 30023 events from nos.lol, >=90% parse without
# crash". Deterministic CI never touches the wire; run with GNOSTR_LIVE=1 to
# exercise it (same pattern family as the relay-economy live audits).
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    os.environ.get("GNOSTR_LIVE") != "1",
    reason="live nos.lol parse gate; enable with GNOSTR_LIVE=1",
)
class TestLiveNosLolParse:
    def test_thirty_live_articles_parse(self):
        import asyncio

        import websockets

        async def _collect():
            events = []
            async with websockets.connect(
                "wss://nos.lol", open_timeout=10, close_timeout=5
            ) as ws:
                await ws.send(json.dumps(
                    ["REQ", "lf_live", {"kinds": [30023], "limit": 30}]
                ))
                deadline = asyncio.get_event_loop().time() + 20
                while len(events) < 30:
                    left = deadline - asyncio.get_event_loop().time()
                    if left <= 0:
                        break
                    try:
                        msg = json.loads(
                            await asyncio.wait_for(ws.recv(), timeout=left)
                        )
                    except (asyncio.TimeoutError, Exception):
                        break
                    if msg[0] == "EVENT" and msg[1] == "lf_live":
                        events.append(msg[2])
                    elif msg[0] == "EOSE" and msg[1] == "lf_live":
                        break
            return events

        events = asyncio.run(_collect())
        if len(events) < 30:
            pytest.skip(f"nos.lol returned only {len(events)} 30023 events")
        ok = sum(
            1
            for ev in events
            if longform.parse_article_event(ev) is not None
        )
        assert ok >= 27, f"live parse rate {ok}/30 below 90%"


# ---------------------------------------------------------------------------
# C3 (card t_54e093f7): relay attribution + signed-then-gated comment publish
# ---------------------------------------------------------------------------

COMMENTER_PRIV = "11" * 32
COMMENTER_PUB = nostr_utils.get_public_key(COMMENTER_PRIV)
DRAFTER_PRIV = "22" * 32
DRAFTER_PUB = nostr_utils.get_public_key(DRAFTER_PRIV)


def _comment_template():
    # the commenter signs with their own key; the article author is A_PUB
    return longform.build_comment_event(
        COMMENTER_PUB, CANON_ART, "nice writeup", created_at=1700000001
    )


class TestCommentRelayAttribution:
    def test_note_article_relay_records_host(self):
        c = FakeLongformClient()
        c._note_article_relay(ART_ID, "wss://host.example")
        c._note_article_relay(ART_ID, "wss://host.example")  # idempotent
        assert c._longform_article_relays[ART_ID] == {"wss://host.example"}

    def test_targets_union_known_with_defaults(self):
        c = FakeLongformClient()
        c._note_article_relay(ART_ID, "wss://host.example")
        # no _write_relays/relay_urls on the bare fake -> known relays only
        assert c.comment_target_relays(ART_ID) == {"wss://host.example"}
        assert c.comment_target_relays("unknown-article") == set()
        # user-configured default publish set participates too
        c.relay_urls = {"wss://u.example"}
        assert c.comment_target_relays(ART_ID) == {
            "wss://host.example", "wss://u.example",
        }
        assert c.comment_target_relays("unknown-article") == {"wss://u.example"}

    def test_answered_relays_accumulate_on_settle(self):
        c = FakeLongformClient()
        calls = []
        c.fetch_article_comments(
            CANON_ART, lambda evs, meta: calls.append((evs, meta))
        )
        c._longform_note(CANON_COM, "wss://r1.example")
        c.eose_all("longform_c_" + ART_ID[:16])
        assert len(calls) == 1
        assert c._longform_article_relays[ART_ID] == {
            "wss://r0.example", "wss://r1.example",
        }

    def test_seen_survives_the_fallback_phase(self):
        c = FakeLongformClient()
        calls = []
        c.fetch_article_comments(
            CANON_ART, lambda evs, meta: calls.append((evs, meta))
        )
        c.eose_all("longform_c_" + ART_ID[:16])  # primary empty -> fallback
        c._longform_note(CANON_COM, "wss://r0.example")
        c.eose_all("longform_cf_" + ART_ID[:16])
        evs, meta = calls[0]
        assert meta["fallback_used"] is True
        # attribution spans BOTH phases: every relay that answered or
        # produced a match participates in the comment channel
        assert c._longform_article_relays[ART_ID] == {
            "wss://r0.example", "wss://r1.example",
        }


class _FakeBunkerSigner:
    """Non-local backend: sign_event round-trips (real Bunker signs on a
    worker thread + user approval; here it just signs with the priv key)."""

    def __init__(self, pubkeys, priv):
        self.is_local = False
        self.local_provider = None
        self.pubkeys = set(pubkeys)
        self.priv = priv
        self.calls = 0

    def sign_event(self, event):
        if event.get("pubkey") not in self.pubkeys:
            raise ValueError("pubkey not in bunker key set")
        self.calls += 1
        return nostr_utils.sign_event(event, self.priv)


class TestArticleCommentPublish:
    """publish_article_comment fan: sign ONCE (local inline / bunker on
    worker), publish to known ∪ write defaults, one _track_publish entry."""

    def _client(self, known=("wss://known.example",), priv=COMMENTER_PRIV):
        c = FakeLongformClient()
        c._longform_article_relays = {ART_ID: set(known)}
        c.my_privkey = priv
        c.signer = None
        c.tracked = []
        c.new_conns = []

        def add_relay_connection(url):
            c.new_conns.append(url)
            c.active_relays.setdefault(url, _FakeRecRelay(url))

        c.add_relay_connection = add_relay_connection
        c._write_relays = lambda: {"wss://w.example": None}

        def _track_publish(event, label, sent_urls=None, candidates=None):
            c.tracked.append(
                (event, label, set(sent_urls or ()), set(candidates or ()))
            )

        c._track_publish = _track_publish
        c._log_relay = lambda msg: None
        # instance-bound real methods (FakeLongformClient binds via class
        # descriptors; these are attached per-instance with MethodType)
        for m in ("publish_article_comment", "_publish_comment_targets",
                  "_sign"):
            setattr(c, m, types.MethodType(_bound(m), c))
        return c

    def test_local_signer_signs_once_and_fans_targets(self):
        c = self._client()
        template = _comment_template()
        assert c.publish_article_comment(template, ART_ID) is True
        # known hosting ∪ configured write defaults; plain read relays that
        # never carried THIS article are deliberately NOT sprayed
        targets = {"wss://known.example", "wss://w.example"}
        signed_events = []
        for url in targets:
            r = c.active_relays[url]
            assert len(r.published) == 1
            signed_events.append(r.published[0])
        assert c.active_relays["wss://r0.example"].published == []
        assert c.active_relays["wss://r1.example"].published == []
        # every fan copy is THE SAME signed event (signed exactly once)
        ids = {e["id"] for e in signed_events}
        assert ids == {nostr_utils.compute_event_id(template)}
        signed = signed_events[0]
        assert signed["sig"] and verify_event_sig(signed)
        assert signed["kind"] == 1111
        # one track entry covering the full candidate set
        assert len(c.tracked) == 1
        ev, label, sent, candidates = c.tracked[0]
        assert label == "Article comment"
        assert candidates == targets and sent == targets
        # known relay wasn't connected yet -> on-demand connect happened
        assert "wss://known.example" in c.new_conns
        # sign_event fills the template in place (house convention); the
        # optimistic-row key must equal the signed id (dedup invariant)
        assert "sig" in template
        assert template["id"] == signed["id"]
        assert nostr_utils.compute_event_id(
            {k: v for k, v in template.items() if k not in ("id", "sig")}
        ) == signed["id"]

    def test_bunker_backend_signs_once_on_worker(self):
        c = self._client()
        bunker = _FakeBunkerSigner({COMMENTER_PUB}, COMMENTER_PRIV)
        c.signer = bunker
        c.my_privkey = None  # local fallback must NOT be used
        template = _comment_template()
        t0 = time.time()
        assert c.publish_article_comment(template, ART_ID) is True
        assert time.time() - t0 < 1.0  # returns immediately (worker signs)
        deadline = time.time() + 5.0
        while time.time() < deadline and not c.tracked:
            time.sleep(0.05)
        assert bunker.calls == 1
        assert len(c.tracked) == 1
        published = [e for r in c.active_relays.values() for e in r.published]
        # same target set as the local path: known ∪ write defaults
        assert len(published) == 2 and {e["id"] for e in published} == \
            {nostr_utils.compute_event_id(template)}
        assert all(verify_event_sig(e) for e in published)

    def test_local_sign_decline_returns_false(self):
        c = self._client()
        c.my_privkey = None
        template = _comment_template()
        assert c.publish_article_comment(template, ART_ID) is False
        assert not c.tracked
        assert not any(r.published for r in c.active_relays.values())

    def test_no_targets_returns_false(self):
        c = self._client(known=())
        c._write_relays = lambda: {}
        c.active_relays = {}
        assert c.comment_target_relays(ART_ID) == set()
        assert c.publish_article_comment(_comment_template(), ART_ID) is False

    def test_build_comment_event_shape_and_tags(self):
        ev = _comment_template()
        assert ev["kind"] == 1111
        assert ev["pubkey"] == COMMENTER_PUB
        assert ev["content"] == "nice writeup"
        assert ev["created_at"] == 1700000001
        assert "id" not in ev and "sig" not in ev  # template, unsigned
        tags = {(t[0], t[1]) for t in ev["tags"]}
        assert ("e", ART_ID) in tags and ("E", ART_ID) in tags
        assert ("a", ART_ADDR) in tags and ("A", ART_ADDR) in tags
        assert ("p", A_PUB) in tags and ("P", A_PUB) in tags
        assert ("k", "30023") in tags and ("K", "30023") in tags
        kinds = [t[1] for t in ev["tags"] if t[0] in ("k", "K")]
        assert kinds == ["30023", "30023"]  # exactly one lowercase + one root
        # round-trip through the real client signing funnel -> parser sees a
        # comment that binds the article both ways (NIP-26 case discipline)
        signed = nostr_utils.sign_event(ev, COMMENTER_PRIV)
        assert verify_event_sig(signed)
        assert longform.comment_matches(signed, ART_ID, ART_ADDR)
        assert signed["id"] == nostr_utils.compute_event_id(ev)

    def test_build_comment_event_rejects_unbuildable_article(self):
        assert longform.build_comment_event(
            A_PUB, {"kind": 30023}, "x") is None
        assert longform.build_comment_event("", CANON_ART, "x") is None


# ---------------------------------------------------------------------------
# C4 (card t_deee2a81): draft attribution + publish/delete client flows
# ---------------------------------------------------------------------------

DRAFT_WRAP_ID = "a" * 64


def _c4_draft_entry():
    """Real decryptable draft: nip44 wrap (inner 23-style draft w/ title +
    tags) parsed with the owner key — the exact row the DraftsPage renders."""
    priv = DRAFTER_PRIV
    pub = DRAFTER_PUB
    draft = {
        "pubkey": pub, "kind": 23, "created_at": 1750000500,
        "content": "body words from the draft",
        "tags": [["d", "my-post"], ["title", "My Draft"], ["t", "linux"]],
    }
    draft["id"] = _canonical_id(draft)
    conv = nip44.get_conversation_key(bytes.fromhex(priv), bytes.fromhex(pub))
    wrap = {
        "id": DRAFT_WRAP_ID, "pubkey": pub, "kind": 31234,
        "created_at": 1750000000,
        "tags": [["k", "23"], ["d", "wrap-d"]],
        "content": nip44.encrypt(json.dumps(draft), conv),
    }
    return longform.parse_draft_event(wrap, privkey_hex=priv,
                                      self_pubkey_hex=pub)


class TestDraftRelayAttribution:
    def test_fetch_hit_records_wrap_host(self):
        c = FakeLongformClient()
        setattr(c, "draft_target_relays",
                types.MethodType(_bound("draft_target_relays"), c))
        calls = []
        c.fetch_drafts(lambda entries, meta: calls.append((entries, meta)))
        ev = {"id": DRAFT_WRAP_ID, "pubkey": A_PUB, "kind": 31234,
              "created_at": 1750000000, "tags": [["k", "23"], ["d", "x"]],
              "content": "opaque-blob"}
        c._longform_note(ev, "wss://d1.example")
        assert c._longform_draft_relays == {DRAFT_WRAP_ID: {"wss://d1.example"}}
        # targets = seen ∪ write defaults (C3 union shape)
        c.relay_urls = {"wss://u.example"}
        assert c.draft_target_relays(DRAFT_WRAP_ID) == {
            "wss://d1.example", "wss://u.example",
        }
        assert c.draft_target_relays("c" * 64) == {"wss://u.example"}

    def test_legacy_30024_arrival_accepted_as_own_draft(self):
        c = FakeLongformClient()
        calls = []
        c.fetch_drafts(lambda entries, meta: calls.append((entries, meta)))
        ev = {"id": "c" * 64, "pubkey": A_PUB, "kind": 30024,
              "created_at": 1740000000, "tags": [["d", "old"]],
              "content": "plain old draft"}
        c._longform_note(ev, "wss://d2.example")
        c.eose_all("longform_drafts")
        entries, meta = calls[0]
        assert any(e.wrap_id == "c" * 64 and e.legacy for e in entries)
        assert c._longform_draft_relays["c" * 64] == {"wss://d2.example"}


class TestDraftPublishFlow:
    """publish_draft_article: sign-then-gated like C3 — fan the signed 30023
    to seen ∪ write relays, THEN supersede the wrap with a signed kind-5.
    A failed publish keeps the draft."""

    def _client(self, seen=("wss://seen.example",), priv=DRAFTER_PRIV,
                publish_ok=True):
        c = FakeLongformClient()
        c._longform_draft_relays = {DRAFT_WRAP_ID: set(seen)}
        c.my_privkey = priv
        c.my_pubkey = DRAFTER_PUB
        c.signer = None
        c.tracked = []
        c.new_conns = []

        def _new_relay(url):
            r = c.active_relays.setdefault(url, _FakeRecRelay(url))
            if not publish_ok:  # relay rejects EVERY publish (offline path)
                r.publish = lambda ev: False
            return r

        def add_relay_connection(url):
            c.new_conns.append(url)
            _new_relay(url)

        c.add_relay_connection = add_relay_connection
        c._write_relays = lambda: {"wss://w.example": None}

        def _track_publish(event, label, sent_urls=None, candidates=None):
            c.tracked.append(
                (event, label, set(sent_urls or ()), set(candidates or ()))
            )

        c._track_publish = _track_publish
        c._log_relay = lambda msg: None
        for m in ("publish_draft_article", "delete_draft", "_supersede_draft",
                  "draft_target_relays", "_publish_comment_targets", "_sign"):
            setattr(c, m, types.MethodType(_bound(m), c))
        for url in seen:
            _new_relay(url)
        return c

    def test_publish_fans_article_then_supersedes_wrap(self):
        c = self._client()
        entry = _c4_draft_entry()
        template = longform.build_article_from_draft(entry, DRAFTER_PUB)
        assert template is not None and "id" not in template
        assert c.publish_draft_article(template, DRAFT_WRAP_ID) is True
        targets = {"wss://seen.example", "wss://w.example"}
        article_id = nostr_utils.compute_event_id(
            {k: v for k, v in template.items() if k not in ("id", "sig")}
        )
        for url in targets:
            pub = c.active_relays[url].published
            kinds = [e["kind"] for e in pub]
            assert kinds == [30023, 5], f"{url}: {kinds}"
            art, dele = pub
            # every fan copy carries the id computed over the UNSIGNED
            # template (sign-once invariant, C3 lesson)
            assert art["id"] == article_id
            assert verify_event_sig(art) and art["kind"] == 30023
            assert dele["kind"] == 5 and dele["tags"] == [["e", DRAFT_WRAP_ID]]
            assert verify_event_sig(dele)
            assert dele["id"] == nostr_utils.compute_event_id(
                {k: v for k, v in dele.items() if k not in ("id", "sig")}
            )
        # read relays that never hosted the draft are NOT sprayed
        assert c.active_relays["wss://r0.example"].published == []
        assert c.active_relays["wss://r1.example"].published == []
        labels = [t[1] for t in c.tracked]
        assert labels == ["Draft article", "Draft deletion"]
        for _ev, _lbl, sent, cand in c.tracked:
            assert cand == targets and sent == targets
        # the default write relay wasn't connected yet -> publish connects it
        assert "wss://w.example" in c.new_conns

    def test_publish_failure_keeps_the_draft(self):
        c = self._client(publish_ok=False)
        entry = _c4_draft_entry()
        template = longform.build_article_from_draft(entry, DRAFTER_PUB)
        assert c.publish_draft_article(template, DRAFT_WRAP_ID) is False
        # article attempt recorded, but NO supersede deletion was signed/sent
        for url in ("wss://seen.example", "wss://r0.example"):
            assert all(e["kind"] != 5 for e in c.active_relays[url].published)
        assert [t[1] for t in c.tracked] == ["Draft article"]

    def test_publish_no_targets_returns_false_before_signing(self):
        c = self._client(seen=())
        c._write_relays = lambda: {}
        entry = _c4_draft_entry()
        template = longform.build_article_from_draft(entry, DRAFTER_PUB)
        assert c.publish_draft_article(template, DRAFT_WRAP_ID) is False
        assert not c.tracked
        assert not any(r.published for r in c.active_relays.values())

    def test_publish_bunker_signs_article_and_deletion_on_worker(self):
        c = self._client()
        bunker = _FakeBunkerSigner({DRAFTER_PUB}, DRAFTER_PRIV)
        c.signer = bunker
        c.my_privkey = None  # local fallback must NOT be used
        entry = _c4_draft_entry()
        template = longform.build_article_from_draft(entry, DRAFTER_PUB)
        t0 = time.time()
        assert c.publish_draft_article(template, DRAFT_WRAP_ID) is True
        assert time.time() - t0 < 1.0  # worker signs, call returns now
        deadline = time.time() + 5.0
        while time.time() < deadline and len(c.tracked) < 2:
            time.sleep(0.05)
        assert bunker.calls == 2  # article + supersede deletion, no more
        published = [e for r in c.active_relays.values() for e in r.published]
        kinds = sorted(e["kind"] for e in published)
        assert kinds == [5, 5, 30023, 30023]  # 2 targets x article+deletion
        assert all(verify_event_sig(e) for e in published)


class TestDraftDeleteFlow:
    def _client(self, priv=DRAFTER_PRIV, seen=("wss://seen.example",)):
        c = FakeLongformClient()
        c._longform_draft_relays = {DRAFT_WRAP_ID: set(seen)}
        c.my_privkey = priv
        c.my_pubkey = DRAFTER_PUB
        c.signer = None
        c.tracked = []
        c.new_conns = []

        def add_relay_connection(url):
            c.new_conns.append(url)
            c.active_relays.setdefault(url, _FakeRecRelay(url))

        c.add_relay_connection = add_relay_connection
        c._write_relays = lambda: {"wss://w.example": None}

        def _track_publish(event, label, sent_urls=None, candidates=None):
            c.tracked.append(
                (event, label, set(sent_urls or ()), set(candidates or ()))
            )

        c._track_publish = _track_publish
        c._log_relay = lambda msg: None
        for url in seen:
            c.active_relays.setdefault(url, _FakeRecRelay(url))
        for m in ("delete_draft", "draft_target_relays",
                  "_publish_comment_targets", "_sign"):
            setattr(c, m, types.MethodType(_bound(m), c))
        return c

    def test_delete_sends_signed_kind5_to_seen_and_write(self):
        c = self._client()
        assert c.delete_draft(DRAFT_WRAP_ID) is True
        for url in ("wss://seen.example", "wss://w.example"):
            pub = c.active_relays[url].published
            assert len(pub) == 1
            ev = pub[0]
            assert ev["kind"] == 5 and ev["tags"] == [["e", DRAFT_WRAP_ID]]
            assert ev["pubkey"] == DRAFTER_PUB
            assert verify_event_sig(ev)
        assert len(c.tracked) == 1
        assert c.tracked[0][1] == "Draft deletion"
        # local relays never hosting the draft untouched
        assert c.active_relays["wss://r0.example"].published == []

    def test_delete_no_targets_returns_false(self):
        c = self._client(seen=())
        c._write_relays = lambda: {}
        assert c.delete_draft(DRAFT_WRAP_ID) is False
        assert not c.tracked

    def test_delete_bad_wrap_id_returns_false(self):
        c = self._client()
        assert c.delete_draft("nothex") is False
        assert not any(r.published for r in c.active_relays.values())

    def test_delete_bunker_signs_on_worker(self):
        c = self._client()
        bunker = _FakeBunkerSigner({DRAFTER_PUB}, DRAFTER_PRIV)
        c.signer = bunker
        c.my_privkey = None
        assert c.delete_draft(DRAFT_WRAP_ID) is True
        deadline = time.time() + 5.0
        while time.time() < deadline and not c.tracked:
            time.sleep(0.05)
        assert bunker.calls == 1
        published = [e for r in c.active_relays.values() for e in r.published]
        assert len(published) == 2  # 2 targets
        assert all(e["kind"] == 5 and verify_event_sig(e) for e in published)
