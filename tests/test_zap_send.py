"""Zap send tests (spec zaps-payments-v1 §1.1-1.3, §3.3-3.4).

Byte-exact bindings to tests/fixtures/zap_test_vectors.json: the 9734 the
builder produces must reproduce the fixture's id/sig/canonical-JSON/
callback-URL, so a tag-order or serialization drift fails loudly.
"""

import json
import os
import sqlite3
import threading
from unittest.mock import patch

import pytest

from gnostr.nostr_utils import sign_event
from gnostr.wallet.backend import QuotaExceededError
from gnostr.zaps.lnurl import ZapError, clear_metadata_cache
from gnostr.zaps.request import (
    build_9734,
    build_callback_url,
    canonical_event_json,
    verify_event_sig,
)
from gnostr.zaps.send import ZapCancelled, ZapFlow

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture(scope="module")
def vectors():
    with open(os.path.join(FIXTURES, "zap_test_vectors.json")) as f:
        return json.load(f)


@pytest.fixture
def db():
    """Minimal ledger db (same shape as test_zap_budget)."""
    from gnostr.database import Database

    d = Database.__new__(Database)
    d.conn = sqlite3.connect(":memory:", check_same_thread=False)
    d.lock = threading.Lock()
    cur = d.conn.cursor()
    cur.execute(
        "CREATE TABLE paid_invoices (bolt11 TEXT PRIMARY KEY, paid_at INTEGER,"
        " amount_msat INTEGER)"
    )
    cur.execute("CREATE TABLE zap_ledger (month TEXT, bolt11 TEXT, sats INTEGER)")
    d.conn.commit()
    return d


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_metadata_cache()
    yield
    clear_metadata_cache()


# -- §1.3 kind-9734 builder (byte-exact fixture bindings) ----------------------------


def _fixture_9734(vectors):
    return build_9734(
        vectors["keys"]["sender_pub"],
        vectors["keys"]["recipient_pub"],
        1000000,
        vectors["lnurl_bech32"],
        ["wss://relay.damus.io", "wss://nos.lol"],
        event=("9ae37aa68f48645127299e9453eb5d908a0cbb6058ff340d528ed4d37c8994fb", 1),
        comment="Zap!",
        created_at=1763136000,
        priv_key_hex=vectors["keys"]["sender_sec"],
    )


def test_build_9734_tags_exact(vectors):
    ev = _fixture_9734(vectors)
    assert ev["tags"] == vectors["v2_zap_request"]["tags"]
    assert ev["kind"] == 9734
    assert ev["content"] == "Zap!"


def test_build_9734_signature_valid_over_fixture_id(vectors):
    # The fixture sig was generated with random aux (BIP-340 allows any aux;
    # only the VERIFICATION is deterministic). Binding: the builder must
    # reproduce the fixture id byte-exact and produce a sig that verifies.
    ev = _fixture_9734(vectors)
    assert ev["id"] == vectors["v2_zap_request"]["id"]
    assert verify_event_sig(ev) is True


def test_canonical_json_matches_fixture(vectors):
    fixture_ev = vectors["v2_zap_request"]
    assert canonical_event_json(fixture_ev) == vectors["v2_zap_request_canonical_json"]


def test_callback_url_matches_fixture(vectors):
    fixture_ev = vectors["v2_zap_request"]
    assert (
        build_callback_url(
            vectors["v1_lnurlp_metadata"]["callback"],
            fixture_ev,
            vectors["lnurl_bech32"],
        )
        == vectors["v2_callback_url"]
    )


def test_profile_zap_has_no_e_k_tags(vectors):
    ev = build_9734(
        vectors["keys"]["sender_pub"],
        vectors["keys"]["recipient_pub"],
        5000,
        vectors["lnurl_bech32"],
        ["wss://relay.damus.io"],
        event=None,
        created_at=1763136000,
        priv_key_hex=vectors["keys"]["sender_sec"],
    )
    tag_names = [t[0] for t in ev["tags"]]
    assert tag_names == ["relays", "amount", "lnurl", "p"]


def test_verify_event_sig_fixtures(vectors):
    assert verify_event_sig(vectors["v2_zap_request"]) is True
    assert verify_event_sig(vectors["v3_zap_receipt"]) is True
    tampered = dict(vectors["v2_zap_request"])
    tampered["content"] = "not a zap"
    assert verify_event_sig(tampered) is False


# -- §3.3/3.4 ZapFlow -----------------------------------------------------------------


class FakeBackend:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result or {"preimage": "abc", "paid": True}
        self.error = error

    def pay_invoice(self, bolt11):
        self.calls.append(bolt11)
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def flow_parts(vectors, db):
    def signer(ev):
        return sign_event(ev, vectors["keys"]["sender_sec"])

    flow = ZapFlow(
        backend=None,  # set per-test
        db=db,
        write_relays=lambda: ["wss://relay.damus.io", "wss://nos.lol"],
        signer=signer,
        sender_pubkey=vectors["keys"]["sender_pub"],
        recipient_pubkey=vectors["keys"]["recipient_pub"],
        event=("9ae37aa68f48645127299e9453eb5d908a0cbb6058ff340d528ed4d37c8994fb", 1),
        budget_sats=100000,
    )
    meta = {
        "callback": vectors["v1_lnurlp_metadata"]["callback"],
        "nostrPubkey": vectors["keys"]["lnurlp_nostr_pub"],
        "minSendable": 1000,
        "maxSendable": 100000000,
        "commentAllowed": 280,
        "domain": "lnurl.example.com",
    }
    return flow, meta


def test_flow_prepare_builds_signed_request(vectors, flow_parts, monkeypatch):
    flow, meta = flow_parts
    fixture_invoice = json.loads(vectors["v4_nwc_request_plaintext"])["params"][
        "invoice"
    ]
    seen = {}

    def fake_fetch(callback, signed_event, lnurl_bech32, comment=None, **kw):
        seen["event"] = signed_event
        return {"pr": fixture_invoice, "successAction": None}

    monkeypatch.setattr("gnostr.zaps.send.fetch_invoice", fake_fetch)
    prepared = flow.prepare(vectors["lnurl_bech32"], meta, 1000000, "Zap!")
    # the 9734 that reached the callback carries the fixture's exact tags
    # (created_at is live, so the id differs from the frozen fixture) and a
    # valid signature over its own id
    assert seen["event"]["tags"] == vectors["v2_zap_request"]["tags"]
    assert verify_event_sig(seen["event"]) is True
    assert prepared["bolt11"] == fixture_invoice
    assert prepared["sats"] == 1000  # hrp '10u' from the fixture invoice


def test_flow_pay_emits_exactly_one_payment(vectors, flow_parts, monkeypatch):
    flow, meta = flow_parts
    backend = FakeBackend()
    flow.backend = backend
    fixture_invoice = json.loads(vectors["v4_nwc_request_plaintext"])["params"][
        "invoice"
    ]
    monkeypatch.setattr(
        "gnostr.zaps.send.fetch_invoice",
        lambda *a, **k: {"pr": fixture_invoice},
    )
    result = flow.run(
        vectors["lnurl_bech32"],
        meta,
        1000000,
        "alice",
        comment="Zap!",
        confirm=lambda s: True,
    )
    assert backend.calls == [fixture_invoice]
    assert result["paid"] is True


def test_flow_cancel_pays_nothing(vectors, flow_parts, monkeypatch):
    flow, meta = flow_parts
    backend = FakeBackend()
    flow.backend = backend
    fixture_invoice = json.loads(vectors["v4_nwc_request_plaintext"])["params"][
        "invoice"
    ]
    monkeypatch.setattr(
        "gnostr.zaps.send.fetch_invoice",
        lambda *a, **k: {"pr": fixture_invoice},
    )
    with pytest.raises(ZapCancelled):
        flow.run(
            vectors["lnurl_bech32"],
            meta,
            1000000,
            "alice",
            confirm=lambda s: False,
        )
    assert backend.calls == []


def test_flow_budget_blocks_before_pay(vectors, flow_parts, monkeypatch, db):
    from gnostr.wallet.budget import record_payment

    record_payment(db, "lnbc99999u1p" + "x" * 10, 99_500)  # over the 100k cap
    flow, meta = flow_parts
    backend = FakeBackend()
    flow.backend = backend
    fixture_invoice = json.loads(vectors["v4_nwc_request_plaintext"])["params"][
        "invoice"
    ]
    monkeypatch.setattr(
        "gnostr.zaps.send.fetch_invoice",
        lambda *a, **k: {"pr": fixture_invoice},
    )
    with pytest.raises(QuotaExceededError):
        flow.run(
            vectors["lnurl_bech32"],
            meta,
            1000000,
            "alice",
            confirm=lambda s: True,
        )
    assert backend.calls == []  # blocked BEFORE the wallet call


def test_flow_wallet_error_propagates(vectors, flow_parts, monkeypatch):
    from gnostr.wallet.backend import PaymentFailedError

    flow, meta = flow_parts
    flow.backend = FakeBackend(error=PaymentFailedError("no route"))
    fixture_invoice = json.loads(vectors["v4_nwc_request_plaintext"])["params"][
        "invoice"
    ]
    monkeypatch.setattr(
        "gnostr.zaps.send.fetch_invoice",
        lambda *a, **k: {"pr": fixture_invoice},
    )
    with pytest.raises(PaymentFailedError):
        flow.run(
            vectors["lnurl_bech32"],
            meta,
            1000000,
            "alice",
            confirm=lambda s: True,
        )
