"""NIP-44 v2 gate: replays the official committed vectors in
tests/data/nip44.vectors.json against gnostr.nip44 (pure Python, no nacl)."""

import base64
import hashlib
import json
import pathlib

import ecdsa
import pytest

from gnostr import nip44

VECTORS = json.loads(
    (pathlib.Path(__file__).parent / "data" / "nip44.vectors.json").read_text()
)["v2"]


# --- padding -------------------------------------------------------------

@pytest.mark.parametrize("case", VECTORS["valid"]["calc_padded_len"])
def test_calc_padded_len_valid(case):
    unpadded, padded = case
    assert nip44.calc_padded_len(unpadded) == padded


# --- conversation key (ECDH x-only, secp256k1) ----------------------------

@pytest.mark.parametrize("case", VECTORS["valid"]["get_conversation_key"])
def test_get_conversation_key_valid(case):
    got = nip44.get_conversation_key(
        bytes.fromhex(case["sec1"]), bytes.fromhex(case["pub2"])
    )
    assert got.hex() == case["conversation_key"]


@pytest.mark.parametrize("case", VECTORS["invalid"]["get_conversation_key"])
def test_get_conversation_key_invalid(case):
    with pytest.raises(nip44.NIP44Error):
        nip44.get_conversation_key(
            bytes.fromhex(case["sec1"]), bytes.fromhex(case["pub2"])
        )


# --- per-message keys ------------------------------------------------------


def test_get_message_keys():
    gmk = VECTORS["valid"]["get_message_keys"]
    ck = bytes.fromhex(gmk["conversation_key"])
    for kc in gmk["keys"]:
        chacha_key, chacha_nonce, hmac_key = nip44.get_message_keys(
            ck, bytes.fromhex(kc["nonce"])
        )
        assert chacha_key.hex() == kc["chacha_key"]
        assert chacha_nonce.hex() == kc["chacha_nonce"]
        assert hmac_key.hex() == kc["hmac_key"]


# --- AEAD core --------------------------------------------------------------

@pytest.mark.parametrize("case", VECTORS["valid"]["encrypt_decrypt"])
def test_encrypt_decrypt(case):
    ck = bytes.fromhex(case["conversation_key"])
    nonce = bytes.fromhex(case["nonce"])
    assert nip44.encrypt(case["plaintext"], ck, nonce) == case["payload"]
    assert nip44.decrypt(case["payload"], ck) == case["plaintext"]


@pytest.mark.parametrize("case", VECTORS["valid"].get("encrypt_decrypt_long_msg", []))
def test_encrypt_decrypt_long(case):
    ck = bytes.fromhex(case["conversation_key"])
    nonce = bytes.fromhex(case["nonce"])
    plaintext = case["pattern"] * int(case["repeat"])
    assert hashlib.sha256(plaintext.encode()).hexdigest() == case["plaintext_sha256"]
    payload = nip44.encrypt(plaintext, ck, nonce)
    # official long-msg vectors publish payload_sha256 over the base64 STRING
    assert hashlib.sha256(payload.encode()).hexdigest() == case["payload_sha256"]
    assert nip44.decrypt(payload, ck) == plaintext


@pytest.mark.parametrize("case", VECTORS["invalid"]["decrypt"])
def test_decrypt_invalid(case):
    with pytest.raises(nip44.NIP44Error):
        nip44.decrypt(case["payload"], bytes.fromhex(case["conversation_key"]))


@pytest.mark.parametrize("length", VECTORS["invalid"]["encrypt_msg_lengths"])
def test_encrypt_msg_length_invalid(length):
    with pytest.raises(nip44.NIP44Error):
        nip44.encrypt("a" * length, bytes(32))


# --- seal / unseal (anonymous per-message-key AEAD; no official vectors) ----


def _keypair():
    sk = ecdsa.SigningKey.generate(curve=ecdsa.SECP256k1)
    priv = sk.privkey.secret_multiplier.to_bytes(32, "big")
    pub = sk.get_verifying_key().pubkey.point.x().to_bytes(32, "big")
    return priv, pub


def test_seal_unseal_roundtrip():
    priv, pub = _keypair()
    payload = nip44.seal("hello seal", pub)
    assert nip44.unseal(payload, priv) == "hello seal"


def test_seal_unseal_wrong_recipient_rejects():
    priv, pub = _keypair()
    other_priv, _ = _keypair()
    payload = nip44.seal("secret", pub)
    with pytest.raises(nip44.NIP44Error):
        nip44.unseal(payload, other_priv)
