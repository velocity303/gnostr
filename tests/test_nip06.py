"""NIP-06 derivation vectors (issue #16): BIP-39 official vectors + NIP-06 keypairs."""

import hashlib
import hmac

import pytest

from gnostr import nip06

# Official BIP-39 test vectors (trezor/python-mnemonic vectors.json, english,
# passphrase "TREZOR"): [entropy, mnemonic, seed].
BIP39_VECTORS = [
    (
        "00000000000000000000000000000000",
        "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
        "c55257c360c07c72029aebc1b53c05ed0362ada38ead3e3e9efa3708e53495531f09a6987599d18264c1e1c92f2cf141630c7a3c4ab7c81b2f001698e7463b04",
    ),
    (
        "7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f",
        "legal winner thank year wave sausage worth useful legal winner thank yellow",
        "2e8905819b8723fe2c1d161860e5ee1830318dbf49a83bd451cfb8440c28bd6fa457fe1296106559a3c80937a1c1069be3a3a5bd381ee6260e8d9739fce1f607",
    ),
    (
        "ffffffffffffffffffffffffffffffff",
        "zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo wrong",
        "ac27495480225222079d7be181583751e86f571027b0497b5b5d11218e0a8a13332572917f0f8e5a589620c6f15b11c61dee327651a14c34e18231052e48c069",
    ),
    (
        "808080808080808080808080808080808080808080808080",
        "letter advice cage absurd amount doctor acoustic avoid letter advice cage absurd amount doctor acoustic avoid letter always",
        "107d7c02a5aa6f38c58083ff74f04c607c2d2c0ecc55501dadd72d025b751bc27fe913ffb796f841c49b1d33b610cf0e91d3aa239027f5e99fe4ce9e5088cd65",
    ),
    (
        "8080808080808080808080808080808080808080808080808080808080808080",
        "letter advice cage absurd amount doctor acoustic avoid letter advice cage absurd amount doctor acoustic avoid letter advice cage absurd amount doctor acoustic bless",
        "c0c519bd0e91a2ed54357d9d1ebef6f5af218a153624cf4f2da911a0ed8f7a09e2ef61af0aca007096df430022f7a2b6fb91661a9589097069720d015e4e982f",
    ),
    (
        "c0ba5a8e914111210f2bd131f3d5e08d",
        "scheme spot photo card baby mountain device kick cradle pact join borrow",
        "ea725895aaae8d4c1cf682c1bfd2d358d52ed9f0f0591131b559e2724bb234fca05aa9c02c57407e04ee9dc3b454aa63fbff483a8b11de949624b9f1831a9612",
    ),
]

# Official NIP-06 test vectors (current NIP text, path m/44'/1237'/0'/0/0).
NIP06_VECTORS = [
    (
        "leader monkey parrot ring guide accident before fence cannon height naive bean",
        "7f7ff03d123792d6ac594bfa67bf6d0c0ab55b6b1fdb6249303fe861f1ccba9a",
        "17162c921dc4d2518f9a101db33695df1afb56ab82f5ff3e5da6eec3ca5cd917",
    ),
    (
        "what bleak badge arrange retreat wolf trade produce cricket blur garlic valid proud rude strong choose busy staff weather area salt hollow arm fade",
        "c15d739894c81a2fcfd3a2df85a0d2c0dbc47a280d092799f144d73d7ae78add",
        "d41b22899549e1f3d335a31002cfd382174006e166d3e658e3a5eecdb6463573",
    ),
]


@pytest.mark.parametrize("entropy,mnemonic,seed", BIP39_VECTORS)
def test_bip39_seed_vectors(entropy, mnemonic, seed):
    assert nip06.bip39_seed(mnemonic, "TREZOR").hex() == seed


@pytest.mark.parametrize("entropy,mnemonic,_seed", BIP39_VECTORS)
def test_bip39_entropy_roundtrip(entropy, mnemonic, _seed):
    """Mnemonic words decode back to the entropy they were generated from."""
    assert nip06.validate_mnemonic(mnemonic) == mnemonic
    words = mnemonic.split()
    wordlist = nip06._load_wordlist()
    idx = [wordlist.index(w) for w in words]
    bits = "".join(format(i, "011b") for i in idx)
    ent = len(words) * 11 * 32 // 33
    assert int(bits[:ent], 2).to_bytes(ent // 8, "big").hex() == entropy


@pytest.mark.parametrize("mnemonic,priv,pub", NIP06_VECTORS)
def test_nip06_keypair_vectors(mnemonic, priv, pub):
    got_priv, got_pub = nip06.derive_keypair(mnemonic)
    assert got_priv == priv
    assert got_pub == pub


def test_nip06_pubkey_matches_nostr_utils():
    """The derived x-only pubkey agrees with nostr_utils.get_public_key."""
    from gnostr import nostr_utils

    priv, pub = nip06.derive_keypair(NIP06_VECTORS[0][0])
    assert nostr_utils.get_public_key(priv) == pub


def test_nip06_nsec_roundtrip():
    from gnostr import nostr_utils

    priv, _ = nip06.derive_keypair(NIP06_VECTORS[0][0])
    nsec = nostr_utils.hex_to_nsec(priv)
    assert nostr_utils.nsec_to_hex(nsec) == priv


def test_bad_checksum_rejected():
    # last word of the abandon vector swapped: checksum no longer valid
    with pytest.raises(ValueError):
        nip06.validate_mnemonic(
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon"
        )


def test_unknown_word_rejected():
    with pytest.raises(ValueError):
        nip06.validate_mnemonic("notaword " * 11 + "about")


def test_wrong_word_count_rejected():
    with pytest.raises(ValueError):
        nip06.validate_mnemonic("abandon abandon abandon")


def test_bip32_master_vector():
    """BIP-32 test vector 1: master key from seed 000102...0f."""
    seed = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    assert (
        "%064x" % nip06.bip32_derive(seed, "m")
        == "e8f32e723decf4051aefac8e2c93c9c5b214313817cdb01a1494b917c8436b35"
    )
    assert (
        "%064x" % nip06.bip32_derive(seed, "m/0h/1/2h/2")
        == "0f479245fb19a38a1954c5c7c0ebab2f9bdfd96a17563ef28a6a4b1a2a764ef4"
    )
