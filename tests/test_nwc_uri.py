"""NWC connection-string parsing matrix (spec zaps-payments-v1 §3.1)."""

import json
import os

import pytest

from gnostr.wallet.nwc_uri import NWCUriError, parse_nwc_uri

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def vectors():
    with open(os.path.join(FIXTURES, "zap_test_vectors.json")) as f:
        return json.load(f)


# -- NWC URI parsing (spec §3.1) ---------------------------------------------


def test_parse_nwc_uri(vectors):
    cfg = parse_nwc_uri(vectors["v4_nwc_uri"])
    assert cfg["wallet_pubkey"] == vectors["keys"]["nwc_wallet_pub"]
    assert cfg["secret"] == vectors["keys"]["nwc_client_sec"]
    assert cfg["relay"] == "wss://relay.damus.io"
    assert cfg.get("lud16") == "alice@lnurl.example.com"


def test_parse_nwc_uri_rejects_garbage():
    for bad in [
        "",
        "http://x",
        "nostr+walletconnect://abc",
        "nostr+walletconnect://" + "a" * 63 + "?relay=wss://x&secret=" + "b" * 64,
        "nostr+walletconnect://" + "a" * 64 + "?relay=wss://x&secret=short",
    ]:
        with pytest.raises(NWCUriError):
            parse_nwc_uri(bad)


def test_parse_nwc_uri_live_env_optional(monkeypatch):
    """Optional live check against a real wallet endpoint; skipped unless
    TEST_NWC_URI is set (no node identity in the repo — James' rule)."""
    uri = os.environ.get("TEST_NWC_URI")
    if not uri:
        pytest.skip("TEST_NWC_URI not set")
    cfg = parse_nwc_uri(uri)
    assert cfg["secret"] and cfg["relay"].startswith("wss://")
