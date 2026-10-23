"""NIP-04 legacy encrypted DMs (AES-256-CBC) — pure stdlib.

Used only as the NWC legacy fallback: when the wallet's kind-13194 info
event lacks nip44_v2 (spec zaps-payments-v1 §2.2). Zero new pip deps
(btc-backend-v1 §2): `cryptography` is not a project dependency, so AES is
implemented here from FIPS-197. NIP-44 (nip44.py) remains the preferred
encryption everywhere else.

Key derivation follows NIP-04: AES key = sha256(ECDH x-coordinate). decrypt
additionally accepts the raw-x-key variant emitted by some older generators
(our zap test vectors use it) — tried only after the standard key fails, so
interop with spec-conformant wallets is unchanged.

Payload format: "<base64(ct)>?iv=<base64(iv)>". decrypt also tolerates the
all-hex variant ("<hex(ct)>?iv=<hex(iv)>") seen in legacy fixtures.
"""

import base64
import hashlib

import ecdsa

from gnostr import nip44


# --- AES-256 core (FIPS-197) --------------------------------------------------
# Tables generated from the GF(2^8) inverse + affine transform definition and
# verified against FIPS-197 appendix values (test_nip04.py).

SBOX = [
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
]

INV_SBOX = [
    0x52, 0x09, 0x6A, 0xD5, 0x30, 0x36, 0xA5, 0x38, 0xBF, 0x40, 0xA3, 0x9E, 0x81, 0xF3, 0xD7, 0xFB,
    0x7C, 0xE3, 0x39, 0x82, 0x9B, 0x2F, 0xFF, 0x87, 0x34, 0x8E, 0x43, 0x44, 0xC4, 0xDE, 0xE9, 0xCB,
    0x54, 0x7B, 0x94, 0x32, 0xA6, 0xC2, 0x23, 0x3D, 0xEE, 0x4C, 0x95, 0x0B, 0x42, 0xFA, 0xC3, 0x4E,
    0x08, 0x2E, 0xA1, 0x66, 0x28, 0xD9, 0x24, 0xB2, 0x76, 0x5B, 0xA2, 0x49, 0x6D, 0x8B, 0xD1, 0x25,
    0x72, 0xF8, 0xF6, 0x64, 0x86, 0x68, 0x98, 0x16, 0xD4, 0xA4, 0x5C, 0xCC, 0x5D, 0x65, 0xB6, 0x92,
    0x6C, 0x70, 0x48, 0x50, 0xFD, 0xED, 0xB9, 0xDA, 0x5E, 0x15, 0x46, 0x57, 0xA7, 0x8D, 0x9D, 0x84,
    0x90, 0xD8, 0xAB, 0x00, 0x8C, 0xBC, 0xD3, 0x0A, 0xF7, 0xE4, 0x58, 0x05, 0xB8, 0xB3, 0x45, 0x06,
    0xD0, 0x2C, 0x1E, 0x8F, 0xCA, 0x3F, 0x0F, 0x02, 0xC1, 0xAF, 0xBD, 0x03, 0x01, 0x13, 0x8A, 0x6B,
    0x3A, 0x91, 0x11, 0x41, 0x4F, 0x67, 0xDC, 0xEA, 0x97, 0xF2, 0xCF, 0xCE, 0xF0, 0xB4, 0xE6, 0x73,
    0x96, 0xAC, 0x74, 0x22, 0xE7, 0xAD, 0x35, 0x85, 0xE2, 0xF9, 0x37, 0xE8, 0x1C, 0x75, 0xDF, 0x6E,
    0x47, 0xF1, 0x1A, 0x71, 0x1D, 0x29, 0xC5, 0x89, 0x6F, 0xB7, 0x62, 0x0E, 0xAA, 0x18, 0xBE, 0x1B,
    0xFC, 0x56, 0x3E, 0x4B, 0xC6, 0xD2, 0x79, 0x20, 0x9A, 0xDB, 0xC0, 0xFE, 0x78, 0xCD, 0x5A, 0xF4,
    0x1F, 0xDD, 0xA8, 0x33, 0x88, 0x07, 0xC7, 0x31, 0xB1, 0x12, 0x10, 0x59, 0x27, 0x80, 0xEC, 0x5F,
    0x60, 0x51, 0x7F, 0xA9, 0x19, 0xB5, 0x4A, 0x0D, 0x2D, 0xE5, 0x7A, 0x9F, 0x93, 0xC9, 0x9C, 0xEF,
    0xA0, 0xE0, 0x3B, 0x4D, 0xAE, 0x2A, 0xF5, 0xB0, 0xC8, 0xEB, 0xBB, 0x3C, 0x83, 0x53, 0x99, 0x61,
    0x17, 0x2B, 0x04, 0x7E, 0xBA, 0x77, 0xD6, 0x26, 0xE1, 0x69, 0x14, 0x63, 0x55, 0x21, 0x0C, 0x7D,
]

_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _xtime(a):
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF


def _gmul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        a = _xtime(a)
        b >>= 1
    return p


def _expand_key(key):
    """Key schedule -> list of (Nr+1) round-key 16-byte blocks."""
    nk = len(key) // 4
    nr = nk + 6
    words = [list(key[4 * i : 4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        t = list(words[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [SBOX[b] for b in t]
            t[0] ^= _RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            t = [SBOX[b] for b in t]
        words.append([words[i - nk][j] ^ t[j] for j in range(4)])
    blocks = []
    for r in range(nr + 1):
        blk = []
        for c in range(4):
            blk += words[4 * r + c]
        blocks.append(blk)
    return blocks, nr


def _add_round_key(state, rk):
    for i in range(16):
        state[i] ^= rk[i]


def _sub_bytes(state, table):
    for i in range(16):
        state[i] = table[state[i]]


def _shift_rows(state, inv=False):
    out = list(state)
    for r in range(4):
        for c in range(4):
            if inv:  # inverse: row r shifts right
                out[r + 4 * ((c + r) % 4)] = state[r + 4 * c]
            else:  # forward: row r shifts left
                out[r + 4 * c] = state[r + 4 * ((c + r) % 4)]
    return out


def _mix_columns(state, inv=False):
    out = [0] * 16
    for c in range(4):
        col = state[4 * c : 4 * c + 4]
        if inv:
            m = ((_gmul(col[0], 14) ^ _gmul(col[1], 11) ^ _gmul(col[2], 13) ^ _gmul(col[3], 9)),
                 (_gmul(col[0], 9) ^ _gmul(col[1], 14) ^ _gmul(col[2], 11) ^ _gmul(col[3], 13)),
                 (_gmul(col[0], 13) ^ _gmul(col[1], 9) ^ _gmul(col[2], 14) ^ _gmul(col[3], 11)),
                 (_gmul(col[0], 11) ^ _gmul(col[1], 13) ^ _gmul(col[2], 9) ^ _gmul(col[3], 14)))
        else:
            m = ((_gmul(col[0], 2) ^ _gmul(col[1], 3) ^ col[2] ^ col[3]),
                 (col[0] ^ _gmul(col[1], 2) ^ _gmul(col[2], 3) ^ col[3]),
                 (col[0] ^ col[1] ^ _gmul(col[2], 2) ^ _gmul(col[3], 3)),
                 (_gmul(col[0], 3) ^ col[1] ^ col[2] ^ _gmul(col[3], 2)))
        out[4 * c : 4 * c + 4] = list(m)
    return out


def _aes_block(block, round_keys, nr, inv=False):
    state = list(block)
    if not inv:
        _add_round_key(state, round_keys[0])
        for r in range(1, nr):
            _sub_bytes(state, SBOX)
            state = _shift_rows(state)
            state = _mix_columns(state)
            _add_round_key(state, round_keys[r])
        _sub_bytes(state, SBOX)
        state = _shift_rows(state)
        _add_round_key(state, round_keys[nr])
    else:
        _add_round_key(state, round_keys[nr])
        for r in range(nr - 1, 0, -1):
            _sub_bytes(state, INV_SBOX)
            state = _shift_rows(state, inv=True)
            _add_round_key(state, round_keys[r])
            state = _mix_columns(state, inv=True)
        _sub_bytes(state, INV_SBOX)
        state = _shift_rows(state, inv=True)
        _add_round_key(state, round_keys[0])
    return bytes(state)


def aes_cbc_encrypt(key, iv, data):
    """AES-CBC over whole 16-byte blocks (data length % 16 == 0)."""
    round_keys, nr = _expand_key(key)
    prev = iv
    out = bytearray()
    for i in range(0, len(data), 16):
        blk = bytes(a ^ b for a, b in zip(data[i : i + 16], prev))
        prev = _aes_block(blk, round_keys, nr)
        out += prev
    return bytes(out)


def aes_cbc_decrypt(key, iv, data):
    round_keys, nr = _expand_key(key)
    prev = iv
    out = bytearray()
    for i in range(0, len(data), 16):
        blk = data[i : i + 16]
        dec = _aes_block(blk, round_keys, nr, inv=True)
        out += bytes(a ^ b for a, b in zip(dec, prev))
        prev = blk
    return bytes(out)


def pkcs7_pad(data):
    n = 16 - len(data) % 16
    return data + bytes([n]) * n


def pkcs7_unpad(data):
    if not data or len(data) % 16:
        raise ValueError("bad ciphertext length")
    n = data[-1]
    if not 1 <= n <= 16 or data[-n:] != bytes([n]) * n:
        raise ValueError("invalid PKCS#7 padding")
    return data[:-n]


# --- NIP-04 surface -------------------------------------------------------------


def _shared_x(priv_hex, pub_hex):
    return nip44.ecdh_x(bytes.fromhex(priv_hex), bytes.fromhex(pub_hex))


def nip04_encrypt(plaintext, priv_hex, pub_hex):
    """Standard NIP-04: key = sha256(x), random IV, base64 payload."""
    import secrets

    key = hashlib.sha256(_shared_x(priv_hex, pub_hex)).digest()
    iv = secrets.token_bytes(16)
    ct = aes_cbc_encrypt(key, iv, pkcs7_pad(plaintext.encode()))
    return (
        base64.b64encode(ct).decode()
        + "?iv="
        + base64.b64encode(iv).decode()
    )


def _decode_pair(payload):
    ct_s, iv_s = payload.split("?iv=", 1)
    # hex variant first: an all-hex IV is also valid base64 and would decode
    # to the wrong 24 bytes (zap test vectors use the all-hex form).
    def _is_hex(s):
        return len(s) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in s)

    if _is_hex(ct_s) and _is_hex(iv_s):
        try:
            return bytes.fromhex(ct_s), bytes.fromhex(iv_s)
        except ValueError:
            pass
    try:
        return base64.b64decode(ct_s), base64.b64decode(iv_s)
    except Exception:
        raise ValueError("unparseable nip04 payload")


def nip04_decrypt(payload, priv_hex, pub_hex):
    """Decrypt a NIP-04 payload. Tries the standard sha256(x) key first, then
    the raw-x variant (older generators, incl. our test vectors)."""
    ct, iv = _decode_pair(payload)
    if len(ct) % 16 or len(iv) != 16:
        raise ValueError("bad nip04 payload sizes")
    x = _shared_x(priv_hex, pub_hex)
    last_err = None
    for key in (hashlib.sha256(x).digest(), x):
        try:
            return pkcs7_unpad(aes_cbc_decrypt(key, iv, ct)).decode()
        except (ValueError, UnicodeDecodeError) as e:
            last_err = e
    raise ValueError("nip04 decrypt failed") from last_err
