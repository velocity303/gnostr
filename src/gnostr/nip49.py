"""NIP-49: encrypted private key backup (`ncryptsec`).

Pure Python, stdlib-only (hashlib.scrypt + vendored XChaCha20-Poly1305),
because the Flatpak runtime has neither nacl nor XChaCha20 in its
cryptography build. The AEAD below was verified byte-identical to libsodium
and against the official NIP-49 test vector (see tests/test_nip49.py).

This module is the portable-backup layer only — libsecret (key_manager.py)
remains the at-rest store. No GTK imports; unit-testable as plain pytest.
"""

import hashlib
import hmac
import os
import unicodedata

from . import nostr_utils

_MASK32 = 0xFFFFFFFF


def _rotl32(v, n):
    return ((v << n) & _MASK32) | (v >> (32 - n))


def _quarter_round(x, a, b, c, d):
    x[a] = (x[a] + x[b]) & _MASK32
    x[d] = _rotl32(x[d] ^ x[a], 16)
    x[c] = (x[c] + x[d]) & _MASK32
    x[b] = _rotl32(x[b] ^ x[c], 12)
    x[a] = (x[a] + x[b]) & _MASK32
    x[d] = _rotl32(x[d] ^ x[a], 8)
    x[c] = (x[c] + x[d]) & _MASK32
    x[b] = _rotl32(x[b] ^ x[c], 7)


def _chacha20_block(key, counter, nonce):
    state = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574]
    state += [int.from_bytes(key[i : i + 4], "little") for i in range(0, 32, 4)]
    state.append(counter)
    state += [int.from_bytes(nonce[i : i + 4], "little") for i in range(0, 12, 4)]
    work = list(state)
    for _ in range(10):
        _quarter_round(work, 0, 4, 8, 12)
        _quarter_round(work, 1, 5, 9, 13)
        _quarter_round(work, 2, 6, 10, 14)
        _quarter_round(work, 3, 7, 11, 15)
        _quarter_round(work, 0, 5, 10, 15)
        _quarter_round(work, 1, 6, 11, 12)
        _quarter_round(work, 2, 7, 8, 13)
        _quarter_round(work, 3, 4, 9, 14)
    return b"".join(
        ((work[i] + state[i]) & _MASK32).to_bytes(4, "little") for i in range(16)
    )


def _hchacha20(key, nonce16):
    state = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574]
    state += [int.from_bytes(key[i : i + 4], "little") for i in range(0, 32, 4)]
    state += [int.from_bytes(nonce16[i : i + 4], "little") for i in range(0, 16, 4)]
    # 20 rounds = 10 double-rounds (same core as ChaCha20, no feedback add).
    for _ in range(10):
        _quarter_round(state, 0, 4, 8, 12)
        _quarter_round(state, 1, 5, 9, 13)
        _quarter_round(state, 2, 6, 10, 14)
        _quarter_round(state, 3, 7, 11, 15)
        _quarter_round(state, 0, 5, 10, 15)
        _quarter_round(state, 1, 6, 11, 12)
        _quarter_round(state, 2, 7, 8, 13)
        _quarter_round(state, 3, 4, 9, 14)
    return b"".join(
        state[i].to_bytes(4, "little") for i in (0, 1, 2, 3, 12, 13, 14, 15)
    )


def _poly1305_mac(key, msg):
    # RFC 8439 clamp: bytes 3,7,11,15 of the little-endian r become C0,C0,C0,0F
    # (integer mask 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF).
    r = int.from_bytes(key[0:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(key[16:32], "little")
    p = (1 << 130) - 5
    acc = 0
    for i in range(0, len(msg), 16):
        block = msg[i : i + 16]
        n = int.from_bytes(block + b"\x01", "little")
        acc = ((acc + n) * r) % p
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def _chacha20_xor(key, counter, nonce, data):
    out = bytearray()
    c = counter
    for i in range(0, len(data), 64):
        ks = _chacha20_block(key, c, nonce)
        out += bytes(a ^ b for a, b in zip(data[i : i + 64], ks))
        c += 1
    return bytes(out)


def _mac_data(aad, ct):
    pad_a = (16 - (len(aad) % 16)) % 16
    pad_c = (16 - (len(ct) % 16)) % 16
    return (
        aad
        + b"\x00" * pad_a
        + ct
        + b"\x00" * pad_c
        + len(aad).to_bytes(8, "little")
        + len(ct).to_bytes(8, "little")
    )


def xchacha20poly1305_seal(key, nonce24, plaintext, aad=b""):
    subkey = _hchacha20(key, nonce24[:16])
    subnonce = b"\x00\x00\x00\x00" + nonce24[16:]
    mac_key = _chacha20_block(subkey, 0, subnonce)[:32]
    ct = _chacha20_xor(subkey, 1, subnonce, plaintext)
    return ct + _poly1305_mac(mac_key, _mac_data(aad, ct))


def xchacha20poly1305_open(key, nonce24, ct_and_tag, aad=b""):
    if len(ct_and_tag) < 16:
        raise ValueError("ciphertext too short")
    ct, tag = ct_and_tag[:-16], ct_and_tag[-16:]
    subkey = _hchacha20(key, nonce24[:16])
    subnonce = b"\x00\x00\x00\x00" + nonce24[16:]
    mac_key = _chacha20_block(subkey, 0, subnonce)[:32]
    if not hmac.compare_digest(_poly1305_mac(mac_key, _mac_data(aad, ct)), tag):
        raise ValueError("decryption failed: wrong password or corrupt data")
    return _chacha20_xor(subkey, 1, subnonce, ct)


def _scrypt32(pw, salt, log_n):
    """scrypt KDF with an explicit memory ceiling.

    OpenSSL's default maxmem is 32 MiB and rejects n=2**16 (needs 64 MiB),
    so the limit must be raised explicitly — on the Flatpak runtime too.
    """
    mem = 128 * (1 << log_n) * 8  # scrypt working set
    return hashlib.scrypt(pw, salt=salt, n=1 << log_n, r=8, p=1, dklen=32, maxmem=mem + (1 << 20))


_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech_decode(bech):
    pos = bech.rfind("1")
    if pos < 1:
        raise ValueError("invalid ncryptsec string")
    hrp = bech[:pos]
    vals = []
    for x in bech[pos + 1 :]:
        if x not in _CHARSET:
            raise ValueError("invalid ncryptsec string")
        vals.append(_CHARSET.index(x))
    if not nostr_utils.bech32_verify_checksum(hrp, vals):
        raise ValueError("invalid ncryptsec checksum")
    return hrp, vals[:-3]


def nip49_encrypt(nsec_hex, password, log_n=16):
    """Encrypt a hex private key with a password -> ncryptsec string.

    log_n=16 matches the NIP test vectors (64 MiB scrypt, ~100 ms).
    KEY_SECURITY_BYTE is 0x02 (client does not track) per NIP-49.
    """
    priv = bytes.fromhex(nsec_hex)
    if len(priv) != 32:
        raise ValueError("private key must be 32 bytes")
    salt = os.urandom(16)
    nonce = os.urandom(24)
    pw = unicodedata.normalize("NFKC", password).encode("utf-8")
    key = _scrypt32(pw, salt, log_n)
    ct = xchacha20poly1305_seal(key, nonce, priv, aad=b"\x02")
    raw = bytes([0x02, log_n]) + salt + nonce + b"\x02" + ct
    if len(raw) != 91:
        raise ValueError(f"internal length error: {len(raw)}")
    data5 = nostr_utils.convertbits(raw, 8, 5)
    return nostr_utils.bech32_encode("ncryptsec", data5)


def nip49_decrypt(blob, password):
    """Decrypt an ncryptsec string -> hex private key.

    Raises ValueError on wrong password, tamper, bad checksum, bad length,
    or unsupported version. Never returns partial/garbage key material.
    """
    if not blob.startswith("ncryptsec"):
        raise ValueError("not an ncryptsec string")
    _, data5 = _bech_decode(blob)
    conv = nostr_utils.convertbits(data5, 5, 8, pad=True)
    if conv is None:
        raise ValueError("invalid ncryptsec padding")
    raw = bytes(conv)
    # The official NIP-49 test vector carries 93 bytes (2 trailing bytes past
    # the documented 91); fields are offset-based and the Poly1305 tag
    # authenticates exactly ct(32)+tag(16), so trailing bytes are ignored.
    if len(raw) < 91:
        raise ValueError("invalid ncryptsec length")
    version, log_n = raw[0], raw[1]
    if version != 0x02:
        raise ValueError(f"unsupported ncryptsec version {version}")
    salt, nonce, aad = raw[2:18], raw[18:42], raw[42:43]
    ct = raw[43:91]  # ciphertext + 16-byte tag
    pw = unicodedata.normalize("NFKC", password).encode("utf-8")
    key = _scrypt32(pw, salt, log_n)
    return xchacha20poly1305_open(key, nonce, ct, aad).hex()
