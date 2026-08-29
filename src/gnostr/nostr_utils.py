import hashlib
import json
import time

import ecdsa


# --- Npub Conversion ---
def hex_to_npub(hex_key):
    """Convert hex public key to npub (bech32)."""
    if not hex_key or len(hex_key) != 64:
        return None
    try:
        data = bytes.fromhex(hex_key)
        five_bit_data = convertbits(data, 8, 5, True)
        if five_bit_data is None:
            return None
        return bech32_encode("npub", five_bit_data)
    except Exception:
        return None


CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values):
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def bech32_verify_checksum(hrp, data):
    return _bech32_polymod(_bech32_hrp_expand(hrp) + data) == 1


def bech32_create_checksum(hrp, data):
    values = _bech32_hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def bech32_encode(hrp, data):
    combined = data + bech32_create_checksum(hrp, data)
    return hrp + "1" + "".join([CHARSET[d] for d in combined])


def bech32_decode(bech):
    if (any(ord(x) < 33 or ord(x) > 126 for x in bech)) or (
        bech.lower() != bech and bech.upper() != bech
    ):
        return None, None
    bech = bech.lower()
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech):
        return None, None
    if not all(x in CHARSET for x in bech[pos + 1 :]):
        return None, None
    hrp = bech[:pos]
    data = [CHARSET.find(x) for x in bech[pos + 1 :]]
    if not bech32_verify_checksum(hrp, data):
        return None, None
    return hrp, data[:-6]


def convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


# --- Key Utils ---


def nsec_to_hex(nsec):
    if not nsec.startswith("nsec"):
        return None
    hrp, data = bech32_decode(nsec)
    if hrp != "nsec" or data is None:
        return None
    decoded = convertbits(data, 5, 8, False)
    if decoded is None:
        return None
    return bytes(decoded).hex()


def npub_to_hex(npub):
    """Decode a NIP-19 npub into a 64-char hex pubkey. Returns None if invalid."""
    if not npub or not npub.startswith("npub"):
        return None
    hrp, data = bech32_decode(npub)
    if hrp != "npub" or data is None:
        return None
    decoded = convertbits(data, 5, 8, False)
    if decoded is None:
        return None
    return bytes(decoded).hex()


def hex_to_nsec(hex_key):
    if len(hex_key) != 64:
        return None
    try:
        data = bytes.fromhex(hex_key)
    except ValueError:
        return None
    five_bit_data = convertbits(data, 8, 5, True)
    if five_bit_data is None:
        return None
    return bech32_encode("nsec", five_bit_data)


def is_valid_hex_key(key_str):
    if len(key_str) != 64:
        return False
    try:
        int(key_str, 16)
        return True
    except ValueError:
        return False


def get_public_key(priv_key_hex):
    if not ecdsa:
        return None
    try:
        sk = ecdsa.SigningKey.from_string(
            bytes.fromhex(priv_key_hex), curve=ecdsa.SECP256k1
        )
        vk = sk.verifying_key
        compressed = vk.to_string("compressed")
        return compressed[1:].hex()
    except Exception as e:
        print(f"Key derivation error: {e}")
        return None


def extract_followed_pubkeys(event_json):
    tags = event_json.get("tags", [])
    followed = []
    for tag in tags:
        if len(tag) >= 2 and tag[0] == "p":
            followed.append(tag[1])
    return followed


def get_thread_root(tags):
    """Finds the root event ID from tags based on NIP-10."""
    first_e = None

    for t in tags:
        if t[0] == "e":
            if not first_e:
                first_e = t[1]
            # Check for explicit root marker
            if len(t) >= 4 and t[3] == "root":
                return t[1]

    # Fallback to first 'e' tag if no marker found (NIP-10 legacy)
    return first_e


# --- BIP-340 Signing Logic (Using ecdsa lib primitives) ---


def compute_event_id(event):
    data = [
        0,
        event["pubkey"],
        event["created_at"],
        event["kind"],
        event["tags"],
        event["content"],
    ]
    json_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(json_str.encode("utf-8")).hexdigest()


def sha256(b):
    return hashlib.sha256(b).digest()


def tagged_hash(tag, data):
    tag_hash = sha256(tag.encode())
    return sha256(tag_hash + tag_hash + data)


def schnorr_sign_with_key(msg_bytes, sk):
    curve = sk.curve
    n = curve.order
    G_point = curve.generator
    d0 = sk.privkey.secret_multiplier

    P_point = sk.verifying_key.pubkey.point

    if P_point.y() % 2 != 0:
        d0 = n - d0

    t_data = d0.to_bytes(32, "big") + msg_bytes
    aux = sha256(t_data)
    t = d0 ^ int.from_bytes(tagged_hash("BIP0340/aux", aux), "big")

    rand = tagged_hash(
        "BIP0340/nonce",
        t.to_bytes(32, "big") + P_point.x().to_bytes(32, "big") + msg_bytes,
    )
    k = int.from_bytes(rand, "big") % n
    if k == 0:
        raise ValueError("Failure in nonce generation")

    R_point = G_point * k
    if R_point.y() % 2 != 0:
        k = n - k

    e_bytes = tagged_hash(
        "BIP0340/challenge",
        R_point.x().to_bytes(32, "big") + P_point.x().to_bytes(32, "big") + msg_bytes,
    )
    e = int.from_bytes(e_bytes, "big") % n

    s = (k + e * d0) % n

    return R_point.x().to_bytes(32, "big").hex() + s.to_bytes(32, "big").hex()


def sign_event(event, priv_key_hex):
    if not ecdsa:
        print("Error: ECDSA not available.")
        return None

    try:
        event["id"] = compute_event_id(event)
        sk = ecdsa.SigningKey.from_string(
            bytes.fromhex(priv_key_hex), curve=ecdsa.SECP256k1
        )
        event["sig"] = schnorr_sign_with_key(bytes.fromhex(event["id"]), sk)
        return event
    except Exception as e:
        print(f"Signing Error: {e}")
        return None


# --- Event builders (protocol layer) -----------------------------------------
# These are pure functions that assemble the UNSIGNED event dict with the tag
# layout each NIP mandates. They live here (not in client.py) so the tag
# structure is unit-testable without GTK, and so the protocol contract is
# centralized — refactors touch one place. client.py calls these, then signs
# via sign_event() and publishes.


def build_event(pubkey, kind, content, tags, created_at=None):
    """Assemble an unsigned Nostr event. `tags` is a list of tag lists already
    in NIP order. Returns the dict ready for sign_event()."""
    return {
        "pubkey": pubkey,
        "created_at": created_at if created_at is not None else int(time.time()),
        "kind": kind,
        "tags": tags,
        "content": content,
    }


def build_reaction_event(
    pubkey, target_event_id, target_pubkey, content="+", created_at=None
):
    """NIP-25: kind-7 reaction. content is a single char/emoji — '+' adds,
    '-' removes/undo. Tags MUST be [["e", target_id], ["p", target_pubkey]]."""
    tags = [["e", target_event_id], ["p", target_pubkey]]
    return build_event(pubkey, 7, content, tags, created_at)


def build_repost_event(
    pubkey, target_event_id, target_pubkey, target_kind, original_event, created_at=None
):
    """NIP-18: kind-6 generic repost. content = raw JSON of the original
    event; tags MUST be [["k", str(kind)], ["e", id], ["p", pubkey]]."""
    content = json.dumps(original_event, separators=(",", ":"))
    tags = [
        ["k", str(target_kind)],
        ["e", target_event_id],
        ["p", target_pubkey],
    ]
    return build_event(pubkey, 6, content, tags, created_at)


def build_reply_event(
    pubkey, content, root_id, root_pubkey, parent_id, parent_pubkey, created_at=None
):
    """NIP-01: kind-1 threaded reply. First `e` tag = thread ROOT, last `e`
    tag = DIRECT parent (the app resolves replies by the last e-tag); `p` tags
    name the root + parent authors. Order matters for both metrics (first e-tag)
    and the reply tree (last e-tag)."""
    tags = [
        ["e", root_id],
        ["e", parent_id],
        ["p", root_pubkey],
        ["p", parent_pubkey],
    ]
    return build_event(pubkey, 1, content, tags, created_at)


def is_nostr_reference(text):
    prefixes = (
        "nostr:nevent",
        "nostr:nprofile",
        "nostr:naddr",
        "nostr:nrelay",
        "nostr:note",
        "nostr:npub",
    )
    return text.startswith(prefixes)


def resolve_profile_identifier(text):
    """Map a pasted Nostr identifier to a 64-char hex pubkey, or None.

    Supports npub, nprofile, nsec (own key), raw 64-char hex, and a `nostr:`
    URI prefix. nevent/naddr resolve to their author pubkey when present.
    """
    if not text:
        return None
    s = text.strip()
    if s.lower().startswith("nostr:"):
        s = s[6:].strip()
    if not s:
        return None
    if is_valid_hex_key(s):
        return s
    if s.startswith("npub"):
        return npub_to_hex(s)
    if s.startswith("nprofile"):
        r = decode_nprofile(s)
        return r[0] if r else None
    if s.startswith("nsec"):
        priv = nsec_to_hex(s)
        return get_public_key(priv) if priv else None
    if s.startswith("nevent"):
        r = decode_nevent_full(s)
        return r[2] if r and r[2] else None
    if s.startswith("naddr"):
        r = decode_naddr(s)
        return r[1] if r and r[1] else None
    return None


def extract_id_from_nostr_uri(uri):
    if uri.startswith("nostr:"):
        return uri.split(":")[1]
    return uri


# --- NIP-19 TLV decoding -----------------------------------------------------
# Shareable identifiers (nprofile, nevent, naddr) carry a binary TLV list:
# each item is [T (1 byte)][L (1 byte)][V (L bytes)]. Standardized types:
#   0 = special (nprofile->pubkey, nevent->event id, naddr->d-tag)
#   1 = relay (ascii URL, may repeat)
#   2 = author (32-byte pubkey)
#   3 = kind (32-bit big-endian unsigned int)
# Per NIP-19, unknown TLVs are ignored, not errors.


def _decode_tlv(raw_bytes):
    """Parse a NIP-19 TLV byte stream into a list of (type, value_bytes).
    Malformed items (truncated length) are skipped, not fatal."""
    items = []
    i = 0
    while i < len(raw_bytes):
        if i + 2 > len(raw_bytes):
            break
        t = raw_bytes[i]
        ln = raw_bytes[i + 1]
        if i + 2 + ln > len(raw_bytes):
            break
        items.append((t, raw_bytes[i + 2 : i + 2 + ln]))
        i += 2 + ln
    return items


def _tlv_first(items, t):
    for typ, val in items:
        if typ == t:
            return val
    return None


def _tlv_all(items, t):
    return [val for typ, val in items if typ == t]


def decode_naddr(bech32):
    """Decode a NIP-19 naddr into (kind, pubkey, d_tag, relays[]).
    Returns None if the bech32 isn't a valid naddr. d_tag is '' for normal
    replaceable events (empty d-tag)."""
    hrp, data = bech32_decode(bech32)
    if hrp != "naddr" or data is None:
        return None
    raw = bytes(convertbits(data, 5, 8, False) or b"")
    items = _decode_tlv(raw)
    d_tag = bytes(_tlv_first(items, 0) or b"").decode("utf-8", "replace")
    pubkey = bytes(_tlv_first(items, 2) or b"").hex()
    kind_bytes = _tlv_first(items, 3)
    kind = int.from_bytes(kind_bytes, "big") if kind_bytes else 0
    relays = [bytes(v).decode("utf-8", "replace") for v in _tlv_all(items, 1)]
    return (kind, pubkey, d_tag, relays)


def decode_nevent_full(bech32):
    """Decode a NIP-19 nevent into (event_id, relays[], author, kind).
    Returns None if not a valid nevent. author/kind are optional per NIP-19."""
    hrp, data = bech32_decode(bech32)
    if hrp != "nevent" or data is None:
        return None
    raw = bytes(convertbits(data, 5, 8, False) or b"")
    items = _decode_tlv(raw)
    event_id = bytes(_tlv_first(items, 0) or b"").hex()
    relays = [bytes(v).decode("utf-8", "replace") for v in _tlv_all(items, 1)]
    author = bytes(_tlv_first(items, 2) or b"").hex()
    kind_bytes = _tlv_first(items, 3)
    kind = int.from_bytes(kind_bytes, "big") if kind_bytes else None
    return (event_id, relays, author, kind)


def decode_nprofile(bech32):
    """Decode a NIP-19 nprofile into (pubkey, relays[]). Returns None if not
    a valid nprofile."""
    hrp, data = bech32_decode(bech32)
    if hrp != "nprofile" or data is None:
        return None
    raw = bytes(convertbits(data, 5, 8, False) or b"")
    items = _decode_tlv(raw)
    pubkey = bytes(_tlv_first(items, 0) or b"").hex()
    relays = [bytes(v).decode("utf-8", "replace") for v in _tlv_all(items, 1)]
    return (pubkey, relays)


def parse_ok_message(msg):
    """Parse a NIP-01 relay OK message: ['OK', <event_id>, <true|false>, <msg>].
    Returns (event_id, accepted:bool, message:str) or None if not an OK."""
    if not isinstance(msg, list) or len(msg) < 3 or msg[0] != "OK":
        return None
    event_id = msg[1]
    accepted = bool(msg[2]) if isinstance(msg[2], bool) else str(msg[2]) == "true"
    message = msg[3] if len(msg) > 3 else ""
    return (event_id, accepted, message)
