# Minimal Bech32 implementation for NSEC/NPUB
# Sourced from reference implementation
# (Ideally, replace this with the 'nostr' python package in the future)

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

def bech32_decode(bech_string):
    """Decode a bech32 string."""
    if ((any(ord(x) < 33 or ord(x) > 126 for x in bech_string)) or
            (bech_string.lower() != bech_string and bech_string.upper() != bech_string)):
        return None
    bech_string = bech_string.lower()
    pos = bech_string.rfind('1')
    if pos < 1 or pos + 7 > len(bech_string) or len(bech_string) > 90:
        return None
    if not all(x in CHARSET for x in bech_string[pos+1:]):
        return None
    hrp = bech_string[:pos]
    data = [CHARSET.find(x) for x in bech_string[pos+1:]]
    # (Checksum validation omitted for brevity in this snippet, add full implementation for prod)
    return (hrp, data)
