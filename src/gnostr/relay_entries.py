"""Relay entry data model (spec docs/specs/relays-and-settings-v1.md section A).

Pure GTK-free helpers for the ``relay_entries`` config list: ordered
per-relay entries ``{"url": str, "roles": [...], "enabled": bool}``. Roles are
``read`` (REQ/feed queries), ``write`` (publishes) and ``dm`` (DM targeting);
list order is the user's preference order (display + fallback order);
``enabled: false`` parks an entry (kept, never connects).

NostrClient owns the live list; this module owns the shapes and rules so they
are unit-testable without GObject/GTK (same contract as proxy_settings.py).
"""

RELAY_ROLES = ("read", "write", "dm")
DEFAULT_ROLES = ["read", "write"]

# NIPW-P3-FIX-3 (Bug B): relays documented to demand NIP-42 AUTH for REQs
# while gnostr v1 never completes AUTH. Read-role routing pre-skips them so
# the communities glist snapshot never bounces (FLX1 round 3 logged
# 'sub_close relay=wss://aggr.nostr.land sub=glist_self result=fail
# reason=auth-required' on every visit). Write role is untouched: joins
# and publishes must still reach these relays.
KNOWN_AUTH_REQUIRED_READ = frozenset({"wss://aggr.nostr.land"})


def normalize_roles(roles):
    """Canonical-order role list (read, write, dm); unknown entries dropped,
    empty/None -> []."""
    if not roles:
        return []
    wanted = {str(r) for r in roles}
    return [r for r in RELAY_ROLES if r in wanted]


def make_entry(url, roles=None, enabled=True):
    """Build one entry; empty/invalid roles fall back to the default
    [read, write] (spec: default for new relays)."""
    return {
        "url": str(url).strip().rstrip("/"),
        "roles": normalize_roles(roles) or list(DEFAULT_ROLES),
        "enabled": bool(enabled),
    }


def normalize_entry(raw):
    """Coerce one config dict into an entry, or None when unusable."""
    if not isinstance(raw, dict):
        return None
    url = str(raw.get("url") or "").strip().rstrip("/")
    if not url:
        return None
    roles = normalize_roles(raw.get("roles")) or list(DEFAULT_ROLES)
    return {"url": url, "roles": roles, "enabled": bool(raw.get("enabled", True))}


def normalize_entries(raw_list):
    """Normalize a config ``relay_entries`` list; dedupe by url (first wins)."""
    out = []
    seen = set()
    for raw in raw_list or []:
        entry = normalize_entry(raw)
        if entry and entry["url"] not in seen:
            seen.add(entry["url"])
            out.append(entry)
    return out


def entries_from_legacy(relays):
    """Migration from the flat legacy ``relays`` list: sorted for
    determinism, default roles, all enabled (spec section A)."""
    urls = sorted({str(u).strip().rstrip("/") for u in relays or [] if str(u).strip()})
    return [make_entry(u) for u in urls]


def entry_for(entries, url):
    """The entry for `url`, or None."""
    url = str(url).rstrip("/")
    for e in entries or []:
        if e.get("url") == url:
            return e
    return None


def enabled_urls(entries):
    """Urls of enabled entries, in preference order."""
    return [e["url"] for e in entries or [] if e.get("enabled", True)]


def urls_for_role(entries, role):
    """Enabled urls carrying `role`, in preference order (order = priority
    for fallback and display)."""
    return [
        e["url"]
        for e in entries or []
        if e.get("enabled", True) and role in (e.get("roles") or [])
    ]


def relay10002_tags(entries):
    """NIP-65 kind-10002 r-tags from entry roles: plain ["r", url] when a
    relay serves both read and write, ["r", url, "read"] / ["r", url,
    "write"] otherwise. A `dm` role counts as write (DMs are published
    there). Parked (enabled=false) entries are not advertised."""
    tags = []
    for e in entries or []:
        if not e.get("enabled", True):
            continue
        roles = set(e.get("roles") or [])
        has_read = "read" in roles
        has_write = ("write" in roles) or ("dm" in roles)
        if has_read and has_write:
            tags.append(["r", e["url"]])
        elif has_read:
            tags.append(["r", e["url"], "read"])
        else:
            tags.append(["r", e["url"], "write"])
    return tags


def merge_imported(entries, urls):
    """Import-from-profile merge: new ws:// relays append with default roles;
    existing entries keep their roles and order (spec section A). Returns the
    urls that were added."""
    existing = {e["url"] for e in entries or []}
    added = []
    for u in urls or []:
        u = str(u).strip().rstrip("/")
        if u.startswith("ws") and u not in existing:
            entries.append(make_entry(u))
            existing.add(u)
            added.append(u)
    return added
