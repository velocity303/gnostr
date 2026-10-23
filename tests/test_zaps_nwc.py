"""Zaps / NWC wallet tests (spec zaps-payments-v1 test vectors).

Covers: NWC URI parsing, NIP-04 legacy crypto (pure-Python AES), NIP-44
request/response round trips against the fixture events, the fake-wallet
backend round trip (capability gate, error mapping, double-pay guard),
the budget ledger math, and the Database zap tables.
"""

import json
import os
import sqlite3
import threading

import pytest

from gnostr import nip44
from gnostr.wallet import nip04
from gnostr.wallet.backend import (
    InsufficientBalanceError,
    PaymentFailedError,
    QuotaExceededError,
    WalletCapabilityError,
    WalletError,
    WalletTimeoutError,
    error_from_response,
)
from gnostr.wallet.budget import (
    bolt11_amount_sats,
    check_budget,
    month_key,
    month_spent_sats,
    record_payment,
)
from gnostr.wallet.nwc import NWCBackend
from gnostr.wallet.nwc_uri import NWCUriError, parse_nwc_uri

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def vectors():
    with open(os.path.join(FIXTURES, "zap_test_vectors.json")) as f:
        return json.load(f)


@pytest.fixture
def db(vectors):
    """Real Database against a temp sqlite file (bypasses GLib data dir)."""
    from gnostr.database import Database

    d = Database.__new__(Database)
    d.conn = sqlite3.connect(":memory:", check_same_thread=False)
    d.lock = threading.Lock()
    cur = d.conn.cursor()
    cur.execute(
        "CREATE TABLE paid_invoices (bolt11 TEXT PRIMARY KEY, paid_at INTEGER,"
        " amount_msat INTEGER)"
    )
    cur.execute(
        "CREATE TABLE zap_ledger (month TEXT, bolt11 TEXT, sats INTEGER)"
    )
    d.conn.commit()
    return d


# -- NWC URI parsing (spec §3.1) ---------------------------------------------


def test_parse_nwc_uri(vectors):
    cfg = parse_nwc_uri(vectors["v4_nwc_uri"])
    assert cfg["wallet_pubkey"] == vectors["keys"]["nwc_wallet_pub"]
    assert cfg["secret"] == vectors["keys"]["nwc_client_sec"]
    assert cfg["relay"] == "wss://relay.damus.io"
    assert cfg.get("lud16") == "alice@lnurl.example.com"


def test_parse_nwc_uri_rejects_garbage():
    for bad in ["", "http://x", "nostr+walletconnect://abc",
                "nostr+walletconnect://" + "a" * 63 + "?relay=wss://x&secret=" + "b" * 64,
                "nostr+walletconnect://" + "a" * 64 + "?relay=wss://x&secret=short"]:
        with pytest.raises(NWCUriError):
            parse_nwc_uri(bad)


# -- NIP-04 legacy (spec §2.3 fallback path) ----------------------------------


def test_nip04_fixture_request(vectors):
    plain = nip04.nip04_decrypt(
        vectors["v4_nwc_nip04_request_payload"],
        vectors["keys"]["nwc_client_sec"],
        vectors["keys"]["nwc_wallet_pub"],
    )
    assert json.loads(plain) == json.loads(vectors["v4_nwc_request_plaintext"])


def test_nip04_fixture_response(vectors):
    plain = nip04.nip04_decrypt(
        vectors["v4_nwc_nip04_response_payload"],
        vectors["keys"]["nwc_wallet_sec"],
        vectors["keys"]["nwc_client_pub"],
    )
    assert json.loads(plain) == json.loads(vectors["v4_nwc_response_plaintext"])


def test_nip04_roundtrip():
    priv = "11" * 32
    pub = "22" * 32
    from gnostr.nostr_utils import get_public_key

    pub = get_public_key("22" * 32)
    ct = nip04.nip04_encrypt("hello zap", priv, pub)
    assert nip04.nip04_decrypt(ct, priv, pub) == "hello zap"


# -- NIP-44 fixture events (spec §2.3 primary path) ----------------------------


def test_nip44_fixture_request(vectors):
    conv = nip44.get_conversation_key(
        bytes.fromhex(vectors["keys"]["nwc_client_sec"]),
        bytes.fromhex(vectors["keys"]["nwc_wallet_pub"]),
    )
    plain = nip44.decrypt(vectors["v4_nwc_request_event"]["content"], conv)
    assert json.loads(plain) == json.loads(vectors["v4_nwc_request_plaintext"])


def test_nip44_fixture_response(vectors):
    conv = nip44.get_conversation_key(
        bytes.fromhex(vectors["keys"]["nwc_wallet_sec"]),
        bytes.fromhex(vectors["keys"]["nwc_client_pub"]),
    )
    plain = nip44.decrypt(vectors["v4_nwc_response_event"]["content"], conv)
    assert json.loads(plain) == json.loads(vectors["v4_nwc_response_plaintext"])


# -- error mapping (spec §2.6) ---------------------------------------------------


def test_error_from_response_codes():
    assert isinstance(error_from_response({"code": "QUOTA_EXCEEDED", "message": "x"}),
                      QuotaExceededError)
    assert isinstance(error_from_response({"code": "INSUFFICIENT_BALANCE"}),
                      InsufficientBalanceError)
    assert isinstance(error_from_response({"code": "PAYMENT_FAILED"}),
                      PaymentFailedError)
    assert isinstance(error_from_response("INTERNAL"), WalletError)
    assert not isinstance(error_from_response("INTERNAL"),
                          (QuotaExceededError, InsufficientBalanceError,
                           PaymentFailedError))


# -- fake wallet transport: full backend round trip ------------------------------


class FakeWallet:
    """In-process NIP-47 wallet service over the injectable transport seam.
    Speaks the relay wire protocol: [REQ...] / [EVENT...] frames."""

    def __init__(self, vectors, encryption="nip44", fail=None,
                 publish_info=True, settled=False, silent=False):
        self.wallet_sec = vectors["keys"]["nwc_wallet_sec"]
        self.wallet_pub = vectors["keys"]["nwc_wallet_pub"]
        self.encryption = encryption
        self.fail = fail  # error code to return instead of success
        self.settled = settled
        self.silent = silent  # never respond (timeout tests)
        self.paid = []
        self.events = []  # frames the backend sent
        self._on_event = None
        self._publish_info_flag = publish_info
        self.info_event = vectors["v4_nwc_info_event"]

    def transport_factory(self, url, on_event, on_connected):
        self._on_event = on_event

        class T:
            def start(inner):
                on_connected()

            def send(inner, payload):
                self.events.append(payload)
                # auto-respond to published 23194 requests, like a live wallet
                if (isinstance(payload, list) and payload[0] == "EVENT"
                        and payload[1].get("kind") == 23194 and not self.silent):
                    threading.Timer(0.01, self.respond, args=(payload[1],)).start()
                # answer info-event REQs with the info event
                if (isinstance(payload, list) and payload[0] == "REQ"
                        and self._publish_info_flag
                        and payload[2].get("kinds") == [13194]):
                    threading.Timer(0.01, self.deliver_info).start()

            def close(inner):
                pass

        return T()

    def deliver_info(self):
        if self._publish_info_flag:
            self._on_event(self.info_event)

    def _conv(self, client_pub):
        if self.encryption == "nip04":
            return None
        return nip44.get_conversation_key(
            bytes.fromhex(self.wallet_sec), bytes.fromhex(client_pub)
        )

    def respond(self, request):
        """Decrypt a 23194 request and deliver the matching 23195 reply."""
        client_pub = request["pubkey"]
        if self.encryption == "nip04":
            plain = nip04.nip04_decrypt(
                request["content"], self.wallet_sec, client_pub
            )
        else:
            plain = nip44.decrypt(request["content"], self._conv(client_pub))
        req = json.loads(plain)
        method = req["method"]
        if self.fail:
            body = {"result_type": method,
                    "error": {"code": self.fail, "message": self.fail},
                    "result": None}
        elif method == "pay_invoice":
            self.paid.append(req["params"]["invoice"])
            body = {"result_type": method, "error": None,
                    "result": {"preimage": "ab" * 32, "fees_paid_msat": 0}}
        elif method == "lookup_invoice":
            body = {"result_type": method, "error": None,
                    "result": {"settled": self.settled}}
        elif method == "get_balance":
            body = {"result_type": method, "error": None,
                    "result": {"balance_msat": 21_000_000_000}}
        elif method == "make_invoice":
            body = {"result_type": method, "error": None,
                    "result": {"invoice": "lnbc10u...", "payment_hash": "cd" * 32,
                               "created_at": 1763136000}}
        elif method == "get_info":
            body = {"result_type": method, "error": None,
                    "result": {"alias": "alice", "color": "#f7931a",
                               "methods": ["pay_invoice"], "network": "bitcoin"}}
        else:
            body = {"result_type": method, "error":
                    {"code": "NOT_IMPLEMENTED", "message": "nope"}, "result": None}
        content = json.dumps(body, separators=(",", ":"))
        if self.encryption == "nip04":
            content = nip04.nip04_encrypt(content, self.wallet_sec, client_pub)
        else:
            content = nip44.encrypt(content, self._conv(client_pub))
        response = {
            "kind": 23195,
            "pubkey": self.wallet_pub,
            "content": content,
            "tags": [["p", client_pub], ["e", request["id"]]],
        }
        self._on_event(response)


def _backend(vectors, wallet):
    return NWCBackend(vectors["v4_nwc_uri"],
                      transport_factory=wallet.transport_factory,
                      timeout_s=2.0)


def test_backend_nip44_roundtrip(vectors):
    wallet = FakeWallet(vectors)
    be = _backend(vectors, wallet)
    be._encryption = "nip44"
    be._capabilities = ["pay_invoice", "get_balance", "lookup_invoice"]
    assert be.get_balance() == {"balance_msat": 21_000_000_000}


def test_backend_pay_invoice(vectors):
    wallet = FakeWallet(vectors)
    be = _backend(vectors, wallet)
    be._encryption = "nip44"
    be._capabilities = ["pay_invoice", "lookup_invoice"]
    bolt11 = vectors["bolt11_hrp_21000_sats"] + "1q" + "0" * 500
    result = be.pay_invoice(bolt11)
    assert result["preimage"] == "ab" * 32
    assert wallet.paid == [bolt11]


def test_backend_error_mapping(vectors):
    wallet = FakeWallet(vectors, fail="INSUFFICIENT_BALANCE")
    be = _backend(vectors, wallet)
    be._encryption = "nip44"
    be._capabilities = ["pay_invoice", "lookup_invoice"]
    with pytest.raises(InsufficientBalanceError):
        be.call("pay_invoice", {"invoice": "lnbc..."})


def test_backend_capability_gate(vectors):
    wallet = FakeWallet(vectors)
    be = _backend(vectors, wallet)
    be._capabilities = ["get_balance"]
    with pytest.raises(WalletCapabilityError):
        be.call("pay_invoice", {"invoice": "lnbc..."})
    with pytest.raises(WalletCapabilityError):
        be.call("rebrand", {})  # not even a v1 method


def test_backend_timeout(vectors):
    wallet = FakeWallet(vectors, silent=True)
    be = _backend(vectors, wallet)
    be.timeout_s = 0.2
    be._encryption = "nip44"
    be._capabilities = ["get_balance"]
    with pytest.raises(WalletTimeoutError):
        be.get_balance()  # fake wallet never responds


def test_backend_info_event_sets_encryption(vectors):
    wallet = FakeWallet(vectors, encryption="nip04")
    be = _backend(vectors, wallet)
    info = be.fetch_info_event()
    # fixture info event advertises "nip44_v2 nip04" -> nip44 preferred
    assert be._encryption == "nip44"
    assert "pay_invoice" in (info or {}).get("methods", [])


def test_backend_no_info_event_falls_back_nip04(vectors):
    wallet = FakeWallet(vectors, publish_info=False)
    be = _backend(vectors, wallet)
    assert be.fetch_info_event() is None
    assert be._encryption == "nip04"


# -- double-pay guard (spec §3.3) -------------------------------------------------


def test_pay_twice_is_noop(vectors, db):
    wallet = FakeWallet(vectors)
    be = _backend(vectors, wallet)
    be.db = db
    be._encryption = "nip44"
    be._capabilities = ["pay_invoice", "lookup_invoice"]
    bolt11 = vectors["bolt11_hrp_1000_sats"] + "1q" + "0" * 500
    result = be.pay_invoice(bolt11)
    assert result["preimage"] == "ab" * 32
    assert wallet.paid == [bolt11]
    assert db.is_invoice_paid(bolt11)
    # second pay: short-circuits on the persisted set, wallet never sees it
    result = be.pay_invoice(bolt11)
    assert result.get("already_paid") is True
    assert wallet.paid == [bolt11]


def test_lookup_settled_skips_pay(vectors):
    wallet = FakeWallet(vectors, settled=True)
    be = _backend(vectors, wallet)
    be._encryption = "nip44"
    be._capabilities = ["pay_invoice", "lookup_invoice"]
    bolt11 = vectors["bolt11_hrp_1000_sats"] + "1q" + "0" * 500
    result = be.pay_invoice(bolt11)
    assert result.get("already_paid") is True
    assert wallet.paid == []  # pay_invoice never sent


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
