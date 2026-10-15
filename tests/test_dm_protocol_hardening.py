"""Phase P protocol hardening (spec docs/specs/dm-initiation-and-feed-freshness.md).

Covers P1 (probe_dm_reachability tri-state), P2 (lookup_nip05 discovery),
P3 (kind-10050 tag-name tolerance r|relay), P4 (fetch_dm_relay_status batch
probe), P5 (new-conversation initiation throttle).

Client-side units reuse the ast-extraction harness from
test_dm_relay_targeting.py (FakeClient + FakeRelay, GLib.idle_add as a direct
call — deterministic, no main loop). NIP-05 runs against a LOCAL http.server
fixture only — no test touches the network.
"""

import http.server
import json
import threading
import time
from unittest.mock import MagicMock

import pytest

from test_dm_relay_targeting import (
    DM_RELAY_A,
    DM_RELAY_B,
    OTHER_RELAY,
    PEER,
    PEER2,
    FakeClient,
    _dmrelays_event,
    _relay_requests,
)

import gnostr.profile_nips as profile_nips

BOB = "a" * 64


@pytest.fixture
def client():
    return FakeClient()


# ---------------------------------------------------------------------------
# P1 — probe_dm_reachability: tri-state semantics (reachable / no-10050 /
# in-flight), same cache + waiters as resolve_dm_relays, no second cache.
# ---------------------------------------------------------------------------


def test_probe_reachable_via_10050_event(client):
    seen = []
    client.probe_dm_reachability(PEER, lambda pk, relays: seen.append((pk, relays)))
    # In-flight: callback has NOT fired yet (third state).
    assert seen == []
    assert PEER in client._dm_relay_waiters
    client._handle_event(_dmrelays_event(PEER, [DM_RELAY_A]))
    assert seen == [(PEER, [DM_RELAY_A])]
    # Positive cached in the SHARED cache (no second cache).
    assert PEER in client.dm_relay_cache


def test_probe_unreachable_on_timeout(client):
    seen = []
    client.probe_dm_reachability(PEER, lambda pk, relays: seen.append((pk, relays)))
    client._dm_relay_timeout(PEER)
    assert seen == [(PEER, None)]
    # Negatives are never cached.
    assert PEER not in client.dm_relay_cache


def test_probe_cache_hit_skips_request(client):
    client.dm_relay_cache[PEER] = (time.time(), [DM_RELAY_A])
    seen = []
    client.probe_dm_reachability(PEER, lambda pk, relays: seen.append((pk, relays)))
    assert seen == [(PEER, [DM_RELAY_A])]
    assert _relay_requests(client) == []  # no REQ on a fresh cache hit


def test_probe_shares_one_lookup_with_send(client):
    """P1 wrapper and publish_dm share the waiter list: one REQ total."""
    client.publish_dm(PEER, "hi")
    client.probe_dm_reachability(PEER, lambda pk, relays: None)
    assert len(_relay_requests(client)) == 1


# ---------------------------------------------------------------------------
# P3 — kind-10050 tag-name tolerance: parse BOTH "r" and "relay"; publish
# stays "r". A spec-literal peer's list must not read as empty.
# ---------------------------------------------------------------------------


def test_10050_relay_tag_name_parses(client):
    ev = _dmrelays_event(PEER, [DM_RELAY_A])
    ev["tags"] = [["relay", DM_RELAY_A]]  # NIP-17 example spelling
    client.publish_dm(PEER, "hi")
    client._handle_event(ev)
    # Published to the named relay only via the "relay"-tagged list:
    assert client.active_relays[DM_RELAY_A].published != []
    assert client.active_relays[OTHER_RELAY].published == []
    assert client.dm_relay_cache[PEER][1] == [DM_RELAY_A]


def test_10050_mixed_tag_names_parses(client):
    ev = _dmrelays_event(PEER, [])
    ev["tags"] = [["r", DM_RELAY_A], ["relay", DM_RELAY_B]]
    client._handle_event(ev)
    assert client.dm_relay_cache[PEER][1] == [DM_RELAY_A, DM_RELAY_B]


def test_10050_publish_still_uses_r_tags(client):
    assert client.publish_dm_relay_list() is True
    ev = client.published[-1]
    names = {t[0] for t in ev["tags"] if t[1].startswith("wss://")}
    assert names == {"r"}  # publish stays "r" (majority convention)


# ---------------------------------------------------------------------------
# P4 — fetch_dm_relay_status: chunked <=50 REQs, EOSE-or-timeout settle,
# positives into dm_relay_cache, negatives never cached.
# ---------------------------------------------------------------------------


def _status_reqs(client):
    return [f for (_, f) in client.requests if f.get("kinds") == [10050]]


def test_batch_probe_chunks_50_authors(client):
    pks = [f"{i:064x}" for i in range(120)]
    client.fetch_dm_relay_status(pks, lambda m: None)
    reqs = _status_reqs(client)
    assert [len(f["authors"]) for f in reqs] == [50, 50, 20]
    for f in reqs:
        assert f["limit"] == len(f["authors"])


def test_batch_probe_settles_on_eose_fanin(client):
    got = []
    client.fetch_dm_relay_status([PEER, PEER2], lambda m: got.append(m))
    sub_id = next(iter(client._dm_status_by_sub))
    # One relay answering is not enough — wait for all connected relays.
    client._handle_eose(sub_id, DM_RELAY_A)
    assert got == []
    client._handle_eose(sub_id, DM_RELAY_B)
    assert got == []
    client._handle_eose(sub_id, OTHER_RELAY)
    # Settled: only PRESENT peers appear; here nobody answered -> {}
    assert got == [{}]
    # Absent peers are unknown, never cached.
    assert PEER not in client.dm_relay_cache and PEER2 not in client.dm_relay_cache


def test_batch_probe_positive_feeds_shared_cache(client):
    got = []
    client.fetch_dm_relay_status([PEER, PEER2], lambda m: got.append(m))
    sub_id = next(iter(client._dm_status_by_sub))
    client._handle_event(_dmrelays_event(PEER, [DM_RELAY_A]))
    for url in (DM_RELAY_A, DM_RELAY_B, OTHER_RELAY):
        client._handle_eose(sub_id, url)
    assert got == [{PEER: [DM_RELAY_A]}]
    # Positive landed in the shared TTL cache (first DM skips the lookup).
    assert client.dm_relay_cache[PEER][1] == [DM_RELAY_A]
    assert PEER2 not in client.dm_relay_cache


def test_batch_probe_timeout_settles(client):
    got = []
    client.fetch_dm_relay_status([PEER], lambda m: got.append(m))
    token = next(iter(client._dm_status_batches))
    client._dm_status_timeout(token)
    assert got == [{}]
    # Settled batches are gone; a late EOSE must not re-fire the callback.
    client._dm_status_timeout(token)
    assert got == [{}]


def test_batch_probe_dedupes_and_empty(client):
    got = []
    client.fetch_dm_relay_status([], lambda m: got.append(m))
    assert got == [{}]
    client.fetch_dm_relay_status([PEER, PEER], lambda m: None)
    assert [len(f["authors"]) for f in _status_reqs(client)] == [1]


# ---------------------------------------------------------------------------
# P5 — initiation throttle: warn at 10/h (send allowed), hard-stop at 30/h
# (send refused), replies in existing conversations exempt.
# ---------------------------------------------------------------------------


def test_throttle_warns_at_10_allows_send(client):
    for i in range(9):
        assert client._dm_initiation_allowed(f"{i:064x}") is True
    assert client._dm_initiation_allowed("9" * 64) is True  # 10th: allowed
    levels = [a[2] for a in client.emitted if a[0] == "dm-initiation-throttled"]
    assert levels == ["warn"]  # warned once at the threshold


def test_throttle_hard_stops_at_30(client):
    for i in range(30):
        assert client._dm_initiation_allowed(f"{i:064x}") is True
    assert client._dm_initiation_allowed("f" * 64) is False  # 31st refused
    levels = [a[2] for a in client.emitted if a[0] == "dm-initiation-throttled"]
    assert "hard-stop" in levels


def test_throttle_reply_exempt(client):
    """Existing conversation (dm gateway has a thread row) never counts."""
    gw = MagicMock()
    gw.get_thread.return_value = [{"id": "x"}]
    client.dm_gateway = gw
    for _ in range(40):
        assert client._dm_initiation_allowed(PEER) is True
    assert len(client._dm_initiation_times) == 0
    assert not [a for a in client.emitted if a[0] == "dm-initiation-throttled"]


def test_throttle_blocks_publish_dm(client):
    """Hard-stop refuses the send BEFORE any optimistic row is created."""
    client._dm_initiation_times.extend([time.time()] * 30)
    assert client.publish_dm(PEER, "spam") is None
    assert client.published == []
    assert _relay_requests(client) == []


# ---------------------------------------------------------------------------
# P2 — lookup_nip05 discovery against a LOCAL http.server (no network).
# ---------------------------------------------------------------------------


class _Nip05Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.hits.append(self.path)
        entry = self.server.routes.get(self.path)
        if entry is None:
            self.send_error(404)
            return
        status, payload = entry
        if status in (301, 302, 307, 308):
            self.send_response(status)
            self.send_header("Location", payload)
            self.end_headers()
            return
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass  # keep pytest output clean


@pytest.fixture
def nip05_server(monkeypatch):
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Nip05Handler)
    srv.hits = []
    srv.routes = {}  # path -> (status, payload) ; 3xx payload = Location
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    monkeypatch.setattr(profile_nips, "_NIP05_SCHEME", "http")
    yield srv, f"127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_lookup_nip05_returns_pubkey_and_hints(nip05_server):
    srv, domain = nip05_server
    srv.routes["/.well-known/nostr.json?name=bob"] = (
        200,
        {
            "names": {"bob": BOB},
            "relays": {"bob": ["wss://hint.example", "http://bad"]},
        },
    )
    result = profile_nips.lookup_nip05(f"bob@{domain}")
    assert result == (BOB, ["wss://hint.example"])  # wss-only hints


def test_lookup_nip05_no_hints_key(nip05_server):
    srv, domain = nip05_server
    srv.routes["/.well-known/nostr.json?name=bob"] = (200, {"names": {"bob": BOB}})
    assert profile_nips.lookup_nip05(f"bob@{domain}") == (BOB, [])


def test_lookup_nip05_rejects_non_hex_name(nip05_server):
    srv, domain = nip05_server
    srv.routes["/.well-known/nostr.json?name=bob"] = (
        200,
        {"names": {"bob": "not-a-pubkey"}},
    )
    assert profile_nips.lookup_nip05(f"bob@{domain}") is None


def test_lookup_nip05_refuses_redirect(nip05_server):
    """NIP-05: MUST NOT follow redirects. The redirect target maps bob to a
    DIFFERENT key; if the client followed it, the lookup would wrongly
    succeed. It must return None instead."""
    srv, domain = nip05_server
    srv.routes["/.well-known/nostr.json?name=bob"] = (
        302,
        "/.well-known/nostr.json?name=attacker",
    )
    srv.routes["/.well-known/nostr.json?name=attacker"] = (
        200,
        {"names": {"attacker": BOB}},
    )
    assert profile_nips.lookup_nip05(f"bob@{domain}") is None
    # The target path was never fetched — the redirect was refused, not followed.
    assert "/.well-known/nostr.json?name=attacker" not in srv.hits


def test_lookup_nip05_bad_local_part_no_request(nip05_server):
    srv, domain = nip05_server
    for bad in ("bad!name", "UPPER", "sp ace", ""):
        assert profile_nips.lookup_nip05(f"{bad}@{domain}") is None
    assert srv.hits == []  # charset rejected client-side, zero requests


def test_lookup_nip05_malformed_identifier():
    assert profile_nips.lookup_nip05("") is None
    assert profile_nips.lookup_nip05("no-at-sign") is None
    assert profile_nips.lookup_nip05("@nolocal.example") is None


def test_lookup_nip05_404_is_none(nip05_server):
    srv, domain = nip05_server
    assert profile_nips.lookup_nip05(f"ghost@{domain}") is None
