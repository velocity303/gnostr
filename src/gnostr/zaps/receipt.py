"""Zap receipt (kind 9735) verification + feed aggregates.

Spec zaps-payments-v1 §1.4: a 9735 is only rendered as a real zap after
all seven rules pass. This module is the whole client-side gate; the
event-level id/sig check (rule 7) reuses the NIP-01 verifier from
request.py, and the bolt11 amount (rule 3) uses the local decoder.

Public surface:
  verify_9735(receipt, lnurlp_nostr_pubkey=None) -> dict   (raises ZapReceiptError)
  receipt_dedupe_key(receipt) -> str                        (rule 6 key)
  aggregate_totals(verified) -> {"by_event": {...}, "by_profile": {...}}
"""

import hashlib
import json

from .bolt11 import Bolt11Error, decode_invoice
from .lnurl import ZapError, _is_hex32
from .request import verify_event_sig


class ZapReceiptError(ZapError):
    """Receipt failed §1.4 verification; message names the rule."""


def _tag(receipt, name):
    """First value of tag `name`, or None."""
    for t in receipt.get("tags", []):
        if len(t) >= 2 and t[0] == name:
            return t[1]
    return None


def verify_9735(receipt, lnurlp_nostr_pubkey=None):
    """Verify a kind-9735 receipt against spec §1.4 rules 1-7.

    `lnurlp_nostr_pubkey` is the LNURL server's nostrPubkey from §1.2
    metadata (hex). Pass it whenever known — rule 1 is skipped only when
    the caller has no metadata (e.g. offline replay), never silently.

    Returns dict on success:
      event_id, request_id, target_e, target_k, recipient, sender,
      amount_msat, comment, dedupe_key
    Raises ZapReceiptError naming the failed rule.
    """
    if not isinstance(receipt, dict) or receipt.get("kind") != 9735:
        raise ZapReceiptError("rule 7: not a 9735 event")

    # --- rule 7: event-level id + sig (NIP-01) ---------------------------
    if not verify_event_sig(receipt):
        raise ZapReceiptError("rule 7: bad event id or signature")

    # --- rule 1: signer is the LNURL server's nostrPubkey ----------------
    if lnurlp_nostr_pubkey is not None:
        if (
            not _is_hex32(lnurlp_nostr_pubkey)
            or receipt.get("pubkey") != lnurlp_nostr_pubkey
        ):
            raise ZapReceiptError("rule 1: signer is not the LNURL nostrPubkey")

    # --- rule 2: description == zap request, id + sig recomputed ---------
    desc = _tag(receipt, "description")
    if desc is None:
        raise ZapReceiptError("rule 2: missing description tag")
    try:
        request = json.loads(desc)
    except (ValueError, TypeError):
        raise ZapReceiptError("rule 2: description is not JSON")
    if not isinstance(request, dict) or request.get("kind") != 9734:
        raise ZapReceiptError("rule 2: description is not a 9734 zap request")
    if not verify_event_sig(request):
        raise ZapReceiptError("rule 2: zap request id/sig invalid")

    # --- rule 3: bolt11 present and amount >= request amount -------------
    bolt11 = _tag(receipt, "bolt11")
    if not bolt11:
        raise ZapReceiptError("rule 3: missing bolt11 tag")
    req_amount = None
    for t in request.get("tags", []):
        if len(t) >= 2 and t[0] == "amount":
            try:
                req_amount = int(t[1])
            except ValueError:
                raise ZapReceiptError("rule 3: request amount not an int")
    try:
        inv_msat = decode_invoice(bolt11)["amount_msat"]
    except Bolt11Error:
        raise ZapReceiptError("rule 3: bolt11 undecodable")
    if req_amount is not None and inv_msat < req_amount:
        raise ZapReceiptError(
            "rule 3: underpaid (%d msat < %d msat)" % (inv_msat, req_amount)
        )
    amount_msat = inv_msat if inv_msat else (req_amount or 0)

    # --- rule 4: recipient p tag and sender P tag bind the request -------
    req_p = _tag(request, "p")
    if _tag(receipt, "p") != req_p:
        raise ZapReceiptError("rule 4: p tag does not match request")
    if _tag(receipt, "P") != request.get("pubkey"):
        raise ZapReceiptError("rule 4: P tag does not match request sender")

    # --- rule 5: description-hash (when present) --------------------------
    dhash = _tag(receipt, "description-hash")
    if dhash is not None:
        if hashlib.sha256(desc.encode("utf-8")).hexdigest() != dhash:
            raise ZapReceiptError("rule 5: description-hash mismatch")

    # --- rule 6: dedupe key (bolt11 preferred, request id fallback) ------
    dedupe_key = bolt11 or request["id"]

    return {
        "event_id": receipt["id"],
        "request_id": request["id"],
        "target_e": _tag(request, "e"),
        "target_k": _tag(request, "k"),
        "recipient": req_p,
        "sender": request["pubkey"],
        "bolt11": bolt11,
        "amount_msat": amount_msat,
        "comment": request.get("content") or "",
        "dedupe_key": dedupe_key,
        "ts": int(receipt.get("created_at") or 0),
    }


def process_9735(receipt, db, metadata_lookup=None):
    """§1.4 gate + persistence in one call (client's 9735 branch entry).

    metadata_lookup(pubkey) -> LNURL server nostrPubkey (hex) or None;
    when a lookup is provided and comes up empty the receipt is REJECTED —
    rule 1 is mandatory and an unverified zap must be indistinguishable
    from an absent one. Returns (info, None) on success, (None, reason)
    on rejection; never raises for bad input.
    """
    try:
        recipient = _tag(receipt, "p") if isinstance(receipt, dict) else None
        if metadata_lookup is not None:
            lnurlp_pk = metadata_lookup(recipient) if recipient else None
            if not lnurlp_pk:
                return None, "rule 1: recipient LNURL metadata unavailable"
        else:
            lnurlp_pk = None
        info = verify_9735(receipt, lnurlp_pk)
    except ZapReceiptError as e:
        return None, str(e)
    except Exception as e:  # malformed event shapes
        return None, f"verify crashed: {e}"
    try:
        db.save_zap(info)
    except Exception as e:
        return None, f"persist failed: {e}"
    return info, None


def receipt_dedupe_key(receipt):
    """Rule 6 key without full verification (bolt11 tag, fallback the
    description's embedded request id). None if neither exists."""
    bolt11 = _tag(receipt, "bolt11")
    if bolt11:
        return bolt11
    desc = _tag(receipt, "description")
    if desc:
        try:
            return json.loads(desc).get("id")
        except (ValueError, TypeError):
            return None
    return None


def aggregate_totals(verified):
    """Feed aggregates over *verified* receipt dicts (verify_9735 output).

    Rule 6 dedupe applies first: one receipt per dedupe_key (first wins);
    distinct receipts for one request (split payments) all count toward
    the sum. Returns:
      {"by_event": {event_id: msat}, "by_profile": {pubkey: msat}}
    """
    seen = set()
    by_event = {}
    by_profile = {}
    for v in verified:
        key = v["dedupe_key"]
        if key in seen:
            continue
        seen.add(key)
        msat = v["amount_msat"]
        if v.get("target_e"):
            by_event[v["target_e"]] = by_event.get(v["target_e"], 0) + msat
        if v.get("recipient"):
            by_profile[v["recipient"]] = by_profile.get(v["recipient"], 0) + msat
    return {"by_event": by_event, "by_profile": by_profile}
