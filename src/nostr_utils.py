# src/nostr_utils.py
# Pure Python implementation of Bech32 (NIP-19)
import binascii

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

def _bech32_polymod(values):
    """Internal function that computes the Bech32 checksum."""
    generator = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ value
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk

def _bech32_hrp_expand(hrp):
    """Expand the HRP into values for checksum computation."""
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]

def bech32_verify_checksum(hrp, data):
    """Verify a checksum given HRP and data."""
    return _bech32_polymod(_bech32_hrp_expand(hrp) + data) == 1

def bech32_create_checksum(hrp, data):
    """Compute the checksum values given HRP and data."""
    values = _bech32_hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]

def bech32_encode(hrp, data):
    """Compute a Bech32 string given HRP and data values."""
    combined = data + bech32_create_checksum(hrp, data)
    return hrp + '1' + ''.join([CHARSET[d] for d in combined])

def bech32_decode(bech):
    """Validate a Bech32 string, and determine HRP and data."""
    if ((any(ord(x) < 33 or ord(x) > 126 for x in bech)) or
            (bech.lower() != bech and bech.upper() != bech)):
        return None, None
    bech = bech.lower()
    pos = bech.rfind('1')
    if pos < 1 or pos + 7 > len(bech) or len(bech) > 90:
        return None, None
    if not all(x in CHARSET for x in bech[pos+1:]):
        return None, None
    hrp = bech[:pos]
    data = [CHARSET.find(x) for x in bech[pos+1:]]
    if not bech32_verify_checksum(hrp, data):
        return None, None
    return hrp, data[:-6]

def convertbits(data, frombits, tobits, pad=True):
    """General power-of-2 base conversion."""
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

# --- Public Helper Functions ---

def nsec_to_hex(nsec):
    """
    Decodes an 'nsec1...' string into a raw 64-character hex string.
    Returns None if invalid.
    """
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
    """
    Encodes a 64-character hex string into 'nsec1...' format.
    Returns None if invalid.
    """
    if len(hex_key) != 64:
        return None

    try:
        data = bytes.fromhex(hex_key)
    except ValueError:
        return None

    # Convert 8-bit bytes to 5-bit groups
    five_bit_data = convertbits(data, 8, 5, True)
    if five_bit_data is None:
        return None

    return bech32_encode("nsec", five_bit_data)

def is_valid_hex_key(key_str):
    """Simple check if a string is a valid 64-char hex key."""
    if len(key_str) != 64:
        return False
    try:
        int(key_str, 16)
        return True
    except ValueError:
        return False
