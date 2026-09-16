"""NIP-49 ncryptsec — pure pytest, no GTK. Vectors from the NIP itself."""

import pytest

from gnostr.nip49 import nip49_decrypt, nip49_encrypt

# Official NIP-49 decryption vector (log_n=16 embedded in blob).
VECTOR = (
    "ncryptsec1qgg9947rlpvqu76pj5ecreduf9jxhselq2nae2kghhvd5g7dgjtcxfqt"
    "d67p9m0w57lspw8gsq6yphnm8623nsl8xn9j4jdzz84zm3frztj3z7s35vpzmqf6ks"
    "u8r89qk5z2zxfmu5gv8th8wclt0h4p"
)
VECTOR_KEY = "3501454135014541350145413501453fefb02227e449e57cf4d3a3ce05378683"


def test_official_vector_decrypts():
    assert nip49_decrypt(VECTOR, "nostr") == VECTOR_KEY


def test_wrong_password_raises():
    with pytest.raises(ValueError):
        nip49_decrypt(VECTOR, "wrong")


def test_roundtrip():
    sk = "ab" * 32
    blob = nip49_encrypt(sk, "pässwörd-🔐")
    assert blob.startswith("ncryptsec1")
    assert nip49_decrypt(blob, "pässwörd-🔐") == sk


def test_tampered_blob_raises():
    blob = nip49_encrypt("cd" * 32, "pw")
    flipped = blob[:-4] + ("q" if blob[-4] != "q" else "p") + blob[-3:]
    with pytest.raises(ValueError):
        nip49_decrypt(flipped, "pw")


def test_not_ncryptsec_raises():
    with pytest.raises(ValueError):
        nip49_decrypt("nsec1whatever", "pw")


def test_log_n_override_roundtrips():
    blob = nip49_encrypt("ef" * 32, "pw", log_n=16)
    assert nip49_decrypt(blob, "pw") == "ef" * 32
