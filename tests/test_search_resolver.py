"""Search identifier resolver — maps pasted npub/nprofile/nsec/hex/nostr: to a pubkey."""

from gnostr import nostr_utils as nu

PUB = "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899"


def test_npub_to_hex_roundtrip():
    npub = nu.hex_to_npub(PUB)
    assert npub
    assert nu.npub_to_hex(npub) == PUB


def test_npub_to_hex_rejects_non_npub():
    assert nu.npub_to_hex("nsec1qqqq") is None
    assert nu.npub_to_hex("") is None


def test_resolve_npub():
    npub = nu.hex_to_npub(PUB)
    assert nu.resolve_profile_identifier(npub) == PUB


def test_resolve_nprofile():
    # Build a valid NIP-19 nprofile: TLV type 0 (pubkey) = \x00\x20 + 32 bytes
    raw = b"\x00\x20" + bytes.fromhex(PUB)
    five = nu.convertbits(raw, 8, 5, True)
    nprofile = nu.bech32_encode("nprofile", five)
    assert nu.resolve_profile_identifier(nprofile) == PUB


def test_resolve_hex():
    assert nu.resolve_profile_identifier(PUB) == PUB


def test_resolve_nostr_prefix():
    npub = nu.hex_to_npub(PUB)
    assert nu.resolve_profile_identifier(f"nostr:{npub}") == PUB
    assert nu.resolve_profile_identifier(f"nostr:{PUB}") == PUB


def test_resolve_nsec():
    # nsec -> priv hex -> pubkey (own key)
    priv = "11" * 32
    pub = nu.get_public_key(priv)
    nsec = nu.hex_to_nsec(priv)
    assert nsec
    assert nu.resolve_profile_identifier(nsec) == pub


def test_resolve_invalid_returns_none():
    assert nu.resolve_profile_identifier("garbage") is None
    assert nu.resolve_profile_identifier("") is None
    assert nu.resolve_profile_identifier("nostr:") is None
