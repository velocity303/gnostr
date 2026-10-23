"""Zap receipt (kind 9735) verification tests (spec zaps-payments-v1 §1.4).

Every §1.4 rule gets a pass case against the v3_zap_receipt fixture plus a
tamper case (flip exactly one field, re-sign where the rule under test is
NOT the signature rule) proving the verifier rejects it. Plus: BOLT-11
amount decoder vectors (§4), rule-6 dedupe, aggregate sums incl. split
receipts, underpayment reject, and the persistence layer (zaps table).
"""

import copy
import hashlib
import json
import os
from unittest.mock import patch

import pytest

from gnostr.nostr_utils import sign_event
from gnostr.zaps.bolt11 import Bolt11Error, amount_msat, decode_invoice
from gnostr.zaps.receipt import (ZapReceiptError, aggregate_totals,
                                 process_9735, receipt_dedupe_key, verify_9735)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")

# BOLT-11 spec appendix decode vector (spec §4, from satoshi's survey).
BOLT11_SPEC_INVOICE = (
    "lnbc2500u1pvjluezpp5qqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfq"
    "ypqdq5xysxxatsyp3k7enxv4jsxqzpuaztrnwngzn3kdzw5hydlzf03qdgm2hdq27cqv3"
    "agm2awhz5se903vruatfhq77w3ls4evs3ch9zw97j25emudupq63nyw24cg27h2rspfj9srp"
)


@pytest.fixture(scope="module")
def vectors():
    with open(os.path.join(FIXTURES, "zap_test_vectors.json")) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def lnurlp_pk(vectors):
    return vectors["v1_lnurlp_metadata"]["nostrPubkey"]


@pytest.fixture
def receipt(vectors):
    """Fresh deep copy of the fixture receipt (tamper-safe)."""
    return copy.deepcopy(vectors["v3_zap_receipt"])


def _resign(event, sec):
    """Recompute id + BIP-340 sig after tampering (keeps rule 7 green)."""
    return sign_event(event, sec)


def _set_tag(event, name, value):
    for t in event["tags"]:
        if t[0] == name:
            t[1] = value
            return
    event["tags"].append([name, value])


def _drop_tag(event, name):
    event["tags"] = [t for t in event["tags"] if t[0] != name]


def _inner_request(receipt):
    for t in receipt["tags"]:
        if t[0] == "description":
            return json.loads(t[1])
    raise AssertionError("no description tag")


def _embed_request(receipt, request, vectors):
    """Put a (possibly re-signed) 9734 back into the description tag."""
    _set_tag(
        receipt,
        "description",
        json.dumps(request, separators=(",", ":"), ensure_ascii=False),
    )
    _drop_tag(receipt, "description-hash")
    return _resign(receipt, vectors["keys"]["lnurlp_nostr_sec"])


# --- happy path: the fixture passes every rule ------------------------------


def test_fixture_receipt_verifies(vectors, receipt, lnurlp_pk):
    info = verify_9735(receipt, lnurlp_pk)
    req = vectors["v2_zap_request"]
    assert info["request_id"] == req["id"]
    assert info["sender"] == vectors["keys"]["sender_pub"]
    assert info["recipient"] == vectors["keys"]["recipient_pub"]
    assert info["amount_msat"] == 1000000  # hrp 10u == request amount tag
    assert info["target_e"] == req["tags"][-2][1]  # e tag on the 9734
    assert info["comment"] == "Zap!"
    assert info["dedupe_key"]  # bolt11 present


def test_rule1_wrong_lnurl_pubkey_rejected(receipt, lnurlp_pk):
    with pytest.raises(ZapReceiptError, match="rule 1"):
        verify_9735(receipt, "ab" * 32)


def test_rule1_receipt_signed_by_wrong_key_rejected(vectors, receipt, lnurlp_pk):
    # LNURL server key swapped for the sender's: rule 7 still passes (the
    # event is self-consistent), rule 1 must catch the wrong attester.
    forged = receipt
    forged["pubkey"] = vectors["keys"]["sender_pub"]
    _resign(forged, vectors["keys"]["sender_sec"])
    with pytest.raises(ZapReceiptError, match="rule 1"):
        verify_9735(forged, lnurlp_pk)


def test_rule2_description_not_json_rejected(vectors, receipt, lnurlp_pk):
    _set_tag(receipt, "description", "not json")
    _resign(receipt, vectors["keys"]["lnurlp_nostr_sec"])
    with pytest.raises(ZapReceiptError, match="rule 2"):
        verify_9735(receipt, lnurlp_pk)


def test_rule2_inner_request_tampered_rejected(vectors, receipt, lnurlp_pk):
    req = _inner_request(receipt)
    req["content"] = "changed after signing"  # inner id/sig now broken
    _embed_request(receipt, req, vectors)
    with pytest.raises(ZapReceiptError, match="rule 2"):
        verify_9735(receipt, lnurlp_pk)


def test_rule2_description_wrong_kind_rejected(vectors, receipt, lnurlp_pk):
    req = _inner_request(receipt)
    req["kind"] = 1
    _embed_request(receipt, req, vectors)
    with pytest.raises(ZapReceiptError, match="rule 2"):
        verify_9735(receipt, lnurlp_pk)


def test_rule3_missing_bolt11_rejected(vectors, receipt, lnurlp_pk):
    _drop_tag(receipt, "bolt11")
    _resign(receipt, vectors["keys"]["lnurlp_nostr_sec"])
    with pytest.raises(ZapReceiptError, match="rule 3"):
        verify_9735(receipt, lnurlp_pk)


def test_rule3_underpayment_rejected(vectors, receipt, lnurlp_pk):
    # Request 2000 sats, invoice still pays 1000 sats: underpay -> reject.
    req = _inner_request(receipt)
    for t in req["tags"]:
        if t[0] == "amount":
            t[1] = "2000000"
    _resign(req, vectors["keys"]["sender_sec"])
    _embed_request(receipt, req, vectors)
    with pytest.raises(ZapReceiptError, match="rule 3"):
        verify_9735(receipt, lnurlp_pk)


def test_rule3_overpayment_accepted(vectors, receipt, lnurlp_pk):
    # Request 500 sats, invoice pays 1000 sats: overpay tolerated by spec.
    req = _inner_request(receipt)
    for t in req["tags"]:
        if t[0] == "amount":
            t[1] = "500000"
    _resign(req, vectors["keys"]["sender_sec"])
    _embed_request(receipt, req, vectors)
    info = verify_9735(receipt, lnurlp_pk)
    assert info["amount_msat"] == 1000000  # aggregate uses the INVOICE amount


def test_rule4_p_tag_mismatch_rejected(vectors, receipt, lnurlp_pk):
    _set_tag(receipt, "p", "cd" * 32)
    _resign(receipt, vectors["keys"]["lnurlp_nostr_sec"])
    with pytest.raises(ZapReceiptError, match="rule 4"):
        verify_9735(receipt, lnurlp_pk)


def test_rule4_P_tag_mismatch_rejected(vectors, receipt, lnurlp_pk):
    _set_tag(receipt, "P", "ef" * 32)
    _resign(receipt, vectors["keys"]["lnurlp_nostr_sec"])
    with pytest.raises(ZapReceiptError, match="rule 4"):
        verify_9735(receipt, lnurlp_pk)


def test_rule5_description_hash_pass(vectors, receipt, lnurlp_pk):
    desc = [t[1] for t in receipt["tags"] if t[0] == "description"][0]
    good = hashlib.sha256(desc.encode("utf-8")).hexdigest()
    _set_tag(receipt, "description-hash", good)
    _resign(receipt, vectors["keys"]["lnurlp_nostr_sec"])
    assert verify_9735(receipt, lnurlp_pk)["amount_msat"] == 1000000


def test_rule5_description_hash_mismatch_rejected(vectors, receipt, lnurlp_pk):
    _set_tag(receipt, "description-hash", "00" * 32)
    _resign(receipt, vectors["keys"]["lnurlp_nostr_sec"])
    with pytest.raises(ZapReceiptError, match="rule 5"):
        verify_9735(receipt, lnurlp_pk)


def test_rule7_event_id_tamper_rejected(receipt, lnurlp_pk):
    receipt["created_at"] += 1  # id no longer matches canonical serialization
    with pytest.raises(ZapReceiptError, match="rule 7"):
        verify_9735(receipt, lnurlp_pk)


def test_rule7_wrong_kind_rejected(receipt, lnurlp_pk):
    receipt["kind"] = 9734
    with pytest.raises(ZapReceiptError, match="rule 7"):
        verify_9735(receipt, lnurlp_pk)


# --- BOLT-11 amount decoder vectors (spec §4) --------------------------------


def test_bolt11_hrp_vectors(vectors):
    # Spec vectors are sats -> hrp amount; the decoder must invert exactly.
    assert vectors["bolt11_hrp_21000_sats"] == "210u"
    assert vectors["bolt11_hrp_1000_sats"] == "10u"
    # Invoices carrying those hrps decode back to the sats (x1000 = msat).
    assert amount_msat("lnbc210u1pq00000") == 21000 * 1000
    assert amount_msat("lnbc10u1pq00000") == 1000 * 1000


def test_bolt11_spec_decode_vector():
    d = decode_invoice(BOLT11_SPEC_INVOICE)
    assert d["network"] == "mainnet"
    assert d["amount_msat"] == 250000 * 1000  # 250000 sat per spec §4
    assert d["timestamp"] == 1496314658
    assert (
        d["payment_hash"]
        == "0001020304050607080900010203040506070809000102030405060708090102"
    )
    assert d["description"] == "1 cup coffee"


def test_bolt11_junk_rejected():
    for junk in ("", "lnbc", "not-an-invoice", "lnbcXXu1abc"):
        with pytest.raises(Bolt11Error):
            decode_invoice(junk)


# --- rule 6: dedupe + aggregates ---------------------------------------------


def _fake_verified(v, bolt11, request_id, target, msat, recipient=None):
    return {
        "event_id": hashlib.sha256(bolt11.encode()).hexdigest(),
        "request_id": request_id,
        "target_e": target,
        "target_k": "1",
        "recipient": recipient or v["keys"]["recipient_pub"],
        "sender": v["keys"]["sender_pub"],
        "bolt11": bolt11,
        "amount_msat": msat,
        "comment": "",
        "dedupe_key": bolt11 or request_id,
        "ts": 1763136060,
    }


def test_dedupe_key_prefers_bolt11(vectors, receipt):
    assert (
        receipt_dedupe_key(receipt)
        == [t[1] for t in receipt["tags"] if t[0] == "bolt11"][0]
    )
    no_bolt = copy.deepcopy(receipt)
    _drop_tag(no_bolt, "bolt11")
    assert receipt_dedupe_key(no_bolt) == vectors["v2_zap_request"]["id"]


def test_aggregate_dedupes_relay_echo(vectors):
    a = _fake_verified(vectors, "lnbcA", "req1", "evt1", 1000000)
    echo = dict(a, event_id="ff" * 32)  # same bolt11, re-echoed event
    totals = aggregate_totals([a, echo])
    assert totals["by_event"]["evt1"] == 1000000  # counted ONCE


def test_aggregate_split_receipts_sum(vectors):
    # Two distinct receipts (different bolt11) for ONE zap request:
    # render once, SUM for the aggregate (spec rule 6).
    s1 = _fake_verified(vectors, "lnbcS1", "req1", "evt1", 600000)
    s2 = _fake_verified(vectors, "lnbcS2", "req1", "evt1", 400000)
    totals = aggregate_totals([s1, s2])
    assert totals["by_event"]["evt1"] == 1000000
    assert totals["by_profile"][vectors["keys"]["recipient_pub"]] == 1000000


# --- process_9735: gate + persistence ----------------------------------------


@pytest.fixture
def db():
    """Real Database over a temp dir (full init_db schema, incl. zaps)."""
    import tempfile

    from gnostr.database import Database

    tmp = tempfile.mkdtemp()

    class FakeGLib:
        @staticmethod
        def get_user_data_dir():
            return tmp

    with patch("gnostr.database.GLib", FakeGLib):
        d = Database()
    yield d
    d.conn.close()


def test_process_9735_persists_verified(vectors, receipt, lnurlp_pk, db):
    info, reason = process_9735(receipt, db, metadata_lookup=lambda pk: lnurlp_pk)
    assert reason is None and info["amount_msat"] == 1000000
    rows = db.zaps_received(vectors["keys"]["recipient_pub"])
    assert len(rows) == 1
    assert rows[0]["msat"] == 1000000
    assert rows[0]["sender"] == vectors["keys"]["sender_pub"]


def test_process_9735_rejects_without_metadata(vectors, receipt, db):
    # Rule 1 is mandatory: no LNURL metadata -> hard reject, nothing stored.
    info, reason = process_9735(receipt, db, metadata_lookup=lambda pk: None)
    assert info is None and "rule 1" in reason
    assert db.zaps_received(vectors["keys"]["recipient_pub"]) == []


def test_save_zap_dedupes_relay_echo(vectors, receipt, lnurlp_pk, db):
    info, _ = process_9735(receipt, db, metadata_lookup=lambda pk: lnurlp_pk)
    # Same receipt again (relay echo): INSERT OR IGNORE on both dedupe keys.
    db.save_zap(info)
    db.save_zap(dict(info, event_id="ab" * 32))  # same bolt11, new event id
    assert len(db.zaps_received(vectors["keys"]["recipient_pub"])) == 1


def test_db_aggregates_and_lists(vectors, db):
    t = vectors["v2_zap_request"]["tags"][-2][1]
    db.save_zap(_fake_verified(vectors, "lnbcA", "req1", t, 1000000))
    db.save_zap(_fake_verified(vectors, "lnbcB", "req2", t, 500000))
    assert db.zap_totals_for_events([t]) == {t: 1500000}
    assert db.zap_totals_for_profiles([vectors["keys"]["recipient_pub"]]) == {
        vectors["keys"]["recipient_pub"]: 1500000
    }
    rows = db.zaps_for_event(t)
    assert sorted(r["msat"] for r in rows) == [500000, 1000000]
    assert db.zap_totals_for_events(["nope"]) == {}
