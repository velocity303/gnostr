"""Minimal BOLT-11 amount + header decoder (spec zaps-payments-v1 §4).

Stdlib-only (no new pip deps). Two jobs, both exact-integer:

* decode the hrp amount (digits + multiplier table m/u/n/p) into msat —
  float math is never used, so the `p` (pico-BTC) multiplier can't drift;
* read the network prefix, timestamp, payment_hash (t field) and short
  description so callers can sanity-check an invoice.

Signature verification is deliberately NOT implemented: a 9735 receipt's
amount is bound to the zap request by §1.4 rule 3 and attested by the
LNURL server's signature over the whole receipt — a forger can't produce
that pairing. Decoding is all the client needs to render sats.
"""

import re

# bech32 charset -> 5-bit value (BOLT-11 data part).
_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_CHARSET_MAP = {c: i for i, c in enumerate(_CHARSET)}

# hrp amount multipliers as an exact (num, den) msat factor per digit
# (1 BTC = 10^11 msat; m=10^-3, u=10^-6, n=10^-9, p=10^-12 BTC):
_MULT_MSAT = {"m": (10**8, 1), "u": (10**5, 1), "n": (10**2, 1), "p": (1, 10)}

_NETWORKS = {"bc": "mainnet", "tb": "testnet", "bcrt": "regtest"}

_HRP_RE = re.compile(r"^ln(bc|tb|bcrt)(\d*)([mnup]?)$")


class Bolt11Error(ValueError):
    """Invoice is not a decodable BOLT-11 string."""


def _bech32_hrp_split(inv):
    """BOLT-11 hrp is everything before the LAST '1' (data part is bech32)."""
    pos = inv.rfind("1")
    if pos < 4:
        raise Bolt11Error("not a bolt11 invoice")
    return inv[:pos], inv[pos + 1 :]


def decode_invoice(bolt11):
    """Decode a BOLT-11 invoice header.

    Returns dict:
      network      'mainnet' | 'testnet' | 'regtest'
      amount_msat  int (exact; 0 when the invoice encodes no amount)
      amount_sat   float (msat / 1000)
      timestamp    int (seconds; 0 when the t field is absent)
      payment_hash hex str (64 chars) or None
      description  str or None (short-description 'd' field)

    Raises Bolt11Error on a malformed string.
    """
    if not isinstance(bolt11, str) or not bolt11:
        raise Bolt11Error("not a bolt11 invoice")
    if bolt11 != bolt11.lower() and bolt11 != bolt11.upper():
        raise Bolt11Error("mixed-case bech32")
    low = bolt11.lower()
    hrp, data = _bech32_hrp_split(low)
    m = _HRP_RE.match(hrp)
    if not m:
        raise Bolt11Error("unknown bolt11 prefix %r" % hrp)
    network = _NETWORKS[m.group(1)]

    # --- amount (hrp): digits * multiplier, exact integer msat -----------
    digits, mult = m.group(2), m.group(3)
    if digits and not mult:
        raise Bolt11Error("amount digits without multiplier")
    if mult == "p" and digits and int(digits) % 10:
        raise Bolt11Error("pico-bitcoin amount not a whole satoshi")
    amount_msat = 0
    if digits:
        num, den = _MULT_MSAT[mult]
        scaled = int(digits) * num
        if scaled % den:
            raise Bolt11Error("amount not a whole millisatoshi")
        amount_msat = scaled // den
    amount_sat = amount_msat / 1000

    # --- tagged fields (data part, 5-bit values) --------------------------
    vals = []
    for ch in data:
        if ch not in _CHARSET_MAP:
            raise Bolt11Error("invalid bech32 character")
        vals.append(_CHARSET_MAP[ch])
    timestamp = 0
    if len(vals) >= 7:
        for v in vals[:7]:
            timestamp = timestamp * 32 + v

    payment_hash = None
    description = None
    i = 7
    # tail = signature (103 values) + recovery flag (1) + bech32 checksum (6)
    end = max(len(vals) - 110, i)
    while i + 2 <= end:
        tag = vals[i]
        ln = vals[i + 1] * 32 + vals[i + 2]  # length in 5-bit groups
        start = i + 3
        stop = start + ln
        if stop > end:
            break
        if tag == 13:  # charset index of 'd' = short description
            description = _decode_8(vals[start:stop]).decode("utf-8", "replace")
        elif tag == 1:  # charset index of 'p' = payment_hash
            payment_hash = _decode_8(vals[start:stop]).hex()
        i = stop
    return {
        "network": network,
        "amount_msat": amount_msat,
        "amount_sat": amount_sat,
        "timestamp": timestamp,
        "payment_hash": payment_hash,
        "description": description,
    }


def amount_msat(bolt11):
    """Invoice amount in msat (0 = no amount encoded). Bolt11Error on junk."""
    return decode_invoice(bolt11)["amount_msat"]


def _decode_8(vals):
    """5-bit value list -> bytes (trailing pad bits dropped)."""
    acc = 0
    nbits = 0
    out = bytearray()
    for v in vals:
        acc = (acc << 5) | v
        nbits += 5
        if nbits >= 8:
            nbits -= 8
            out.append((acc >> nbits) & 0xFF)
    return bytes(out)
