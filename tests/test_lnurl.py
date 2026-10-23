"""LNURL-pay target resolution + metadata gate tests (spec §1.1-1.2).

Includes an end-to-end pass over a stdlib http.server fake LNURL endpoint
serving the fixture metadata — exercises the real _http_get_json /
proxy_opener path (no monkeypatch), the same code .onion LNURLs use over
the Tor SOCKS5 endpoint.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from gnostr.zaps import lnurl as lnurl_mod
from gnostr.zaps.lnurl import (
    NotZapableError,
    ProfileSharingRequired,
    clear_metadata_cache,
    fetch_metadata,
    lnurl_to_url,
    resolve_target,
    url_to_lnurl,
    zapable_profile,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture(scope="module")
def vectors():
    with open(os.path.join(FIXTURES, "zap_test_vectors.json")) as f:
        return json.load(f)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_metadata_cache()
    yield
    clear_metadata_cache()


# -- §1.1 target resolution ----------------------------------------------------------


def test_resolve_lud16_matches_fixture(vectors):
    # alice@lnurl.example.com -> the fixture's exact bech32 lnurl
    got = resolve_target({"lud16": "alice@lnurl.example.com"})
    assert got == vectors["lnurl_bech32"]
    assert lnurl_to_url(got) == "https://lnurl.example.com/.well-known/lnurlp/alice"


def test_resolve_lud06_passthrough(vectors):
    prof = {"lud06": vectors["lnurl_bech32"]}
    assert resolve_target(prof) == vectors["lnurl_bech32"]


def test_resolve_lud16_precedence_over_lud06(vectors):
    prof = {"lud16": "alice@lnurl.example.com", "lud06": "lnurl1invalid"}
    assert resolve_target(prof) == vectors["lnurl_bech32"]


def test_resolve_nip05_fallback():
    # CONVENTION (spec §1.1 step 3): nip05 user@domain doubles as lud16
    got = resolve_target({"nip05": "alice@lnurl.example.com"})
    assert lnurl_to_url(got) == "https://lnurl.example.com/.well-known/lnurlp/alice"


def test_resolve_nip05_underscore_skipped():
    # "_" is the NIP-05 standard local-part, not a lightning identity
    assert resolve_target({"nip05": "_@example.com"}) is None


def test_resolve_onion_uses_http():
    # LUD-16 allows onion services; they are served over plain http and
    # reach the user through proxy_opener's Tor SOCKS5 path.
    got = resolve_target({"lud16": "alice@examp1e2onion.onion"})
    assert lnurl_to_url(got) == "http://examp1e2onion.onion/.well-known/lnurlp/alice"


def test_resolve_not_zapable():
    assert resolve_target({}) is None
    assert resolve_target({"name": "alice"}) is None
    assert resolve_target("not json") is None
    assert resolve_target({"lud06": "garbage"}) is None
    assert resolve_target({"lud16": "no-at-sign"}) is None


def test_zapable_profile_gate(vectors):
    assert zapable_profile({"lud16": "alice@lnurl.example.com"}) is True
    assert zapable_profile({"lud06": vectors["lnurl_bech32"]}) is True
    assert zapable_profile({"name": "x"}) is False
    assert zapable_profile(None) is False


def test_url_to_lnurl_roundtrip(vectors):
    assert (
        url_to_lnurl("https://lnurl.example.com/.well-known/lnurlp/alice")
        == vectors["lnurl_bech32"]
    )


# -- §1.2 metadata gate (unit matrix) -------------------------------------------------


def _meta_body(vectors, **overrides):
    body = dict(vectors["v1_lnurlp_metadata"])
    body.update(overrides)
    return body


def test_fetch_metadata_accepts_fixture(vectors):
    from unittest.mock import patch

    with patch.object(lnurl_mod, "_http_get_json", return_value=_meta_body(vectors)):
        meta = fetch_metadata(vectors["lnurl_bech32"])
    assert meta["nostrPubkey"] == vectors["keys"]["lnurlp_nostr_pub"]
    assert meta["minSendable"] == 1000
    assert meta["maxSendable"] == 100000000
    assert meta["commentAllowed"] == 280
    assert meta["domain"] == "lnurl.example.com"
    # optional-but-allowed payerData must NOT abort (all mandatory=false)


def test_fetch_metadata_requires_allows_nostr(vectors):
    from unittest.mock import patch

    body = _meta_body(vectors, allowsNostr=False)
    with patch.object(lnurl_mod, "_http_get_json", return_value=body):
        with pytest.raises(NotZapableError):
            fetch_metadata(vectors["lnurl_bech32"])


def test_fetch_metadata_requires_hex_pubkey(vectors):
    from unittest.mock import patch

    body = _meta_body(vectors, nostrPubkey="not-hex")
    with patch.object(lnurl_mod, "_http_get_json", return_value=body):
        with pytest.raises(NotZapableError):
            fetch_metadata(vectors["lnurl_bech32"])


def test_fetch_metadata_rejects_wrong_tag(vectors):
    from unittest.mock import patch

    body = _meta_body(vectors, tag="login")
    with patch.object(lnurl_mod, "_http_get_json", return_value=body):
        with pytest.raises(NotZapableError):
            fetch_metadata(vectors["lnurl_bech32"])


def test_fetch_metadata_mandatory_payer_data_aborts(vectors):
    from unittest.mock import patch

    body = _meta_body(vectors, payerData={"name": {"mandatory": True}})
    with patch.object(lnurl_mod, "_http_get_json", return_value=body):
        with pytest.raises(ProfileSharingRequired):
            fetch_metadata(vectors["lnurl_bech32"])


def test_fetch_metadata_cached(vectors):
    from unittest.mock import patch

    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return _meta_body(vectors)

    with patch.object(lnurl_mod, "_http_get_json", side_effect=fake_get):
        fetch_metadata(vectors["lnurl_bech32"])
        fetch_metadata(vectors["lnurl_bech32"])
    assert len(calls) == 1


# -- §1.2 end-to-end over a fake LNURL server (real HTTP, real opener) ----------------


class _FakeLnurlHandler(BaseHTTPRequestHandler):
    body_json = "{}"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body_json.encode())

    def log_message(self, format, *args):
        pass  # keep pytest output clean


@pytest.fixture
def fake_lnurl_server():
    srv = HTTPServer(("127.0.0.1", 0), _FakeLnurlHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


def test_fetch_metadata_end_to_end(vectors, fake_lnurl_server):
    """Real socket round-trip through _http_get_json + proxy_opener — the
    same path an .onion LNURL takes over Tor. settings dict pins the proxy
    off (the test env's GSettings is a Mock)."""
    _FakeLnurlHandler.body_json = json.dumps(vectors["v1_lnurlp_metadata"])
    host, port = fake_lnurl_server.server_address
    lnurl = url_to_lnurl(f"http://{host}:{port}/.well-known/lnurlp/alice")
    meta = fetch_metadata(lnurl, settings={"proxy-enabled": False})
    assert meta["nostrPubkey"] == vectors["keys"]["lnurlp_nostr_pub"]
    assert meta["minSendable"] == 1000
    assert meta["maxSendable"] == 100000000
    assert meta["commentAllowed"] == 280
    assert meta["domain"] == host


def test_fetch_metadata_end_to_end_gate(vectors, fake_lnurl_server):
    """Gate must reject over the wire too (allowsNostr missing)."""
    body = dict(vectors["v1_lnurlp_metadata"])
    del body["allowsNostr"]
    _FakeLnurlHandler.body_json = json.dumps(body)
    host, port = fake_lnurl_server.server_address
    lnurl = url_to_lnurl(f"http://{host}:{port}/.well-known/lnurlp/alice")
    with pytest.raises(NotZapableError):
        fetch_metadata(lnurl, settings={"proxy-enabled": False})
