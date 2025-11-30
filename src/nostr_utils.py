import binascii
import time
import json
import hashlib

try:
    import ecdsa
except ImportError:
    ecdsa = None

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

# --- Bech32 Implementation (Keep existing code) ---
def _bech32_polymod(values):
    generator = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ value
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
    return hrp + '1' + ''.join([CHARSET[d] for d in combined])

def bech32_decode(bech):
    # FIX: Removed "or len(bech) > 90" check to support long Nostr entities
    if ((any(ord(x) < 33 or ord(x) > 126 for x in bech)) or
            (bech.lower() != bech and bech.upper() != bech)):
        return None, None
    bech = bech.lower()
    pos = bech.rfind('1')
    if pos < 1 or pos + 7 > len(bech):
        return None, None
    if not all(x in CHARSET for x in bech[pos+1:]):
        return None, None
    hrp = bech[:pos]
    data = [CHARSET.find(x) for x in bech[pos+1:]]
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
    if not nsec.startswith("nsec"): return None
    hrp, data = bech32_decode(nsec)
    if hrp != "nsec" or data is None: return None
    decoded = convertbits(data, 5, 8, False)
    if decoded is None: return None
    return bytes(decoded).hex()

def hex_to_nsec(hex_key):
    if len(hex_key) != 64: return None
    try:
        data = bytes.fromhex(hex_key)
    except ValueError: return None
    five_bit_data = convertbits(data, 8, 5, True)
    if five_bit_data is None: return None
    return bech32_encode("nsec", five_bit_data)

def is_valid_hex_key(key_str):
    if len(key_str) != 64: return False
    try:
        int(key_str, 16)
        return True
    except ValueError: return False

def get_public_key(priv_key_hex):
    if not ecdsa: return None
    try:
        sk = ecdsa.SigningKey.from_string(bytes.fromhex(priv_key_hex), curve=ecdsa.SECP256k1)
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

# --- Signing Logic ---

def compute_event_id(event):
    data = [
        0,
        event['pubkey'],
        event['created_at'],
        event['kind'],
        event['tags'],
        event['content']
    ]
    json_str = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(json_str.encode('utf-8')).hexdigest()

def sign_event(event, priv_key_hex):
    if not ecdsa:
        print("Error: ECDSA not available for signing.")
        return None

    try:
        event['id'] = compute_event_id(event)
        sk = ecdsa.SigningKey.from_string(bytes.fromhex(priv_key_hex), curve=ecdsa.SECP256k1)
        sig_bytes = sk.sign_digest(bytes.fromhex(event['id']), sigencode=ecdsa.util.sigencode_schnorr)
        event['sig'] = sig_bytes.hex()
        return event
    except Exception as e:
        print(f"Signing Error: {e}")
        return None

def is_nostr_reference(text):
    prefixes = ("nostr:nevent", "nostr:nprofile", "nostr:note", "nostr:npub")
    return text.startswith(prefixes)

def extract_id_from_nostr_uri(uri):
    if uri.startswith("nostr:"):
        return uri.split(":")[1]
    return uri
