"""NWC connection-string parsing/validation (spec zaps-payments-v1 §2.1).

`nostr+walletconnect://<wallet-pubkey-hex>?relay=<wss-url>&secret=<hex32>[&lud16=...]`

The URI is a SPENDING credential (§3.2): parsing is strict — a missing or
malformed component is a hard error, never a silent default. Unknown params
are tolerated (forward-compat) except `secret`/`relay` duplicates, which
are rejected as ambiguous.
"""

import re
from urllib.parse import unquote, urlparse

HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_SCHEME = "nostr+walletconnect"


class NWCUriError(ValueError):
    """The connection string is not a usable NWC URI."""


def parse_nwc_uri(uri):
    """-> dict(wallet_pubkey, relay, secret, lud16|None).

    Raises NWCUriError on anything that would make the connection unsafe or
    ambiguous: wrong scheme, missing/duplicate relay or secret, non-hex keys,
    non-wss relay (wss only: the NWC relay carries a spending credential).
    """
    if not isinstance(uri, str) or not uri.strip():
        raise NWCUriError("empty connection string")
    try:
        parsed = urlparse(uri.strip())
    except ValueError as e:
        raise NWCUriError(f"unparseable URI: {e}") from e

    if parsed.scheme != _SCHEME:
        raise NWCUriError(f"scheme must be '{_SCHEME}://'")

    # wallet pubkey: userinfo (walletconnect://<pk>?...) or the host part
    pk = parsed.username or parsed.hostname or ""
    pk = pk.replace(":", "")  # some encoders leave a trailing colon
    if not HEX64.match(pk):
        raise NWCUriError("wallet pubkey must be 64-char hex")
    wallet_pubkey = pk.lower()

    params = _parse_query(parsed.query)

    relay = params.pop("relay", None)
    if relay is None:
        raise NWCUriError("missing relay param")
    if isinstance(relay, list):
        raise NWCUriError("duplicate relay param")
    relay = unquote(relay)
    if not relay.startswith("wss://"):
        # Loopback ws:// is permitted (local test relays only — the spending
        # credential never leaves the machine). Everything else: wss only.
        if not re.match(r"^ws://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", relay):
            raise NWCUriError("relay must be a wss:// URL")

    secret = params.pop("secret", None)
    if secret is None:
        raise NWCUriError("missing secret param")
    if isinstance(secret, list):
        raise NWCUriError("duplicate secret param")
    secret = unquote(secret)
    if not HEX64.match(secret):
        raise NWCUriError("secret must be 32-byte hex (64 chars)")
    secret = secret.lower()

    lud16 = params.pop("lud16", None)
    if isinstance(lud16, list):
        lud16 = lud16[0]
    lud16 = unquote(lud16) if lud16 else None

    # Remaining params (e.g. future NIP-47 additions) are ignored, never
    # fatal — but a second `secret`/`relay` already raised above.
    return {
        "wallet_pubkey": wallet_pubkey,
        "relay": relay,
        "secret": secret,
        "lud16": lud16,
    }


def _parse_query(query):
    """key -> value, or list when a key repeats (duplicate detection)."""
    params = {}
    for part in query.split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        key = key.lower()
        if key in params:
            prev = params[key]
            params[key] = prev + [value] if isinstance(prev, list) else [prev, value]
        else:
            params[key] = value
    return params
