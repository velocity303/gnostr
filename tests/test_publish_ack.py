"""Tests for NIP-01 publish OK acknowledgment parsing.

nostr_utils is GTK-free, so this imports the real module. The client's
publish-result flow (pending dict, OK resolution, timeout sweep) is covered
here at the parser level; the signal emission path is exercised on-device.
"""

from gnostr import nostr_utils


def test_parse_ok_accepted():
    assert nostr_utils.parse_ok_message(["OK", "abc123", True, ""]) == (
        "abc123",
        True,
        "",
    )


def test_parse_ok_rejected():
    assert nostr_utils.parse_ok_message(
        ["OK", "abc123", False, "spam: rate limited"]
    ) == ("abc123", False, "spam: rate limited")


def test_parse_ok_string_bool():
    # Some relays serialize the bool as a JSON string.
    assert nostr_utils.parse_ok_message(["OK", "e1", "true", "ok"]) == (
        "e1",
        True,
        "ok",
    )


def test_parse_ok_not_ok():
    assert nostr_utils.parse_ok_message(["EVENT", "e1"]) is None
    assert nostr_utils.parse_ok_message(["NOTICE", "x"]) is None
    assert nostr_utils.parse_ok_message("garbage") is None


def test_parse_ok_short():
    # Missing message field is tolerated (defaults to empty).
    assert nostr_utils.parse_ok_message(["OK", "e1", True]) == ("e1", True, "")
