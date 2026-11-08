"""Zap budget ledger tests (spec zaps-payments-v1 §2.5)."""

import os
import sqlite3
import threading

import pytest

from gnostr.wallet.backend import QuotaExceededError
from gnostr.wallet.budget import (bolt11_amount_sats, check_budget, month_key,
                                  month_spent_sats, record_payment)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def vectors():
    import json

    with open(os.path.join(FIXTURES, "zap_test_vectors.json")) as f:
        return json.load(f)


@pytest.fixture
def db():
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


# -- budget (spec §2.5) --------------------------------------------------------------


def test_bolt11_amount_parsing(vectors):
    # fixture hrp values: 21000 sats = '210u', 1000 sats = '10u'
    assert bolt11_amount_sats("lnbc210u1p" + "x" * 10) == 21000
    assert bolt11_amount_sats("lnbc10u1p" + "x" * 10) == 1000
    assert bolt11_amount_sats("lnbc100m1p" + "x" * 10) == 10_000_000  # 0.1 BTC
    assert bolt11_amount_sats("lnbc" + "x" * 10) is None  # no amount
    assert bolt11_amount_sats("notlnbc1u") is None


def test_budget_check(db):
    bolt11 = "lnbc1000n1p" + "x" * 20
    record_payment(db, bolt11, 1000, 1_000_000)
    assert month_spent_sats(db) == 1000
    check_budget(db, 500, 2000)  # 1000+500 <= 2000 ok
    with pytest.raises(QuotaExceededError):
        check_budget(db, 1500, 2000)


def test_record_payment_idempotent(db):
    bolt11 = "lnbc500n1p" + "x" * 20
    record_payment(db, bolt11, 500, 500_000)
    record_payment(db, bolt11, 500, 500_000)  # duplicate must not double-count
    assert month_spent_sats(db) == 500


def test_month_key_format():
    assert len(month_key(1763136000).split("-")) == 2
