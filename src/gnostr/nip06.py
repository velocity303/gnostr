"""NIP-06 key derivation from a BIP-39 mnemonic (power-user option, issue #16).

NIP-06 is flagged *unrecommended* by the NIP itself ("prefer a single nsec");
gnostr exposes it only as an explicit power-user option and the UI copy must
carry that warning. Pure functions, no GTK — unit-testable without mocks
(same contract as nip49.py / nip44.py).

Derivation per current NIP-06 + SLIP-0044 (Nostr entry, coin 1237'):
    m/44'/1237'/<account>'/0/0
The design spec's shorthand `m/1237h/0h/0h` refers to the same scheme; the
official NIP-06 test vectors only validate against the full SLIP-0044 path,
which is what this module implements (account defaults to 0).
"""

import hashlib
import hmac
import os

import ecdsa

CURVE = ecdsa.SECP256k1
N = CURVE.order
G = CURVE.generator

HARDENED = 0x80000000
WORDLIST_PATH = os.path.join(os.path.dirname(__file__), "bip39_wordlist_english.txt")


def _load_wordlist():
    with open(WORDLIST_PATH, encoding="utf-8") as f:
        words = f.read().split()
    if len(words) != 2048:
        raise RuntimeError(f"corrupt BIP-39 wordlist: {len(words)} words")
    return words


def bip39_seed(mnemonic, passphrase=""):
    """BIP-39 seed: PBKDF2-HMAC-SHA512(mnemonic NFKD, 'mnemonic'+passphrase, 2048, 64)."""
    import unicodedata

    password = unicodedata.normalize("NFKD", mnemonic.strip())
    salt = ("mnemonic" + passphrase).encode("utf-8")
    return hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, 2048, 64)


def validate_mnemonic(mnemonic):
    """Return the mnemonic normalized, or raise ValueError (bad word / bad checksum).

    BIP-39: checksum = first ENT/32 bits of SHA256(entropy), appended to the
    entropy, then split into 11-bit word indexes.
    """
    words = mnemonic.split()
    if len(words) not in (12, 15, 18, 21, 24):
        raise ValueError(
            "A BIP-39 mnemonic is 12, 15, 18, 21 or 24 words (got %d)." % len(words)
        )
    wordlist = _load_wordlist()
    index_of = {w: i for i, w in enumerate(wordlist)}
    bits = ""
    for w in words:
        if w not in index_of:
            raise ValueError(f"Unknown word: {w}")
        bits += format(index_of[w], "011b")
    ent = len(words) * 11 * 32 // 33
    cs = len(words) * 11 - ent
    entropy = int(bits[:ent], 2).to_bytes(ent // 8, "big")
    checksum = int(bits[ent:], 2)
    if (hashlib.sha256(entropy).digest()[0] >> (8 - cs)) != checksum:
        raise ValueError("Checksum mismatch — the mnemonic is mistyped or corrupt.")
    return " ".join(words)


def _ser32(i):
    return i.to_bytes(4, "big")


def _compress(point):
    x = point.x()
    return (b"\x03" if (point.y() & 1) else b"\x02") + x.to_bytes(32, "big")


def _ckd_priv(k, c, index):
    """BIP-32 CKDpriv (hardened when index >= 2^31)."""
    if index >= HARDENED:
        data = b"\x00" + k.to_bytes(32, "big") + _ser32(index)
    else:
        data = _compress(k * G) + _ser32(index)
    I = hmac.new(c, data, hashlib.sha512).digest()
    IL, IR = int.from_bytes(I[:32], "big"), I[32:]
    if IL >= N:
        raise ValueError("IL >= N")
    ki = (IL + k) % N
    if ki == 0:
        raise ValueError("ki == 0")
    return ki, IR


def bip32_derive(seed, path):
    """Derive the private scalar at a BIP-32 path like m/44'/1237'/0'/0/0.

    Hardened segments end with ' or h.
    """
    I = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    k, c = int.from_bytes(I[:32], "big"), I[32:]
    for part in path.split("/")[1:]:
        hardened = part.endswith(("h", "'"))
        idx = int(part.rstrip("h'"))
        if hardened:
            idx += HARDENED
        k, c = _ckd_priv(k, c, idx)
    return k


def derive_keypair(mnemonic, account=0, passphrase=""):
    """NIP-06 keypair at m/44'/1237'/<account>'/0/0.

    Returns (priv_hex, pub_hex). Validates the BIP-39 checksum first.
    """
    mnemonic = validate_mnemonic(mnemonic)
    seed = bip39_seed(mnemonic, passphrase)
    path = f"m/44h/1237h/{account}h/0/0"
    priv = bip32_derive(seed, path)
    if not 0 < priv < N:
        raise ValueError("derived scalar out of SEC2 range")
    pub = (priv * G).x()
    return "%064x" % priv, "%064x" % pub
