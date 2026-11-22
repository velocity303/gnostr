"""NIPW-P3 communities (NIP-29) — golden tests against
tests/fixtures/nipw_communities_test_vectors.json (spec nipw-communities-v1).

Tier-1 (mock-gi): groups.py is GTK-free; the gateway only needs sqlite3 +
mock GLib. No network — fetch_nip11 is exercised only through its URL/shape
helpers, never a socket.
"""

import copy
import json
import os

import pytest

from gnostr import groups as G
from gnostr.gateway.groups_gateway import SQLiteGroupsGateway
from gnostr.nostr_utils import sign_event

VECTORS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "fixtures",
    "nipw_communities_test_vectors.json",
)


@pytest.fixture(scope="module")
def tv():
    with open(VECTORS) as f:
        return json.load(f)


@pytest.fixture
def ref_group(tv):
    return tv["reference_group"]


@pytest.fixture
def live_39000(tv):
    return tv["live_events"]["group_39000"]


# --- relay hygiene (trust-shield §1.2 reuse) ---------------------------------


def test_validate_relay_url_accepts_ws_wss():
    assert G.validate_relay_url("wss://groups.hzrd149.com") == "wss://groups.hzrd149.com"
    assert G.validate_relay_url("wss://groups.hzrd149.com/") == "wss://groups.hzrd149.com"


def test_validate_relay_url_rejects_junk():
    assert G.validate_relay_url("http://groups.hzrd149.com") is None
    assert G.validate_relay_url("wss://localhost") is None
    assert G.validate_relay_url("") is None
    assert G.validate_relay_url(None) is None


# --- kind-10009 registry ------------------------------------------------------


def test_parse_10009_spec_live_shape(tv):
    """The spec §1.1 live shape (2 group tags + 2 r tags) parses to exactly
    the expected refs. NOTE: the JSON's 10009_sample_tags is a broader 5-tag
    fixture; 10009_expected_refs corresponds to the spec's live shape — the
    normative rule (spec §1.2) is 'parse EVERY group tag', tested next."""
    event = {
        "kind": 10009,
        "tags": [
            ["group", "2f28ff", "wss://groups.hzrd149.com"],
            ["group", "21510156189947904", "wss://nostr.gravitywell.xyz"],
            ["r", "wss://nostr.gravitywell.xyz"],
            ["r", "wss://groups.hzrd149.com"],
        ],
    }
    refs = G.parse_10009(event)
    expected = tv["expected_parses"]["10009_expected_refs"]
    assert [(r.id, r.relay, r.name) for r in refs] == [
        (e["id"], e["relay"], e["name"]) for e in expected
    ]
    assert all(r.valid for r in refs)


def test_parse_10009_sample_parses_every_group_tag(tv):
    """Spec §1.2 is normative: every ["group", id, relay, name?] tag becomes
    a ref; ["r", …] tags are ignored for routing."""
    event = {"kind": 10009, "tags": tv["expected_parses"]["10009_sample_tags"]}
    refs = G.parse_10009(event)
    assert len(refs) == 5
    assert [r.id for r in refs] == [
        "21510156189947904", "21251425750130315", "9627828936903456",
        "2f28ff", "7090093746930372",
    ]
    assert all(r.valid for r in refs)


def test_parse_10009_empty(tv):
    event = {"kind": 10009, "tags": tv["expected_parses"]["empty_10009_tags"]}
    assert G.parse_10009(event) == []


def test_parse_10009_invalid_relay_kept_greyed():
    refs = G.parse_10009(
        {"kind": 10009, "tags": [["group", "x1", "http://bad.example"]]}
    )
    assert len(refs) == 1
    assert refs[0].valid is False


def test_group_tags_roundtrip_refs():
    refs = [G.GroupRef("a", "wss://r.example", "Alpha"), G.GroupRef("b", "wss://r.example")]
    tags = G.group_tags_from_refs(refs)
    assert tags == [
        ["group", "a", "wss://r.example", "Alpha"],
        ["group", "b", "wss://r.example"],
    ]
    assert [r.id for r in G.parse_10009({"tags": tags})] == ["a", "b"]


# --- naddr (NIP-19 TLV) --------------------------------------------------------


def test_naddr_decode_vector(tv):
    rg = tv["reference_group"]
    got = G.decode_naddr(rg["naddr"])
    assert got["identifier"] == rg["d"]
    assert G.validate_relay_url(got["relay"]) == rg["relay"]
    assert got["pubkey"] == rg["nip11_self_pubkey"]
    assert got["kind"] == 39000
    assert got["invite"] is None


def test_naddr_encode_roundtrip(tv):
    rg = tv["reference_group"]
    d = G.decode_naddr(rg["naddr"])
    assert G.encode_naddr(d["identifier"], d["relay"], d["pubkey"], d["kind"]) == rg["naddr"]


def test_naddr_invite_suffix(tv):
    rg = tv["reference_group"]
    got = G.decode_naddr(rg["naddr"] + "?invite=s3cr3t")
    assert got["invite"] == "s3cr3t"
    assert got["identifier"] == rg["d"]


def test_naddr_garbage():
    assert G.decode_naddr("nsec1junk") is None
    assert G.decode_naddr("not an naddr") is None
    assert G.decode_naddr("") is None


def test_relay_group_input_fallback():
    assert G.parse_relay_group_input("wss://groups.hzrd149.com 0a3991") == (
        "wss://groups.hzrd149.com", "0a3991",
    )
    assert G.parse_relay_group_input("garbage") is None
    assert G.parse_relay_group_input("http://x.example y") is None


# --- 39000/39001/39002 with the relay-signature gate ----------------------------


def test_39000_live_verifies(tv, ref_group):
    meta = G.parse_group_metadata(
        tv["live_events"]["group_39000"], ref_group["nip11_self_pubkey"],
        relay=ref_group["relay"],
    )
    assert meta is not None
    assert meta.group_id == "0a3991"
    assert meta.name == "blossom"
    assert meta.has_flag("public") and meta.has_flag("open")
    assert not meta.has_flag("private")
    assert "General group" in meta.about
    assert meta.picture.startswith("https://")


def test_39000_re_signed_with_other_key_rejected(tv, ref_group):
    """The preview-gate vector: same content, signed by a random key. The
    signer != NIP-11 self pubkey, so it must be dropped."""
    ev = copy.deepcopy(tv["live_events"]["group_39000"])
    ev = sign_event(ev, "11" * 31 + "22")
    assert G.parse_group_metadata(ev, ref_group["nip11_self_pubkey"]) is None


def test_39000_tampered_content_rejected(tv, ref_group):
    ev = copy.deepcopy(tv["live_events"]["group_39000"])
    ev["tags"] = ev["tags"] + [["name", "evil"]]
    assert G.parse_group_metadata(ev, ref_group["nip11_self_pubkey"]) is None


def test_39000_wrong_self_pk_rejected(tv):
    assert G.parse_group_metadata(tv["live_events"]["group_39000"], "ab" * 32) is None


def test_39001_admins(tv, ref_group):
    admins = G.parse_group_admins(
        tv["live_events"]["group_39001"], ref_group["nip11_self_pubkey"]
    )
    assert admins == [("266815e0c9210dfa324c6cba3573b14bee49da4209a9456f9484e5106cd408a5", "king")]


def test_39001_wrong_signer_dropped(tv):
    assert G.parse_group_admins(tv["live_events"]["group_39001"], "ab" * 32) is None


def test_39002_member_count(tv):
    assert G.count_members(tv["live_events"]["group_39002"]) == tv["live_events"]["member_count_39002_p_tags"]


# --- membership truth table (spec §2.4) ----------------------------------------


def _mem(kind, ts, pk):
    return {"kind": kind, "created_at": ts, "tags": [["p", pk]]}


def test_membership_unknown_when_empty():
    assert G.latest_membership([], "aa" * 32) == "unknown"


def test_membership_9001_is_member():
    assert G.latest_membership([_mem(9001, 100, "aa" * 32)], "aa" * 32) == "member"


def test_membership_latest_wins_removed():
    evs = [_mem(9001, 100, "aa" * 32), _mem(9000, 200, "aa" * 32)]
    assert G.latest_membership(evs, "aa" * 32) == "removed"


def test_membership_latest_wins_member():
    evs = [_mem(9000, 100, "aa" * 32), _mem(9001, 200, "aa" * 32)]
    assert G.latest_membership(evs, "aa" * 32) == "member"


def test_membership_other_user_events_ignored():
    assert G.latest_membership([_mem(9000, 100, "bb" * 32)], "aa" * 32) == "unknown"


# --- REQ shapes + event templates (spec §3 vectors) ------------------------------


def test_req_shapes_match_vectors(tv):
    gid = tv["reference_group"]["d"]
    assert G.metadata_filter(gid) == tv["req_shapes"]["metadata"]
    assert G.timeline_filter(gid) == tv["req_shapes"]["timeline"]
    assert G.membership_filter(gid, "<self_hex>") == tv["req_shapes"]["membership"]


def test_join_template_matches_vector(tv):
    tmpl = tv["event_templates"]["9021_join"]
    ev = G.build_join_event("aa" * 32, "0a3991", created_at=123)
    assert ev["kind"] == tmpl["kind"]
    assert ev["tags"] == tmpl["tags"]
    assert ev["content"] == tmpl["content"]


def test_join_code_tag_only_with_invite():
    assert ["code", "x"] not in G.build_join_event("aa" * 32, "g")["tags"]
    ev = G.build_join_event("aa" * 32, "g", invite="x")
    assert ["code", "x"] in ev["tags"]


def test_leave_template_matches_vector(tv):
    tmpl = tv["event_templates"]["9022_leave"]
    ev = G.build_leave_event("aa" * 32, "0a3991", created_at=123)
    assert ev["kind"] == tmpl["kind"]
    assert ev["tags"] == tmpl["tags"]
    assert ev["content"] == tmpl["content"]


def test_post_template_h_tag_and_previous():
    evs = [
        {"id": "11" * 32, "pubkey": "bb" * 32},
        {"id": "22" * 32, "pubkey": "cc" * 32},
        {"id": "33" * 32, "pubkey": "dd" * 32},
        {"id": "44" * 32, "pubkey": "ee" * 32},
    ]
    prev = G.previous_tags(evs, self_pk="ff" * 32)
    assert prev == ["11111111", "22222222", "33333333"]  # <=3, first-8-hex
    ev = G.build_group_post_event("aa" * 32, "0a3991", "hi", previous=prev)
    assert ev["kind"] == 1
    assert ev["tags"][0] == ["h", "0a3991"]
    assert ["previous", "11111111"] in ev["tags"]


def test_previous_tags_skips_self():
    evs = [
        {"id": "11" * 32, "pubkey": "aa" * 32},
        {"id": "22" * 32, "pubkey": "cc" * 32},
    ]
    assert G.previous_tags(evs, self_pk="aa" * 32) == ["22222222"]


# --- join state machine (spec §2) -------------------------------------------------


def _machine(invite=None):
    return G.GroupJoinMachine("0a3991", "wss://groups.hzrd149.com", "aa" * 32, invite=invite)


def _meta(**kw):
    base = dict(name="g", flags=["public", "open"])
    base.update(kw)
    return G.GroupMeta("wss://groups.hzrd149.com", "0a3991", **base)


def test_machine_happy_path():
    m = _machine()
    assert m.begin() == "resolving"
    assert m.on_nip11({"supported_nips": [29]}) == "resolving"
    assert m.on_metadata(_meta()) == "preview"
    assert m.on_join("evt123") == "join_sent"
    # NIPW-P3-FIX-3 (Bug C): an OK ack means ACCEPTED, not member — an
    # approval-gated group (blossom) sits at 'pending' until 39002 confirms.
    assert m.on_join_ok(True) == "pending"
    assert m.on_membership("member") == "member"


def test_machine_join_blocked_before_preview():
    m = _machine()
    m.begin()
    assert m.on_join("x") == "resolving"  # gate: no 9021 before verified preview


def test_machine_private_aborts():
    m = _machine()
    m.begin()
    m.on_nip11({"supported_nips": [29]})
    assert m.on_metadata(_meta(flags=["private"])) == "aborted"
    assert "NIP-42" in m.abort_reason
    assert m.on_join("x") == "aborted"


def test_machine_not_found_aborts():
    m = _machine()
    m.begin()
    m.on_nip11({"supported_nips": [29]})
    assert m.on_metadata(None) == "aborted"
    assert "not found" in m.abort_reason


def test_machine_relay_without_29_aborts():
    m = _machine()
    m.begin()
    assert m.on_nip11({"supported_nips": [1, 9]}) == "aborted"
    assert m.on_nip11(None) == "aborted"


def test_machine_duplicate_ok_is_already_member():
    m = _machine()
    m.begin()
    m.on_nip11({"supported_nips": [29]})
    m.on_metadata(_meta())
    m.on_join("x")
    assert m.on_join_ok(False, "duplicate: already in group") == "already_member"


def test_machine_rejection_carries_reason():
    m = _machine()
    m.begin()
    m.on_nip11({"supported_nips": [29]})
    m.on_metadata(_meta())
    m.on_join("x")
    assert m.on_join_ok(False, "restricted: ask an admin") == "rejected"
    assert m.rejection_reason == "restricted: ask an admin"


def test_machine_closed_without_invite_blocks_join():
    m = _machine()
    m.begin()
    m.on_nip11({"supported_nips": [29]})
    m.on_metadata(_meta(flags=["closed"]))
    assert m.state == "preview"
    assert m.closed_without_invite() is True
    assert m.on_join("x") == "preview"  # button disabled; no event leaves


def test_machine_closed_with_invite_joins():
    m = _machine(invite="code1")
    m.begin()
    m.on_nip11({"supported_nips": [29]})
    m.on_metadata(_meta(flags=["closed"]))
    assert m.on_join("x") == "join_sent"


def test_machine_pending_then_member():
    m = _machine()
    m.begin()
    m.on_nip11({"supported_nips": [29]})
    m.on_metadata(_meta())
    m.on_join("x")
    assert m.on_membership("unknown") == "pending"
    assert m.on_membership("member") == "member"


def test_machine_removed_after_join_is_rejected():
    m = _machine()
    m.begin()
    m.on_nip11({"supported_nips": [29]})
    m.on_metadata(_meta())
    m.on_join("x")
    assert m.on_membership("removed") == "rejected"


# --- NIP-11 helpers ---------------------------------------------------------------


def test_nip11_url():
    assert G.nip11_url("wss://groups.hzrd149.com") == "https://groups.hzrd149.com"
    assert G.nip11_url("ws://localhost:7777") == "http://localhost:7777"
    assert G.nip11_url("nonsense") is None


def test_nip11_supports_and_self():
    doc = {"supported_nips": [1, 9, 29], "pubkey": "49" * 32}
    assert G.nip11_supports_groups(doc) is True
    assert G.nip11_supports_groups({"supported_nips": [1]}) is False
    assert G.nip11_supports_groups(None) is False
    assert G.nip11_self_pk(doc) == "49" * 32
    assert G.nip11_self_pk({"pubkey": "nothex"}) is None


# --- SQLite gateway (spec §4 schema) ------------------------------------------------


@pytest.fixture
def gw(tmp_path):
    g = SQLiteGroupsGateway(str(tmp_path / "g.db"))
    g.init_db()
    yield g
    g.close()


def test_gateway_group_roundtrip(gw):
    gw.upsert_group("wss://r.example", "g1", name="One", flags=["public"], admins=[["aa" * 32, "king"]])
    rows = gw.list_groups()
    assert len(rows) == 1
    assert rows[0]["name"] == "One"
    assert rows[0]["flags"] == ["public"]
    gw.upsert_group("wss://r.example", "g1", member_count=97)
    rows = gw.list_groups()
    assert rows[0]["member_count"] == 97
    assert rows[0]["name"] == "One"  # partial update keeps other fields


def test_gateway_events_roundtrip(gw):
    ev = {"id": "aa" * 32, "kind": 1, "pubkey": "bb" * 32, "created_at": 100,
          "content": "hi", "tags": [["h", "g1"]]}
    gw.save_event("wss://r.example", "g1", ev)
    gw.save_event("wss://r.example", "g1", ev)  # idempotent
    out = gw.get_events("wss://r.example", "g1")
    assert len(out) == 1
    assert out[0]["tags"] == [["h", "g1"]]


def test_gateway_membership_and_leave(gw):
    gw.upsert_group("wss://r.example", "g1")
    gw.set_membership("wss://r.example", "g1", "member")
    assert gw.get_membership("wss://r.example", "g1") == "member"
    gw.remove_group("wss://r.example", "g1")
    assert gw.get_membership("wss://r.example", "g1") is None
    assert gw.list_groups() == []


# --- NIPW-P3-FIX-4: relay-key hygiene (membership regression on revisit) ----
# Live tag audit 2026-11-21 (nak req probes, groups.hzrd149.com +
# chat.wisp.talk) confirmed every REQ shape already matches relay reality:
# 39000/39001/39002 are #d-tagged on khatru relays (39002 #h probes return
# ZERO — the relay's own log says 'filter does not match'), while group
# timeline posts and 9000/9001 audit events are #h-tagged. Those four shapes
# are pinned below (test_req_shapes_* cover metadata/timeline/membership;
# test_members_filter_shape pins 39002 #d). The only remaining way a correct
# REQ can still read 'not joined' on revisit is the gateway losing the row
# between a write keyed by one relay-string form and a read keyed by
# another — fixed by normalizing every relay key + migrating legacy rows.


def test_gateway_relay_keys_unified_across_forms(tmp_path):
    """A membership row written via a trailing-slash relay form must be
    readable via the no-slash form (and vice versa) — the exact silent-None
    path that showed 'not joined' on revisit."""
    db = str(tmp_path / "gk.db")
    g = SQLiteGroupsGateway(db)
    g.init_db()
    g.upsert_group("wss://r.example/", "g1", name="One")
    g.set_membership("wss://r.example/", "g1", "pending")
    assert g.get_membership("wss://r.example", "g1") == "pending"
    g.set_membership("wss://r.example", "g1", "member")
    rows = [r for r in g.list_groups() if r["group_id"] == "g1"]
    assert len(rows) == 1 and rows[0]["relay"] == "wss://r.example"
    g.save_event("wss://r.example/", "g1", {"id": "ab" * 32, "kind": 1,
                                            "pubkey": "bb" * 32, "created_at": 5,
                                            "content": "", "tags": []})
    assert len(g.get_events("wss://r.example", "g1")) == 1
    g.close()


def test_gateway_legacy_rows_migrated_on_reopen(tmp_path):
    """Legacy DB (rows keyed with the old raw/slash form) migrates once at
    init_db: membership survives the reopen, no duplicate rows, migration
    is idempotent across restarts."""
    db = str(tmp_path / "lg.db")
    g1 = SQLiteGroupsGateway(db)
    g1.init_db()
    # simulate pre-FIX-4 writes (raw slash key in both tables)
    g1.conn.execute(
        "INSERT INTO groups (relay, group_id, name, fetched_at)"
        " VALUES (?, ?, ?, ?)", ("wss://r.example/", "g1", "Old", 100))
    g1.conn.execute(
        "INSERT INTO group_membership (relay, group_id, state, updated_at)"
        " VALUES (?, ?, ?, ?)", ("wss://r.example/", "g1", "member", 200))
    g1.conn.commit()
    g1.close()

    g2 = SQLiteGroupsGateway(db)  # no init_db: reopen must NOT touch data
    assert g2.conn is None
    g2.init_db()
    assert g2.get_membership("wss://r.example", "g1") == "member"
    rows = [r for r in g2.list_groups() if r["group_id"] == "g1"]
    assert len(rows) == 1 and rows[0]["relay"] == "wss://r.example"
    g2.conn.execute(  # a fresher no-slash groups row already exists
        "UPDATE groups SET fetched_at=500 WHERE relay=? AND group_id=?",
        ("wss://r.example", "g1"))
    # re-create the legacy slash row ALONGSIDE the fresh normalized row:
    # next migration must keep the fresher normalized row, drop the stale
    g2.conn.execute(
        "INSERT OR REPLACE INTO groups (relay, group_id, name, fetched_at)"
        " VALUES (?, ?, ?, ?)", ("wss://r.example/", "g1", "Stale", 50))
    g2.conn.commit()
    g2.close()
    g3 = SQLiteGroupsGateway(db)
    g3.init_db()  # collision merge: freshest wins, no duplicate rows
    rows = [r for r in g3.list_groups() if r["group_id"] == "g1"]
    assert len(rows) == 1 and rows[0]["fetched_at"] == 500
    assert g3.get_membership("wss://r.example", "g1") == "member"
    g3.close()


# Pin of the live 2-relay tag audit (see module comment above): the four
# group REQ shapes vs what the khatru relays actually store.
def test_req_shapes_pinned_against_live_tag_audit():
    gid = "0a3991"  # blossom @ groups.hzrd149.com
    assert G.members_filter(gid) == {"kinds": [39002], "#d": [gid]}
    f = G.membership_filter(gid, "aa" * 32)
    assert f["#h"] == [gid] and 9000 in f["kinds"] and 9001 in f["kinds"]
    assert "#d" not in f  # 9000/9001 carry ONLY h (probe: #d -> zero)
    assert G.metadata_filter(gid)["#d"] == [gid] and "#h" not in G.metadata_filter(gid)
    assert G.timeline_filter(gid)["#h"] == [gid] and "#d" not in G.timeline_filter(gid)
    # NIPW-P3-ACTIVITY: live audit found the groups' ACTIVITY outside kind 1
    # (blossom: kind 9 file events; wisp: kind 11 videos + 1111 comments) —
    # the activity set is pinned so a regression to (1,7,9) fails the suite.
    assert set(G.timeline_filter(gid)["kinds"]) == {1, 6, 7, 9, 11, 1111, 9734}


# --- 39002-set membership (live finding: khatru relays keep no per-user
# 9000/9001 audit trail for open joins; the 39002 set is the truth) ---------


def test_members_filter_shape():
    f = G.members_filter("0a3991")
    assert f == {"kinds": [39002], "#d": ["0a3991"]}


def test_is_member_in_members_event():
    ev = {"kind": 39002, "tags": [["d", "g"], ["p", "aa" * 32], ["p", "bb" * 32]]}
    assert G.is_member_in_members_event(ev, "aa" * 32) is True
    assert G.is_member_in_members_event(ev, "cc" * 32) is False
    assert G.is_member_in_members_event({"kind": 39000, "tags": []}, "aa" * 32) is None
    assert G.is_member_in_members_event(None, "aa" * 32) is None


def test_machine_membership_authoritative_from_preview():
    # 39002 snapshot says already a member -> preview becomes member
    m = _machine()
    m.begin()
    m.on_nip11({"supported_nips": [29]})
    m.on_metadata(_meta())
    assert m.on_membership("member") == "member"


def test_machine_membership_removed_after_join_is_rejection():
    m = _machine()
    m.begin()
    m.on_nip11({"supported_nips": [29]})
    m.on_metadata(_meta())
    m.on_join("evt1")
    assert m.on_membership("removed") == "rejected"


def test_machine_membership_leave_drops_back_to_preview():
    m = _machine()
    m.begin()
    m.on_nip11({"supported_nips": [29]})
    m.on_metadata(_meta())
    m.on_membership("member")
    assert m.on_membership("removed") == "preview"


def test_machine_membership_ignored_before_preview():
    m = _machine()
    assert m.on_membership("member") == "idle"
