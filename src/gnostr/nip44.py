"""NIP-44 v2: versioned "encrypted" payload for direct messages.

Pure Python, stdlib + the existing `ecdsa` dependency only (the Flatpak
runtime has neither nacl nor a usable AEAD for this). Byte-identical to the
official NIP-44 v2 implementation, verified against the official test
vectors committed at tests/data/nip44.vectors.json
(sha256 269ed0f69e4c192512cc779e78c555090cebc7c785b609e338a62afc3ce25040).

No GTK imports; unit-testable as plain pytest.

Spec: https://github.com/nostr-protocol/nips/blob/master/44.md
"""

import base64
import hashlib
import hmac
import math
import secrets

import ecdsa

# --- limits (NIP-44 "Implementation considerations") ---
MIN_PLAINTEXT_LEN = 1
MAX_PLAINTEXT_LEN = 65535  # u16 padding limit; official invalid vectors start at 65536
MIN_DECODED_LEN = 1 + 32 + 34 + 32  # ver + nonce + ct(min 34 = 2-len + 32 pad) + mac(32)
MAX_DECODED_LEN = 512 * 1024  # DoS guard before decode

CONV_KEY_SALT = b"nip44-v2"


class NIP44Error(ValueError):
    """Any NIP-44 protocol violation (bad version, MAC, padding, size...)."""


# ---------------------------------------------------------------------------
# HKDF (RFC 5869, SHA-256)
# ---------------------------------------------------------------------------


def hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    okm, prev = b"", b""
    counter = 1
    while len(okm) < length:
        prev = hmac.new(prk, prev + info + bytes([counter]), hashlib.sha256).digest()
        okm += prev
        counter += 1
    return okm[:length]


def hkdf(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    return hkdf_expand(hkdf_extract(salt, ikm), info, length)


# ---------------------------------------------------------------------------
# secp256k1 ECDH — unhashed 32-byte x-coordinate (BIP-340 convention)
# ---------------------------------------------------------------------------


def ecdh_x(priv_hex: bytes, pub_hex: bytes) -> bytes:
    """Shared secret = x-coordinate of priv * pub (unhashed, 32 bytes)."""
    if len(priv_hex) != 32 or len(pub_hex) != 32:
        raise NIP44Error("bad key length")
    curve = ecdsa.curves.SECP256k1
    p = curve.curve.p()
    d = int.from_bytes(priv_hex, "big")
    if not (1 <= d < curve.order):
        raise NIP44Error("private key out of range")
    x = int.from_bytes(pub_hex, "big")
    if x >= p:
        raise NIP44Error("pubkey out of range")
    y_sq = (pow(x, 3, p) + 7) % p  # secp256k1: a=0, b=7
    y = pow(y_sq, (p + 1) // 4, p)
    if (y * y) % p != y_sq:
        raise NIP44Error("invalid pubkey (not on curve)")
    if y & 1:  # BIP-340 x-only: even y
        y = p - y
    point = ecdsa.ellipticcurve.Point(curve.curve, x, y, curve.order)
    if not curve.curve.contains_point(x, y):
        raise NIP44Error("invalid pubkey (not on curve)")
    shared = point * d
    if shared == ecdsa.ellipticcurve.INFINITY:
        raise NIP44Error("ECDH produced point at infinity")
    return shared.x().to_bytes(32, "big")


def get_conversation_key(privkey_hex: bytes, counterparty_pubkey_hex: bytes) -> bytes:
    """NIP-44 conversation key: HKDF-extract(salt='nip44-v2') over ECDH x."""
    shared_x = ecdh_x(privkey_hex, counterparty_pubkey_hex)
    return hkdf_extract(salt=CONV_KEY_SALT, ikm=shared_x)


# ---------------------------------------------------------------------------
# ChaCha20 (RFC 8439: 20 rounds, 32-bit counter, 12-byte nonce)
# ---------------------------------------------------------------------------

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


def _chacha20_block(key: bytes, counter: int, nonce: bytes) -> bytes:
    state = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574]
    state += [int.from_bytes(key[i : i + 4], "little") for i in range(0, 32, 4)]
    state.append(counter & _MASK32)
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


def chacha20_xor(key: bytes, counter: int, nonce: bytes, data: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(data), 64):
        block = _chacha20_block(key, counter + i // 64, nonce)
        out += bytes(a ^ b for a, b in zip(data[i : i + 64], block))
    return bytes(out)


# ---------------------------------------------------------------------------
# Poly1305 (RFC 2020 / 8439)
# ---------------------------------------------------------------------------

_POLY1305_P = (1 << 255) - 19


def _poly1305_mac(key: bytes, msg: bytes) -> bytes:
    """Poly1305 MAC. key = 32 bytes (r||s); returns 16-byte tag."""
    r = int.from_bytes(key[:16], "little")
    r &= 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFC  # clamp
    s = int.from_bytes(key[16:32], "little")
    acc = 0
    for i in range(0, len(msg), 16):
        block = msg[i : i + 16]
        n = int.from_bytes(block + b"\x01", "little")  # pad bit after final byte
        acc = ((acc + n) * r) % _POLY1305_P
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def _pad16(data: bytes) -> bytes:
    rem = len(data) % 16
    return b"" if rem == 0 else b"\x00" * (16 - rem)


def _mac_data(aad: bytes, ct: bytes) -> bytes:
    return (
        aad
        + _pad16(aad)
        + ct
        + _pad16(ct)
        + len(aad).to_bytes(8, "little")
        + len(ct).to_bytes(8, "little")
    )


def chacha20poly1305_seal(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    """AEAD seal (key = 64-byte sec_key okey||rkey); returns ct || 16-byte tag."""
    okey = key[:32]
    mask = _chacha20_block(okey, 0, nonce)
    ct = chacha20_xor(okey, 1, nonce, plaintext)
    tag = _poly1305_mac(mask[:16], _mac_data(aad, ct))
    return ct + tag


def chacha20poly1305_open(key: bytes, nonce: bytes, ct_and_tag: bytes, aad: bytes = b"") -> bytes:
    """AEAD open; raises NIP44Error on auth failure (constant-time compare)."""
    if len(ct_and_tag) < 16:
        raise NIP44Error("ciphertext too short")
    ct, tag = ct_and_tag[:-16], ct_and_tag[-16:]
    okey = key[:32]
    mask = _chacha20_block(okey, 0, nonce)
    expected = _poly1305_mac(mask[:16], _mac_data(aad, ct))
    if not hmac.compare_digest(tag, expected):
        raise NIP44Error("AEAD tag mismatch")
    return chacha20_xor(okey, 1, nonce, ct)


# ---------------------------------------------------------------------------
# Padding (NIP-44 log2-interpolated buckets, min 32)
# ---------------------------------------------------------------------------


def calc_padded_len(unpadded_len: int) -> int:
    """Official paulmillr/nip44 formula: power-of-two chunk buckets, min 32."""
    if unpadded_len < 1:
        raise NIP44Error("plaintext length must be >= 1")
    if unpadded_len <= 32:
        return 32
    next_power = 2 ** math.ceil(math.log2(unpadded_len))
    chunk = max(next_power // 8, 32)
    return chunk * math.ceil(unpadded_len / chunk)


def _pad_bytes(plaintext: bytes) -> bytes:
    """[u16 len][plaintext][zeroes] (or [0x00,0x00][u32 len]... ) padded body."""
    unpad_len = len(plaintext)
    if unpad_len < MIN_PLAINTEXT_LEN or unpad_len > MAX_PLAINTEXT_LEN:
        raise NIP44Error("plaintext length out of range")
    padded_len = calc_padded_len(unpad_len)
    if unpad_len < 65536:
        prefix = unpad_len.to_bytes(2, "big")
    else:
        prefix = b"\x00\x00" + unpad_len.to_bytes(4, "big")
    return prefix + plaintext + b"\x00" * (padded_len - unpad_len)


def _unpad_bytes(padded_with_prefix: bytes) -> bytes:
    if len(padded_with_prefix) < 34:
        raise NIP44Error("padding: decoded too short")
    unpadded_sz = int.from_bytes(padded_with_prefix[:2], "big")
    prefix_len = 2
    if unpadded_sz == 0:
        unpadded_sz = int.from_bytes(padded_with_prefix[2:6], "big")
        prefix_len = 6
    if unpadded_sz < MIN_PLAINTEXT_LEN or unpadded_sz > MAX_PLAINTEXT_LEN:
        raise NIP44Error("padding: invalid size")
    padded_body = padded_with_prefix[prefix_len:]
    if len(padded_body) != calc_padded_len(unpadded_sz):
        raise NIP44Error("padding: invalid padded length")
    if any(b != 0 for b in padded_body[unpadded_sz:]):
        raise NIP44Error("padding: trailing bytes must be zero")
    return padded_body[:unpadded_sz]


# ---------------------------------------------------------------------------
# NIP-44 v2 conversation encrypt / decrypt
# ---------------------------------------------------------------------------


def encrypt(plaintext: str, conversation_key: bytes, nonce: bytes = None) -> str:
    if nonce is None:
        nonce = secrets.token_bytes(32)
    if len(conversation_key) != 32 or len(nonce) != 32:
        raise NIP44Error("conversation_key and nonce must be 32 bytes")
    keys = hkdf_expand(conversation_key, nonce, 76)
    chacha_key, chacha_nonce, hmac_key = keys[:32], keys[32:44], keys[44:76]
    padded = _pad_bytes(plaintext.encode("utf-8"))
    ct = chacha20_xor(chacha_key, 0, chacha_nonce, padded)
    mac = hmac.new(hmac_key, nonce + ct, hashlib.sha256).digest()
    return base64.b64encode(b"\x02" + nonce + ct + mac).decode("ascii")


def decrypt(payload_b64: str, conversation_key: bytes) -> str:
    if payload_b64.startswith("#"):
        raise NIP44Error("unsupported version: per-message-key fallback removed")
    if len(payload_b64) > 4 * (MAX_DECODED_LEN // 3):
        raise NIP44Error("payload too large")
    try:
        payload = base64.b64decode(payload_b64, validate=True)
    except Exception:
        raise NIP44Error("invalid base64")
    if len(payload) > MAX_DECODED_LEN:
        raise NIP44Error("payload too large")
    if len(payload) < MIN_DECODED_LEN:
        raise NIP44Error("payload too short")
    if payload[0] != 2:
        raise NIP44Error("unknown version byte")
    nonce = payload[1:33]
    ct_mac = payload[33:]
    ct, mac = ct_mac[:-32], ct_mac[-32:]
    keys = hkdf_expand(conversation_key, nonce, 76)
    chacha_key, chacha_nonce, hmac_key = keys[:32], keys[32:44], keys[44:76]
    expected = hmac.new(hmac_key, nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise NIP44Error("MAC mismatch")
    padded = chacha20_xor(chacha_key, 0, chacha_nonce, ct)
    return _unpad_bytes(padded).decode("utf-8")


# ---------------------------------------------------------------------------
# NIP-44 seal / unseal (per-message-key AEAD, e.g. for NIP-59-style seals)
# ---------------------------------------------------------------------------


def get_message_keys(conversation_key: bytes, nonce: bytes) -> tuple:
    """Per-message AEAD keys: hkdf_expand(conv_key, info=nonce, L=76).

    Returns (chacha_key[32], chacha_nonce[12], hmac_key[32]) exactly as the
    official get_message_keys vectors specify.
    """
    if len(conversation_key) != 32 or len(nonce) != 32:
        raise NIP44Error("conversation_key and nonce must be 32 bytes")
    keys = hkdf_expand(conversation_key, nonce, 76)
    return keys[:32], keys[32:44], keys[44:76]


def seal(plaintext: str, recipient_pubkey: bytes) -> str:
    """Seal to a pubkey with an ephemeral keypair: NIP-44 v2 payload whose
    conversation key is ECDH(eph_priv, recipient_pub). Recipient unseals with
    their own private key; sender stays anonymous to onlookers."""
    sk = ecdsa.SigningKey.generate(curve=ecdsa.SECP256k1)
    eph_priv = sk.privkey.secret_multiplier.to_bytes(32, "big")
    eph_pub = sk.get_verifying_key().pubkey.point.x().to_bytes(32, "big")
    conv = get_conversation_key(eph_priv, recipient_pubkey)
    inner = encrypt(plaintext, conv)  # 0x02 || nonce32 || ct || mac64
    return base64.b64encode(b"\x02" + eph_pub + base64.b64decode(inner)).decode("ascii")


def unseal(payload_b64: str, recipient_privkey: bytes) -> str:
    if payload_b64.startswith("#"):
        raise NIP44Error("unsupported seal version")
    payload = base64.b64decode(payload_b64, validate=True)
    if len(payload) < 1 + 32 + MIN_DECODED_LEN:
        raise NIP44Error("seal payload too short")
    if payload[0] != 2:
        raise NIP44Error("unknown version byte")
    eph_pub = payload[1:33]
    inner = base64.b64encode(payload[33:]).decode("ascii")
    conv = get_conversation_key(recipient_privkey, eph_pub)
    return decrypt(inner, conv)
