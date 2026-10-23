"""LNURL-pay target resolution + metadata gate (spec zaps-payments-v1 §1.1-1.2).

Target precedence (§1.1, de-facto consensus order, client-side rule):
  lud16 (lightning address) -> lud06 (bech32 lnurl) -> NIP-05 fallback -> None.
All HTTP goes through the shared proxy-aware opener so .onion LNURL hosts work
over the configured Tor SOCKS5 endpoint (gnostr-tor-onion-support-v1 pattern).
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from gnostr import proxy_opener
from gnostr.nostr_utils import bech32_decode, bech32_encode, convertbits

# §1.2: cache payRequest metadata per lnurl (receipt verification reuses it).
METADATA_TTL_S = 3600.0
_HTTP_TIMEOUT_S = 10.0

_metadata_cache = {}  # lnurl -> (expiry_ts, metadata dict)


class ZapError(Exception):
    """Base for zap-send failures; message is user-facing copy."""


class NotZapableError(ZapError):
    """No LNURL target, or the endpoint fails the §1.2 zap gate."""


class ProfileSharingRequired(ZapError):
    """LNURL server demands a mandatory payerData field (spec §1.2)."""


class ZapHTTPError(ZapError):
    """LNURL endpoint unreachable / bad response."""


def _is_hex32(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value.lower())
    )


def url_to_lnurl(url):
    """bech32-encode an LNURL-pay URL (LUD-06 form)."""
    return bech32_encode("lnurl", convertbits(url.encode("utf-8"), 8, 5))


def lnurl_to_url(lnurl):
    """Accept a bech32 `lnurl1...` or a bare http(s) URL; return the URL."""
    if isinstance(lnurl, str) and lnurl.startswith(("http://", "https://")):
        return lnurl
    hrp, data = bech32_decode(lnurl)
    if hrp != "lnurl" or not data:
        raise NotZapableError("recipient doesn't support zaps")
    try:
        url = bytes(convertbits(data, 5, 8, False)).decode("utf-8")
    except Exception:
        raise NotZapableError("recipient doesn't support zaps") from None
    if not url.startswith(("http://", "https://")):
        raise NotZapableError("recipient doesn't support zaps")
    return url


def _lnurlp_url_for_address(address):
    """LUD-16: lightning address user@domain -> lnurlp URL. LUD-16 explicitly
    allows onion services, which are served over plain http."""
    user, _, domain = address.rpartition("@")
    if not user or not domain:
        return None
    scheme = "http" if domain.endswith(".onion") else "https"
    return f"{scheme}://{domain}/.well-known/lnurlp/{user}"


def resolve_target(kind0_content):
    """Resolve a kind-0 profile (JSON string or dict) to a bech32 LNURL.

    Returns the lnurl string, or None when the profile is not zapable (§1.1
    step 4 — the UI hides the zap button).
    """
    if isinstance(kind0_content, str):
        try:
            data = json.loads(kind0_content)
        except (ValueError, TypeError):
            return None
    else:
        data = kind0_content or {}
    if not isinstance(data, dict):
        return None

    # 1. lud16 (lightning address)
    lud16 = (data.get("lud16") or "").strip()
    if "@" in lud16:
        url = _lnurlp_url_for_address(lud16)
        if url:
            return url_to_lnurl(url)

    # 2. lud06 (bech32 lnurl) — validate it decodes to an http(s) URL
    lud06 = (data.get("lud06") or "").strip()
    if lud06:
        try:
            lnurl_to_url(lud06)
        except NotZapableError:
            pass
        else:
            return lud06

    # 3. NIP-05 fallback — CONVENTION, not spec (spec §1.1 step 3, James'
    #    product call 2026-10-21): if nip05 is user@domain, try it as a
    #    lightning address. "_" (NIP-05 standard local-part) is not an
    #    addressable lightning identity, so skip it.
    nip05 = (data.get("nip05") or "").strip()
    if "@" in nip05:
        user, _, domain = nip05.rpartition("@")
        if user and user != "_":
            url = _lnurlp_url_for_address(nip05)
            if url:
                return url_to_lnurl(url)

    # 4. not zapable
    return None


def zapable_profile(profile):
    """Cheap UI gate: does this profile dict carry any §1.1 candidate?

    The DB profile row has no lud06 column, so visibility keys off lud16/nip05;
    the authoritative gate is resolve_target + fetch_metadata at click time.
    """
    if not profile:
        return False
    return resolve_target(profile) is not None


def _http_get_json(url, settings=None, timeout=_HTTP_TIMEOUT_S):
    """GET + JSON parse through the proxy-aware opener (Tor-safe for .onion)."""
    opener = proxy_opener.proxy_opener(settings)
    req = urllib.request.Request(url, headers={"User-Agent": "gnostr/1.0"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise ZapHTTPError(f"LNURL server error {e.code}") from e
    except Exception as e:
        raise ZapHTTPError(f"Could not reach LNURL server: {e}") from e
    try:
        return json.loads(body)
    except ValueError:
        raise ZapHTTPError("LNURL server returned invalid JSON") from None


def fetch_metadata(lnurl, settings=None, timeout=_HTTP_TIMEOUT_S, force=False):
    """Fetch + gate the LUD-06 payRequest metadata (§1.2).

    v1 requires allowsNostr==true AND a 32-byte-hex nostrPubkey — without
    both, no zap receipt can be guaranteed. A mandatory payerData field
    aborts: v1 sends no payer data, and silently leaking name/pubkey is
    worse than refusing. Returns the fields the flow needs, cached ~1h.
    """
    url = lnurl_to_url(lnurl)
    now = time.time()
    if not force:
        hit = _metadata_cache.get(lnurl)
        if hit and hit[0] > now:
            return hit[1]

    body = _http_get_json(url, settings=settings, timeout=timeout)
    if not isinstance(body, dict) or body.get("tag") != "payRequest":
        raise NotZapableError("recipient doesn't support zaps")
    if body.get("allowsNostr") is not True:
        raise NotZapableError("recipient doesn't support zaps")
    nostr_pubkey = body.get("nostrPubkey") or ""
    if not _is_hex32(nostr_pubkey):
        raise NotZapableError("recipient doesn't support zaps")
    payer_data = body.get("payerData") or {}
    if isinstance(payer_data, dict) and any(
        isinstance(v, dict) and v.get("mandatory") for v in payer_data.values()
    ):
        raise ProfileSharingRequired("recipient requires profile sharing")

    try:
        min_sendable = int(body.get("minSendable", 1))
        max_sendable = int(body.get("maxSendable", 0))
        comment_allowed = int(body.get("commentAllowed", 0) or 0)
    except (TypeError, ValueError):
        raise NotZapableError("recipient doesn't support zaps") from None
    callback = body.get("callback") or ""
    if not callback.startswith(("http://", "https://")):
        raise NotZapableError("recipient doesn't support zaps")

    meta = {
        "callback": callback,
        "nostrPubkey": nostr_pubkey.lower(),
        "minSendable": min_sendable,
        "maxSendable": max_sendable,
        "commentAllowed": max(0, comment_allowed),
        "domain": urllib.parse.urlsplit(url).hostname or "",
        "url": url,
    }
    _metadata_cache[lnurl] = (now + METADATA_TTL_S, meta)
    return meta


def clear_metadata_cache():
    """Test hook: drop cached payRequest metadata."""
    _metadata_cache.clear()
