"""NIPW-P3-FIX-3 contracts (no GTK, no network): revisit meta loop,
aggregator auth noise, requested-vs-confirmed.

FLX1 round-3 device log:
  sub_open x4 ok -> nip11 ok ms=0 -> sub_meta result=fail -> subs re-opened
  (three cycles on a REVISIT whose metadata was already in the gateway DB);
  sub_close relay=wss://aggr.nostr.land sub=glist_self result=fail
  reason=auth-required (every communities visit);
  blossom join_ok ok but 'joined' vs 'membership confirmed' unresolved.

Contracts pinned here (source-extracted, same pattern as
test_group_hardening):
- NostrRelay.subscribe(exclusive=False): group REQs coexist with the relay's
  feed sub — no CLOSE cascade (the CLOSED cycle's real source).
- NostrRelay.subscribe(exclusive=True): feed-slot swap unchanged (regression).
- NostrRelay.handle_sub_closed: CLOSED with an auth reason marks the relay
  (_auth_required) and never re-REQs; idle/timeout sweeps on LIVE subs still
  re-open, bounded (regression).
- NostrClient._routed_relays: 'read' fan-out skips relays proven NIP-42 this
  session; 'write' (publish — blossom joins must reach blossom relays) keeps
  them.
- GroupJoinMachine: OK-ack == ACCEPTED, not member (join_sent -> pending);
  the gateway seed pending applies from preview/join_sent/resolving and can
  never downgrade member.
- SQLiteGroupsGateway.get_group: revisit reads the cached row (get_group +
  get_membership) without any relay I/O.
"""

import ast
import json
import os
import time

import pytest

import gnostr
import gnostr.groups as groups
from gnostr.gateway.groups_gateway import SQLiteGroupsGateway
from gnostr.groups import GroupJoinMachine

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src", "gnostr"))
_CLIENT_PATH = os.path.join(_SRC, "client.py")


def _extract(path, target, method_name, extra_ns=None):
    """Extract a real function from source (class method or module-level
    function) without importing the module under the mocked GObject."""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == target:
            fn = next(
                (
                    n
                    for n in node.body
                    if isinstance(n, ast.FunctionDef) and n.name == method_name
                ),
                None,
            )
            break
        if isinstance(node, ast.FunctionDef) and node.name == method_name:
            fn = node
            break
    if fn is None:
        raise LookupError(f"{target}.{method_name} not found in {path}")
    ns = {"json": json, "time": time}
    if extra_ns:
        ns.update(extra_ns)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), path, "exec"), ns)
    return ns[method_name]


# Reuse the REAL module-level helpers the extracted methods call.
_ns_helpers = {
    "nip01_sub_id": _extract(_CLIENT_PATH, None, "nip01_sub_id"),
    "is_group_sub_id": _extract(_CLIENT_PATH, None, "is_group_sub_id"),
    "sub_closed_reopenable": _extract(_CLIENT_PATH, None, "sub_closed_reopenable"),
}


class _FakeWS:
    def __init__(self):
        self.frames = []

    def send(self, text):
        self.frames.append(json.loads(text))


class _FakeRelay:
    """Attribute surface NostrRelay.subscribe / handle_sub_closed touch."""

    def __init__(self, url="wss://aggr.nostr.land", connected=True):
        self.url = url
        self.ws = _FakeWS()
        self.is_connected = connected
        self.sub_id = None
        self.active_sub_ids = set()
        self.live_subs = {}
        self.snapshot_ids = set()
        self.request_queue = []
        self.queued_sub_ids = set()
        self._reopen_counts = {}
        self._closed_notified = 0
        self._auth_required = False
        self._idle_closed = False
        self.enqueued = []

    def _enqueue(self, sub_id, filters):
        self.enqueued.append((sub_id, filters))

    def wake(self):
        pass


# --- Bug A: concurrent group subs must not CLOSE each other ------------------


def test_group_req_exclusive_false_does_not_close_prior_sub():
    sub = _extract(_CLIENT_PATH, "NostrRelay", "subscribe", _ns_helpers)
    r = _FakeRelay(connected=True)
    sub(r, "feed_ab", [{"kinds": [1]}])  # feed's exclusive REQ
    assert r.sub_id == "feed_ab"
    # GroupDetailPage opens four concurrent subs (gmeta/gmemset/gtl/gmem).
    for sid in ("gmeta_test-gro", "gmemset_test-gro", "gtl_test-gro"):
        sub(r, sid, [{"kinds": [39000]}], snapshot=sid.startswith("gmeta"),
            exclusive=False)
    closes = [f for f in r.ws.frames if f[0] == "CLOSE"]
    assert not closes, f"exclusive=False REQs still closed prior subs: {closes}"
    assert r.sub_id == "feed_ab", "group REQ stole the feed's sub slot"
    reqs = [f[1] for f in r.ws.frames if f[0] == "REQ"]
    assert reqs == ["feed_ab", "gmeta_test-gro", "gmemset_test-gro", "gtl_test-gro"]
    # snapshot/live bookkeeping unchanged by the exclusive flag
    assert "gmeta_test-gro" in r.snapshot_ids
    assert "gtl_test-gro" in r.live_subs


def test_exclusive_req_still_swaps_feed_slot():
    """Regression: the feed's rotating queries keep the single-sub swap."""
    sub = _extract(_CLIENT_PATH, "NostrRelay", "subscribe", _ns_helpers)
    r = _FakeRelay(connected=True)
    sub(r, "feed_ab", [{"kinds": [1]}])
    sub(r, "feed_cd", [{"kinds": [0]}])  # exclusive default
    closes = [f for f in r.ws.frames if f[0] == "CLOSE"]
    assert closes == [["CLOSE", "feed_ab"]], closes
    assert r.sub_id == "feed_cd"


# --- Bug B: auth-required CLOSED marks the relay, never re-REQs --------------


def test_closed_auth_required_marks_relay_and_skips_reopen():
    h = _extract(_CLIENT_PATH, "NostrRelay", "handle_sub_closed", _ns_helpers)
    r = _FakeRelay()
    r.snapshot_ids.add("glist_self"[:64])
    # FLX1: sub_close relay=aggr... sub=glist_self reason=auth-required
    assert h(r, "glist_self", "closed: auth-required") is False
    assert r._auth_required is True
    assert r.enqueued == [], "auth-refused sub was re-REQ'd (ban spiral)"
    assert r.request_queue == []
    # a second auth close stays quiet + still no re-REQ
    assert h(r, "glist_self", "closed: auth-required") is False
    assert r.enqueued == []


def test_idle_closed_live_sub_still_reopens_bounded():
    """Regression (NIPW-P3-HARDEN sweep-recovery) + the bounded cap."""
    h = _extract(_CLIENT_PATH, "NostrRelay", "handle_sub_closed", _ns_helpers)
    r = _FakeRelay()
    r.live_subs["gtl_test-gro"] = [{"kinds": [1, 7]}]
    for attempt in range(1, 7):
        reopened = h(r, "gtl_test-gro", "closed: timeout")
        if attempt <= 5:
            r.live_subs["gtl_test-gro"] = [{"kinds": [1, 7]}]  # re-REQ re-tracks
        else:
            assert reopened is False, "unbounded re-REQ loop"
    assert len([e for e in r.enqueued if e[0] == "gtl_test-gro"]) == 5


# --- Bug B: read routing skips auth-marked relays; write does not -----------


def _routed(client_self, role):
    routed = _extract(
        _CLIENT_PATH, "NostrClient", "_routed_relays",
        {"gnostr": gnostr},
    )
    return routed(client_self, role)


def test_read_routing_skips_auth_relay_write_keeps_it():
    a = _FakeRelay(url="wss://groups.hzrd149.com")
    b = _FakeRelay(url="wss://aggr.nostr.land")
    b._auth_required = True
    fake_self = type(
        "S", (),
        {
            "active_relays": {a.url: a, b.url: b},
            "relay_entries": [
                {"url": a.url, "roles": ["read", "write"]},
                {"url": b.url, "roles": ["read", "write"]},
            ],
        },
    )()
    read = _routed(fake_self, "read")
    write = _routed(fake_self, "write")
    assert list(read) == [a.url], "read fan-out still hit the NIP-42 relay"
    assert set(write) == {a.url, b.url}, "joins must still reach blossom relay"


def test_read_routing_without_entries_marks_auth_relay_skipped():
    """active_relays-only path (no relay_entries): the flag alone excludes."""
    a = _FakeRelay(url="wss://good.relay")
    b = _FakeRelay(url="wss://aggr.nostr.land")
    b._auth_required = True
    fake_self = type("S", (), {"active_relays": {a.url: a, b.url: b},
                               "relay_entries": None})()
    routed = _extract(_CLIENT_PATH, "NostrClient", "_routed_relays",
                      {"gnostr": gnostr})
    assert list(routed(fake_self, "read")) == [a.url]
    assert set(routed(fake_self, "write")) == {a.url, b.url}


def test_auth_relay_pre_skipped_without_session_flag():
    """FLX1 acceptance: FIRST communities visit must not log an aggr
    auth-required failure — the relay is pre-marked in relay_entries, not
    discovered mid-session (fresh connection, flag never tripped)."""
    a = _FakeRelay(url="wss://good.relay")
    b = _FakeRelay(url="wss://aggr.nostr.land")  # no _auth_required attribute
    fake_self = type("S", (), {"active_relays": {a.url: a, b.url: b},
                               "relay_entries": None})()
    routed = _extract(_CLIENT_PATH, "NostrClient", "_routed_relays",
                      {"gnostr": gnostr})
    assert list(routed(fake_self, "read")) == [a.url]
    assert set(routed(fake_self, "write")) == {a.url, b.url}


# --- Bug C: accepted != member; seed semantics -------------------------------


def _machine():
    m = GroupJoinMachine("g" * 64, "wss://relay.test", "s" * 64)
    m.begin()
    m.on_nip11({"supported_nips": [29]})
    return m


def _meta():
    return groups.GroupMeta("wss://relay.test", "g" * 64, name="Blossom",
                            about="a", flags=["public"], admins=[])


def test_ok_ack_means_accepted_not_member():
    m = _machine()
    assert m.on_metadata(_meta()) == "preview"
    assert m.on_join("evt1") == "join_sent"
    # blossom answers OK then waits on an admin: pending is the resting state
    assert m.on_join_ok(True) == "pending"
    assert m.on_join_ok(True) == "pending"  # already pending: no double flip
    # and the 39002 push finally settles it
    assert m.on_membership("member") == "member"


def test_seed_pending_applies_from_preview_resolving_never_downgrades_member():
    m = _machine()
    m.on_metadata(_meta())
    assert m.on_membership("pending") == "pending"  # revisit seed on preview
    assert m.on_membership("member") == "member"  # 39002 confirms
    m2 = GroupJoinMachine("g" * 64, "wss://relay.test", "s" * 64)
    m2.begin()  # resolving
    assert m2.on_membership("pending") == "pending"  # seed before probe ends
    assert m2.on_membership("member") == "member"
    m3 = _machine()
    m3.on_metadata(_meta())
    m3.on_join("e")
    m3.on_join_ok(True)
    assert m3.state == "pending"
    m4 = _machine()
    m4.on_metadata(_meta())
    m4.on_membership("member")
    assert m4.on_membership("pending") == "member"  # never past member


# --- Bug A/B gateway surface: revisit reads cache only ------------------------


def test_gateway_get_group_and_membership_roundtrip(tmp_path):
    gw = SQLiteGroupsGateway(str(tmp_path / "gnostr.db"))
    gw.init_db()
    assert gw.get_group("wss://relay.test", "nope") is None
    assert gw.get_membership("wss://relay.test", "nope") is None
    now = int(time.time())
    gw.upsert_group(
        "wss://relay.test", "wisp",
        name="Wisp", about="quiet corner", picture="pic", banner=None,
        flags=["public"], admins=["a" * 64], member_count=3,
        supported_kinds=[1, 39000], fetched_at=now,
    )
    gw.set_membership("wss://relay.test", "wisp", "pending")
    row = gw.get_group("wss://relay.test", "wisp")
    assert row and row["name"] == "Wisp" and row["about"] == "quiet corner"
    assert row["flags"] == ["public"] and row["member_count"] == 3
    assert gw.get_membership("wss://relay.test", "wisp") == "pending"
    gw.set_membership("wss://relay.test", "wisp", "member")
    assert gw.get_membership("wss://relay.test", "wisp") == "member"
