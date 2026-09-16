"""Onboarding key primitives — pure pytest, no GTK (conftest mocks gi anyway)."""
import re

import pytest

from gnostr.nostr_utils import generate_keypair, nsec_to_hex, hex_to_nsec

ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def test_generate_keypair_returns_valid_hex_pair():
    priv_hex, pub_hex = generate_keypair()
    assert re.fullmatch(r"[0-9a-f]{64}", priv_hex)
    assert re.fullmatch(r"[0-9a-f]{64}", pub_hex)
    assert 0 < int(priv_hex, 16) < ORDER


def test_generate_keypair_roundtrips_through_nsec():
    priv_hex, _ = generate_keypair()
    assert nsec_to_hex(hex_to_nsec(priv_hex)) == priv_hex


def test_generate_keypair_is_random():
    assert generate_keypair()[0] != generate_keypair()[0]
