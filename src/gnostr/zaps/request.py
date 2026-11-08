"""Kind-9734 zap request builder + LNURL callback client (spec §1.3).

The 9734 is signed with the sender's MAIN key — the one payment-adjacent
operation that uses it (§3.1): it signs an attestation, not a payment, and
is NEVER published to relays. Payment authority stays with the NWC secret.
"""

import hashlib
import json
import urllib.error
import urllib.parse

from gnostr import proxy_opener
from gnostr.nostr_utils import build_event, sign_event
from gnostr.zaps.lnurl import ZapHTTPError, lnurl_to_url

_HTTP_TIMEOUT_S = 10.0

# BIP-340 / secp256k1 curve params (affine verifier below is stdlib-only).
_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8


def _sha(b):
    return hashlib.sha256(b).digest()


def _tagged(tag, data):
    h = _sha(tag.encode())
    return _sha(h + h + data)


def _pt_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if p1 == p2:
        lam = 3 * x1 * x1 * pow(2 * y1, _P - 2, _P) % _P
    else:
        lam = (y2 - y1) * pow(x2 - x1, _P - 2, _P) % _P
    x3 = (lam * lam - x1 - x2) % _P
    y3 = (lam * (x1 - x3) - y1) % _P
    return (x3, y3)


def _pt_mul(k, pt):
    res = None
    while k:
        if k & 1:
            res = _pt_add(res, pt)
        pt = _pt_add(pt, pt)
        k >>= 1
    return res


def _lift_x(x):
    y2 = (pow(x, 3, _P) + 7) % _P
    y = pow(y2, (_P + 1) // 4, _P)
    if (y * y) % _P != y2:
        return None
    return (x, y if y % 2 == 0 else _P - y)


def schnorr_verify(msg, pubkey32, sig64):
    """BIP-340 verification (pure stdlib). msg/pubkey/sig are bytes."""
    if len(pubkey32) != 32 or len(sig64) != 64:
        return False
    pk = _lift_x(int.from_bytes(pubkey32, "big"))
    if pk is None:
        return False
    r = int.from_bytes(sig64[:32], "big")
    s = int.from_bytes(sig64[32:], "big")
    if r >= _P or s >= _N:
        return False
    e = (
        int.from_bytes(_tagged("BIP0340/challenge", sig64[:32] + pubkey32 + msg), "big")
        % _N
    )
    R = _pt_add(_pt_mul(s, (_GX, _GY)), _pt_mul((_N - e) % _N, pk))
    return R is not None and R[1] % 2 == 0 and R[0] == r


def verify_event_sig(event):
    """NIP-01 event-level check: recompute id, verify BIP-340 sig."""
    try:
        data = [
            0,
            event["pubkey"],
            event["created_at"],
            event["kind"],
            event["tags"],
            event["content"],
        ]
        canon = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
        if digest != event.get("id"):
            return False
        return schnorr_verify(
            bytes.fromhex(digest),
            bytes.fromhex(event["pubkey"]),
            bytes.fromhex(event.get("sig", "")),
        )
    except (KeyError, TypeError, ValueError):
        return False


def build_9734(
    sender_pubkey,
    recipient_pubkey,
    amount_msat,
    lnurl_bech32,
    relays,
    event=None,
    comment="Zap!",
    created_at=None,
    priv_key_hex=None,
):
    """Assemble (and optionally sign) the kind-9734 zap request per §1.3.

    Tag order is normative: relays, amount, lnurl, p, then e/k for note-zaps.
    `relays` = the user's configured WRITE relays (same set DMs use) — where
    the sender wants the 9735 receipt. `event` = (id, kind) tuple for a
    note-zap, or None for a profile zap. NEVER publish the result to relays.
    """
    tags = [
        ["relays", *relays],
        ["amount", str(int(amount_msat))],
        ["lnurl", lnurl_bech32],
        ["p", recipient_pubkey],
    ]
    if event:
        event_id, event_kind = event
        tags.append(["e", event_id])
        tags.append(["k", str(event_kind)])
    ev = build_event(
        sender_pubkey,
        9734,
        comment if comment is not None else "Zap!",
        tags,
        created_at=created_at,
    )
    if priv_key_hex:
        return sign_event(ev, priv_key_hex)
    return ev


def canonical_event_json(event):
    """Byte-exact canonical serialization sent in the callback's `nostr`
    param: key order kind, pubkey, created_at, tags, content, id, sig
    (fixture v2_zap_request_canonical_json)."""
    ordered = {
        "kind": event["kind"],
        "pubkey": event["pubkey"],
        "created_at": event["created_at"],
        "tags": event["tags"],
        "content": event["content"],
        "id": event["id"],
        "sig": event["sig"],
    }
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)


def build_callback_url(callback, signed_event, lnurl_bech32, comment=None):
    """`<callback>?amount=<msat>&nostr=<urlencoded canonical>[&comment=..]`
    (+ lnurl param, per fixture v2_callback_url). quote(safe='') — ':' '/'
    '!' must not survive unescaped."""
    amount = ""
    for tag in signed_event.get("tags", []):
        if tag and tag[0] == "amount":
            amount = tag[1]
            break
    canon = canonical_event_json(signed_event)
    url = (
        f"{callback}?amount={amount}"
        f"&nostr={urllib.parse.quote(canon, safe='')}"
        f"&lnurl={urllib.parse.quote(lnurl_bech32, safe='')}"
    )
    if comment:
        url += f"&comment={urllib.parse.quote(comment, safe='')}"
    return url


def fetch_invoice(
    callback,
    signed_event,
    lnurl_bech32,
    comment=None,
    settings=None,
    timeout=_HTTP_TIMEOUT_S,
):
    """GET the LNURL callback and parse {pr, successAction} (§1.3).

    Returns the parsed dict; raises ZapHTTPError on transport/shape errors.
    The signed 9734 travels ONLY to this callback — never a relay.
    """
    url = build_callback_url(callback, signed_event, lnurl_bech32, comment)
    opener = proxy_opener.proxy_opener(settings)
    req = urllib.request.Request(url, headers={"User-Agent": "gnostr/1.0"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise ZapHTTPError(f"LNURL callback error {e.code}") from e
    except Exception as e:
        raise ZapHTTPError(f"LNURL callback unreachable: {e}") from e
    try:
        data = json.loads(body)
    except ValueError:
        raise ZapHTTPError("LNURL callback returned invalid JSON") from None
    if not isinstance(data, dict):
        raise ZapHTTPError("LNURL callback returned unexpected response")
    if data.get("status", "OK") != "OK":
        raise ZapHTTPError(data.get("reason") or "LNURL callback rejected the zap")
    if not data.get("pr"):
        raise ZapHTTPError("LNURL callback returned no invoice")
    return data
