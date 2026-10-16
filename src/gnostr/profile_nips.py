"""Profile NIP support — GTK-free protocol helpers for the profile page.

Pure functions for NIP-05 identity verification, NIP-39 external identities,
NIP-58 profile badges, and LUD-16 lightning address resolution. Uses only the
stdlib (urllib, json) so it stays unit-testable without GTK, matching the
nostr_utils.py convention.

Specs (nostr-protocol/nips master):
- NIP-05:  https://<domain>/.well-known/nostr.json?name=<local-part>
- NIP-39:  kind 10011 with ["i", "platform:identity", "proof"] tags
- NIP-58:  kind 10008 profile badges (ordered a/e tag pairs)
- LUD-16:  lightning address user@domain -> https://<domain>/.well-known/lnurlp/<user>
"""

import json
import re
import urllib.request

# NIP-05: local-part MUST only use a-z0-9-_. (RFC 5322 atext subset)
_LOCAL_PART_RE = re.compile(r"^[a-z0-9\-_.]+$")
# NIP-05: names map to 64-char hex pubkeys — anything else is invalid
_HEX_PUBKEY_RE = re.compile(r"^[0-9a-f]{64}$")
# NIP-39: identity provider names only a-z0-9 and ._/- (no colon)
_PLATFORM_RE = re.compile(r"^[a-z0-9._/-]+$")

# NIP-05 requires https in production; module attribute so the local
# http.server test fixture (tests/test_dm_protocol_hardening.py) can point
# lookups at 127.0.0.1 without a certificate.
_NIP05_SCHEME = "https"


def _nip05_url(domain: str, local_part: str) -> str:
    return f"{_NIP05_SCHEME}://{domain}/.well-known/nostr.json?name={local_part}"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """NIP-05 normative: the client MUST NOT follow redirects when fetching
    nostr.json (a redirect could move the identity onto a host the address
    does not name). Returning None from redirect_request makes urllib raise
    HTTPError on any 3xx instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def verify_nip05(pubkey: str, nip05: str) -> bool:
    """Verify a NIP-05 identifier maps to the given pubkey.

    Splits `local-part@domain`, GETs the well-known nostr.json, and compares
    the returned hex pubkey (lowercase) against `pubkey`. Returns False on any
    malformed identifier, network error, or mismatch — never raises.
    """
    if not nip05 or "@" not in nip05:
        return False
    local_part, _, domain = nip05.rpartition("@")
    if not local_part or not domain or not _LOCAL_PART_RE.match(local_part):
        return False
    url = f"https://{domain}/.well-known/nostr.json?name={local_part}"
    try:
        # Explicit UA: many CDNs (Cloudflare included) 403 the default
        # Python-urllib agent, which would silently break verification.
        req = urllib.request.Request(url, headers={"User-Agent": "gnostr/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return False
    names = data.get("names") if isinstance(data, dict) else None
    if not isinstance(names, dict):
        return False
    return str(names.get(local_part, "")).lower() == str(pubkey).lower()


def lookup_nip05(identifier: str):
    """P2 NIP-05 discovery (spec Phase P): address -> pubkey.

    `identifier` is `local-part@domain`. GETs
    https://<domain>/.well-known/nostr.json?name=<local-part> and reads
    names[<local-part>]. Returns (pubkey_hex, relay_hints) on confirmation —
    relay_hints is the optional relays[<local-part>] list (NIP-05 relay hints,
    [] when absent) — or None on any failure.

    NIP-05 normative rules enforced here: redirects are refused (the
    no-redirect opener makes any 3xx an error — following one could move the
    identity onto a host the address does not name); names values must be
    64-char hex; the local-part charset (a-z0-9-_.) is validated client-side
    before any request. Never raises; the UI calls this off the main thread
    (same worker pattern as verify_nip05).
    """
    if not identifier or "@" not in identifier:
        return None
    local_part, _, domain = identifier.rpartition("@")
    if not local_part or not domain or not _LOCAL_PART_RE.match(local_part):
        return None
    url = _nip05_url(domain, local_part)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "gnostr/1.0"})
        # No-redirect opener: NIP-05 forbids following redirects here.
        with _NO_REDIRECT_OPENER.open(req, timeout=5) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    names = data.get("names")
    if not isinstance(names, dict):
        return None
    pubkey = names.get(local_part)
    if not isinstance(pubkey, str) or not _HEX_PUBKEY_RE.match(pubkey.lower()):
        return None
    hints = []
    relays = data.get("relays")
    if isinstance(relays, dict):
        raw = relays.get(local_part)
        if isinstance(raw, list):
            hints = [u for u in raw if isinstance(u, str) and u.startswith("wss://")]
    return (pubkey.lower(), hints)


# --- NIP-39 external identities ---

# platform -> profile URL template. identity is the raw identity value.
_PLATFORM_URLS = {
    "github": "https://github.com/{identity}",
    "twitter": "https://twitter.com/{identity}",
    "mastodon": "https://{identity}/{proof}",
    "telegram": "https://t.me/{proof}",
}


def parse_external_identities(event: dict) -> list:
    """Parse NIP-39 external identities from a kind-10011 event.

    Returns a list of dicts: {platform, identity, proof, url}. An `i` tag MUST
    have two params (platform:identity + proof) per NIP-39; shorter tags are
    ignored. Unknown platforms still yield a dict (url may be None).
    """
    if event.get("kind") != 10011:
        return []
    out = []
    for tag in event.get("tags", []):
        if len(tag) < 3 or tag[0] != "i":
            continue
        platform_identity = tag[1]
        proof = tag[2]
        if ":" not in platform_identity:
            continue
        platform, _, identity = platform_identity.partition(":")
        if not _PLATFORM_RE.match(platform) or not identity:
            continue
        url = None
        tpl = _PLATFORM_URLS.get(platform)
        if tpl:
            url = tpl.format(identity=identity, proof=proof)
        out.append(
            {"platform": platform, "identity": identity, "proof": proof, "url": url}
        )
    return out


# --- NIP-58 profile badges ---


def parse_profile_badges(event: dict) -> list:
    """Parse NIP-58 profile badges from a kind-10008 event.

    Returns a list of (badge_definition_a_tag, badge_award_e_tag) pairs from
    ordered consecutive a/e tags. NIP-58: clients SHOULD ignore an `a` without
    a corresponding `e` and viceversa.
    """
    if event.get("kind") != 10008:
        return []
    tags = event.get("tags", [])
    out = []
    i = 0
    while i < len(tags):
        t = tags[i]
        if t and t[0] == "a" and len(t) > 1:
            a_val = t[1]
            # look ahead for the paired e tag
            if i + 1 < len(tags) and tags[i + 1] and tags[i + 1][0] == "e":
                out.append((a_val, tags[i + 1][1]))
                i += 2
                continue
        i += 1
    return out


# --- LUD-16 lightning address ---


def resolve_lnurl_endpoint(lud16: str):
    """Resolve a lightning address (user@domain) to its lnurl-pay endpoint.

    Returns the well-known lnurlp URL, or None if the address is malformed.
    """
    if not lud16 or "@" not in lud16:
        return None
    user, _, domain = lud16.rpartition("@")
    if not user or not domain:
        return None
    return f"https://{domain}/.well-known/lnurlp/{user}"
