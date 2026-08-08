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
    root_id = None
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
    p = curve.curve.p()
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
    prefixes = ("nostr:nevent", "nostr:nprofile", "nostr:note", "nostr:npub")
    return text.startswith(prefixes)


def extract_id_from_nostr_uri(uri):
    if uri.startswith("nostr:"):
        return uri.split(":")[1]
    return uri


def parse_ok_message(msg):
    """Parse a NIP-01 relay OK message: ['OK', <event_id>, <true|false>, <msg>].
    Returns (event_id, accepted:bool, message:str) or None if not an OK."""
    if not isinstance(msg, list) or len(msg) < 3 or msg[0] != "OK":
        return None
    event_id = msg[1]
    accepted = bool(msg[2]) if isinstance(msg[2], bool) else str(msg[2]) == "true"
    message = msg[3] if len(msg) > 3 else ""
    return (event_id, accepted, message)
