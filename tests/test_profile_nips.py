"""Tests for profile NIP support — NIP-05 verification, NIP-39 external
identities, NIP-58 badges, and LUD-16 lightning address resolution.

profile_nips is GTK-free (stdlib only), so these tests import the real module
and verify against the authoritative spec examples (nostr-protocol/nips master).
"""

import json
from unittest.mock import patch

from gnostr import profile_nips

# Real pubkey from the NIP-05 spec example.
BOB_PUBKEY = "b0635d6a9851d3aed0cd6c495b282167acf761729078d975fc341b22650b07b9"


# --- NIP-05 verification ---


def test_verify_nip05_matches_pubkey():
    body = json.dumps({"names": {"bob": BOB_PUBKEY}})
    with patch("urllib.request.urlopen") as m:
        m.return_value.__enter__.return_value.read.return_value = body.encode()
        assert profile_nips.verify_nip05(BOB_PUBKEY, "bob@example.com") is True


def test_verify_nip05_mismatched_pubkey():
    body = json.dumps({"names": {"bob": "deadbeef"}})
    with patch("urllib.request.urlopen") as m:
        m.return_value.__enter__.return_value.read.return_value = body.encode()
        assert profile_nips.verify_nip05(BOB_PUBKEY, "bob@example.com") is False


def test_verify_nip05_missing_name():
    body = json.dumps({"names": {}})
    with patch("urllib.request.urlopen") as m:
        m.return_value.__enter__.return_value.read.return_value = body.encode()
        assert profile_nips.verify_nip05(BOB_PUBKEY, "bob@example.com") is False


def test_verify_nip05_network_error_returns_false():
    with patch("urllib.request.urlopen", side_effect=Exception("boom")):
        assert profile_nips.verify_nip05(BOB_PUBKEY, "bob@example.com") is False


def test_verify_nip05_invalid_identifier():
    # local-part must be a-z0-9-_. per NIP-05
    assert profile_nips.verify_nip05(BOB_PUBKEY, "bob!@example.com") is False
    assert profile_nips.verify_nip05(BOB_PUBKEY, "not-an-email") is False


def test_verify_nip05_uses_well_known_url():
    body = json.dumps({"names": {"bob": BOB_PUBKEY}})
    with patch("urllib.request.urlopen") as m:
        m.return_value.__enter__.return_value.read.return_value = body.encode()
        profile_nips.verify_nip05(BOB_PUBKEY, "bob@example.com")
        url = m.call_args.args[0].full_url
        assert url == "https://example.com/.well-known/nostr.json?name=bob"


# --- NIP-39 external identities ---


def test_parse_external_identities_github():
    event = {
        "kind": 10011,
        "tags": [
            ["i", "github:semisol", "9721ce4ee4fceb91c9711ca2a6c9a5ab"],
        ],
    }
    ids = profile_nips.parse_external_identities(event)
    assert len(ids) == 1
    assert ids[0]["platform"] == "github"
    assert ids[0]["identity"] == "semisol"
    assert ids[0]["proof"] == "9721ce4ee4fceb91c9711ca2a6c9a5ab"
    assert ids[0]["url"] == "https://github.com/semisol"


def test_parse_external_identities_multiple_platforms():
    event = {
        "kind": 10011,
        "tags": [
            ["i", "github:semisol", "gist1"],
            ["i", "twitter:semisol_public", "1619358434134196225"],
            ["i", "mastodon:bitcoinhackers.org/@semisol", "109775066355589974"],
            ["i", "telegram:1087295469", "nostrdirectory/770"],
        ],
    }
    ids = profile_nips.parse_external_identities(event)
    assert len(ids) == 4
    assert ids[1]["url"] == "https://twitter.com/semisol_public"
    assert ids[2]["url"] == "https://bitcoinhackers.org/@semisol/109775066355589974"
    assert ids[3]["url"] == "https://t.me/nostrdirectory/770"


def test_parse_external_identities_ignores_short_tags():
    # i tags MUST have 2 params (platform:identity + proof) per NIP-39
    event = {"kind": 10011, "tags": [["i", "github:semisol"]]}
    assert profile_nips.parse_external_identities(event) == []


def test_parse_external_identities_wrong_kind():
    event = {"kind": 1, "tags": [["i", "github:semisol", "proof"]]}
    assert profile_nips.parse_external_identities(event) == []


# --- NIP-58 profile badges ---


def test_parse_profile_badges_ordered_pairs():
    event = {
        "kind": 10008,
        "tags": [
            ["a", "30009:issuer1:bravery"],
            ["e", "award1"],
            ["a", "30009:issuer2:kindness"],
            ["e", "award2"],
        ],
    }
    badges = profile_nips.parse_profile_badges(event)
    assert len(badges) == 2
    assert badges[0] == ("30009:issuer1:bravery", "award1")
    assert badges[1] == ("30009:issuer2:kindness", "award2")


def test_parse_profile_badges_ignores_unpaired():
    # NIP-58: clients SHOULD ignore a without corresponding e and viceversa
    event = {
        "kind": 10008,
        "tags": [
            ["a", "30009:issuer1:bravery"],
            ["e", "award1"],
            ["a", "30009:issuer2:orphan"],
        ],
    }
    badges = profile_nips.parse_profile_badges(event)
    assert len(badges) == 1
    assert badges[0] == ("30009:issuer1:bravery", "award1")


def test_parse_profile_badges_wrong_kind():
    event = {"kind": 1, "tags": [["a", "30009:x:y"], ["e", "e1"]]}
    assert profile_nips.parse_profile_badges(event) == []


# --- LUD-16 lightning address resolution ---


def test_resolve_lnurl_endpoint():
    assert profile_nips.resolve_lnurl_endpoint("bob@example.com") == (
        "https://example.com/.well-known/lnurlp/bob"
    )


def test_resolve_lnurl_endpoint_invalid():
    assert profile_nips.resolve_lnurl_endpoint("not-an-address") is None
