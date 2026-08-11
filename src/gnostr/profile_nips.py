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
# NIP-39: identity provider names only a-z0-9 and ._/- (no colon)
_PLATFORM_RE = re.compile(r"^[a-z0-9._/-]+$")


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
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return False
    names = data.get("names") if isinstance(data, dict) else None
    if not isinstance(names, dict):
        return False
    return str(names.get(local_part, "")).lower() == str(pubkey).lower()


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
