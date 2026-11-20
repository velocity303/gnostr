"""NIP-29 relay-based groups — GTK-free logic layer (spec nipw-communities-v1).

Pure parsing/validation/state-machine code for Communities: the kind-10009
group registry, NIP-19 naddr decode, relay-signed 39000/39001 metadata with
the NIP-11 self-key signature gate, membership resolution (latest 9000/9001),
the preview-gated join state machine, and the REQ/event templates the UI and
service layers use. No GTK, no network I/O here (fetch_nip11 takes an injected
opener so tests never touch the socket).

Hard rules from the spec (nipw-communities-v1 §2/§3):
  * a 39000 whose signer != the relay's NIP-11 `self` pubkey is NEVER shown
    (preview gate — no unverified metadata, no 9021 behind it);
  * `private` groups hard-abort in v1 (no NIP-42 AUTH in gnostr yet);
  * group traffic flows only on the group's own relay hint, never general
    relays — enforced by callers using client.subscribe_on(url, ...).

Relay-hint hygiene reuses trust_shield.normalize_relay_hint (trust-shield-v1
§1.2) — same validator, one source of truth.
"""

import json
import re
import time

from gnostr.zaps.request import verify_event_sig as _verify_event_sig

from .nostr_utils import bech32_decode, bech32_encode, convertbits
from .trust_shield import normalize_relay_hint

# --- kinds (NIP-29 + NIP-51) ---------------------------------------------

KIND_GROUPS_LIST = 10009
KIND_GROUP_METADATA = 39000
KIND_GROUP_ADMINS = 39001
KIND_GROUP_MEMBERS = 39002
KIND_GROUP_REMOVE_USER = 9000
KIND_GROUP_ADD_USER = 9001
KIND_GROUP_JOIN_REQUEST = 9021
KIND_GROUP_LEAVE = 9022

# kinds the client routes to the group-event-received signal (plus timeline
# kinds 1/7/9 when they carry an h tag — decided in client._handle_event).
GROUP_RELAY_KINDS = frozenset(
    {
        KIND_GROUPS_LIST,
        KIND_GROUP_METADATA,
        KIND_GROUP_ADMINS,
        KIND_GROUP_MEMBERS,
    }
    | set(range(9000, 9023))
)

# NIP-29 flag tags on 39000 (displayed verbatim; `private` gates v1).
GROUP_FLAGS = ("public", "open", "private", "closed", "restricted", "hidden", "livekit")
OPEN_FLAGS = ("public", "open")

# v1 timeline kinds (spec §3): notes + reactions; 6/30023 deferred.
GROUP_TIMELINE_KINDS = (1, 7, 9)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def validate_relay_url(s):
    """Same hygiene as trust-shield §1.2 (single source: normalize_relay_hint).
    Returns the normalized ws(s):// URL or None."""
    return normalize_relay_hint(s)


# --- kind-10009 registry ---------------------------------------------------


class GroupRef:
    """One entry of the user's kind-10009 groups list. `valid` is False when
    the relay hint failed hygiene — the row is shown greyed, never connected."""

    __slots__ = ("id", "relay", "name", "valid")

    def __init__(self, id, relay, name=None, valid=None):
        self.id = id
        self.relay = relay
        self.name = name or None
        if valid is None:
            valid = validate_relay_url(relay) is not None
        self.valid = valid

    def key(self):
        return (self.relay, self.id)

    def display_name(self):
        if self.name:
            return self.name
        host = self.relay.split("://", 1)[-1].split("/", 1)[0] if self.relay else "?"
        return f"{self.id}@{host}"

    def __repr__(self):
        return f"GroupRef({self.id!r}, {self.relay!r}, {self.name!r})"


def parse_10009(event):
    """kind-10009 -> list[GroupRef]. Every ["group", id, relay?, name?] tag
    becomes a ref (invalid relay hints keep the row, marked invalid). Empty
    events (tags:[]) — common in the wild — parse to []. ["r", relay] tags are
    informational and ignored for routing (spec §1.3)."""
    refs = []
    for tag in event.get("tags") or []:
        if not tag or tag[0] != "group" or len(tag) < 2:
            continue
        gid = tag[1]
        if not isinstance(gid, str) or not gid:
            continue
        relay = tag[2] if len(tag) > 2 and isinstance(tag[2], str) else ""
        name = tag[3] if len(tag) > 3 and isinstance(tag[3], str) and tag[3] else None
        refs.append(GroupRef(gid, relay, name))
    return refs


def group_tags_from_refs(refs):
    """Build the ["group", ...] tags for a replacement 10009 write (spec §1.5:
    full replacement, replaceable semantics)."""
    tags = []
    for ref in refs:
        tag = ["group", ref.id]
        if ref.relay:
            tag.append(ref.relay)
            if ref.name:
                tag.append(ref.name)
        tags.append(tag)
    return tags


# --- NIP-19 naddr ----------------------------------------------------------


def decode_naddr(code):
    """Decode an naddr1… code (NIP-19 TLV). Returns
    {identifier, relay, pubkey, kind, invite} or None. A '?invite=<code>'
    suffix is stripped before decode (spec §2). The identifier (TLV type 0)
    is SPECIAL/non-hex: raw bytes are the group id string verbatim."""
    if not isinstance(code, str):
        return None
    code = code.strip()
    invite = None
    if "?invite=" in code:
        code, invite = code.split("?invite=", 1)
        invite = invite.split("&", 1)[0] or None
    hrp, data = bech32_decode(code)
    if hrp != "naddr" or data is None:
        return None
    try:
        five = convertbits(data, 5, 8, False)
        if five is None:
            return None
        raw = bytes(five)
    except Exception:
        return None
    if raw is None:
        return None
    out = {"identifier": None, "relay": None, "pubkey": None, "kind": None,
           "invite": invite}
    i = 0
    n = len(raw)
    while i + 1 < n:
        t, ln = raw[i], raw[i + 1]
        v = raw[i + 2 : i + 2 + ln]
        if len(v) != ln:
            return None
        i += 2 + ln
        if t == 0:
            out["identifier"] = v.decode("utf-8", "replace")
        elif t == 1:
            out["relay"] = v.decode("utf-8", "replace")
        elif t == 2 and ln == 32:
            out["pubkey"] = v.hex()
        elif t == 3 and ln == 4:
            out["kind"] = int.from_bytes(v, "big")
    if not out["identifier"]:
        return None
    return out


def encode_naddr(identifier, relay, pubkey, kind):
    """Inverse of decode_naddr (TLV: 0=special, 1=relay, 2=pubkey, 3=kind BE4)."""
    raw = b""
    ident = identifier.encode("utf-8")
    raw += bytes([0, len(ident)]) + ident
    rel = relay.encode("utf-8")
    raw += bytes([1, len(rel)]) + rel
    raw += bytes([2, 32]) + bytes.fromhex(pubkey)
    raw += bytes([3, 4]) + kind.to_bytes(4, "big")
    return bech32_encode("naddr", convertbits(raw, 8, 5, True))


def parse_relay_group_input(text):
    """Fallback join input: '<wss://relay> <group-id>' (spec §2). Returns
    (relay, group_id) or None."""
    if not isinstance(text, str):
        return None
    parts = text.split()
    if len(parts) != 2:
        return None
    relay = validate_relay_url(parts[0])
    if relay and parts[1]:
        return relay, parts[1]
    return None


# --- event signature gate ----------------------------------------------------

# BIP-340 verify + id recompute already exist in the zaps layer (used by the
# 9735 receipt gate) — one verifier, reused here (Ponytail).


def verify_event_sig(event):
    """id recompute + schnorr sig check. A lying relay cannot forge a 39000
    by copying pubkey fields: the sig must match the id, the id the content."""
    return _verify_event_sig(event)


# --- 39000/39001 metadata ---------------------------------------------------


class GroupMeta:
    """Verified group metadata (preview card data). `member_count` is a
    39002 subset count when available — never authoritative (spec §0c)."""

    __slots__ = (
        "relay", "group_id", "name", "picture", "banner", "about", "flags",
        "supported_kinds", "admins", "member_count", "fetched_at",
    )

    def __init__(self, relay, group_id, **kw):
        self.relay = relay
        self.group_id = group_id
        self.name = kw.get("name")
        self.picture = kw.get("picture")
        self.banner = kw.get("banner")
        self.about = kw.get("about")
        self.flags = kw.get("flags") or []
        self.supported_kinds = kw.get("supported_kinds")
        self.admins = kw.get("admins") or []
        self.member_count = kw.get("member_count")
        self.fetched_at = kw.get("fetched_at") or int(time.time())

    def has_flag(self, flag):
        return flag in self.flags

    def to_dict(self):
        return {s: getattr(self, s) for s in self.__slots__}


def _tag_value(tags, name):
    for tag in tags or []:
        if len(tag) >= 2 and tag[0] == name:
            return tag[1]
    return None


def parse_group_metadata(ev, relay_self_pk, relay=None):
    """39000 -> GroupMeta, or None when the event must be dropped:
    kind != 39000, id/sig invalid, signer != the relay's NIP-11 self pubkey
    (the preview gate), or no d tag. `relay` is the URL the metadata belongs
    to (the connection it arrived on); falls back to the event's own hint."""
    if not ev or ev.get("kind") != KIND_GROUP_METADATA:
        return None
    if not relay_self_pk or not _HEX64.match(relay_self_pk):
        return None
    if not verify_event_sig(ev):
        return None
    if ev.get("pubkey") != relay_self_pk:
        return None
    tags = ev.get("tags") or []
    gid = _tag_value(tags, "d")
    if not gid:
        return None
    if not relay:
        relay = _tag_value(tags, "relay")
    flags = [t[0] for t in tags if t and t[0] in GROUP_FLAGS]
    supported = None
    for tag in tags:
        if tag and tag[0] == "supported_kinds":
            try:
                supported = [int(x) for x in tag[1:]]
            except ValueError:
                supported = []
            break
    return GroupMeta(
        relay or "",
        gid,
        name=_tag_value(tags, "name"),
        picture=_tag_value(tags, "picture"),
        banner=_tag_value(tags, "banner"),
        about=_tag_value(tags, "about"),
        flags=flags,
        supported_kinds=supported,
    )


def parse_group_admins(ev, relay_self_pk):
    """39001 -> list[(pubkey, role)] from p-tags (role verbatim — relay
    vocabulary is arbitrary, e.g. 'king' on hzrd149; never gate UI on it).
    Same relay-signature gate as 39000; returns None when dropped."""
    if not ev or ev.get("kind") != KIND_GROUP_ADMINS:
        return None
    if not relay_self_pk or not verify_event_sig(ev):
        return None
    if ev.get("pubkey") != relay_self_pk:
        return None
    admins = []
    for tag in ev.get("tags") or []:
        if len(tag) >= 2 and tag[0] == "p" and _HEX64.match(tag[1] or ""):
            role = tag[2] if len(tag) > 2 and tag[2] else ""
            admins.append((tag[1], role))
    return admins


def group_of(ev):
    """(group_id, kind) for group traffic: 39000-39005 address themselves via
    the d tag, timeline/management events via the h tag. (None, kind) when the
    event is not group traffic."""
    kind = ev.get("kind")
    if kind is None:
        return None, None
    tags = ev.get("tags") or []
    if 39000 <= kind <= 39005:
        for t in tags:
            if len(t) >= 2 and t[0] == "d":
                return t[1], kind
        return None, kind
    for t in tags:
        if len(t) >= 2 and t[0] == "h":
            return t[1], kind
    return None, kind


def is_group_event(ev):
    """True when the event belongs to a NIP-29 group (h tag or 39000-39005
    with d tag) — the client routes these to group pages, never the feed."""
    gid, _ = group_of(ev)
    return gid is not None


def count_members(ev):
    """39002 -> number of p-tags (subset count, never authoritative)."""
    if not ev or ev.get("kind") != KIND_GROUP_MEMBERS:
        return None
    return sum(
        1
        for t in ev.get("tags") or []
        if len(t) >= 2 and t[0] == "p" and _HEX64.match(t[1] or "")
    )


# --- membership --------------------------------------------------------------


def latest_membership(events, self_pk):
    """Latest 9000/9001 with a p-tag == self_pk decides (NIP-29 event
    definitions: 9000 = Group Remove User, 9001 = Group Add User):
    latest 9001 -> 'member', latest 9000 -> 'removed', nothing -> 'unknown'.
    NOTE: spec §2.4 lists this mapping inverted (9000->member); NIP-29 and
    khatru's groups.hzrd149.com behavior (9021 join -> relay emits 9001)
    are authoritative — flagged to miku in the PR.
    """
    best_ts = None
    best_kind = None
    for ev in events or []:
        kind = ev.get("kind")
        if kind not in (KIND_GROUP_REMOVE_USER, KIND_GROUP_ADD_USER):
            continue
        tags = ev.get("tags") or []
        if not any(
            len(t) >= 2 and t[0] == "p" and t[1] == self_pk for t in tags
        ):
            continue
        ts = ev.get("created_at") or 0
        if best_ts is None or ts > best_ts:
            best_ts, best_kind = ts, kind
    if best_kind == KIND_GROUP_ADD_USER:  # 9001 = add user
        return "member"
    if best_kind == KIND_GROUP_REMOVE_USER:  # 9000 = remove user
        return "removed"
    return "unknown"


# --- REQ / event templates (spec §3 table + event_templates vectors) ---------


def metadata_filter(group_id):
    return {"kinds": [KIND_GROUP_METADATA, KIND_GROUP_ADMINS], "#d": [group_id]}


def timeline_filter(group_id, since=None):
    f = {"kinds": list(GROUP_TIMELINE_KINDS), "#h": [group_id], "limit": 100}
    if since:
        f["since"] = int(since)
    return f


def membership_filter(group_id, self_pk):
    return {
        "kinds": [KIND_GROUP_REMOVE_USER, KIND_GROUP_ADD_USER],
        "#h": [group_id],
        "#p": [self_pk],
        "limit": 20,
    }


def members_filter(group_id):
    """39002 members set — the authoritative membership source on khatru
    relays (verified live on groups.hzrd149.com 2026-11-20: open joins do
    NOT produce per-user 9000/9001 audit events; the 39002 set updates)."""
    return {"kinds": [KIND_GROUP_MEMBERS], "#d": [group_id]}


def is_member_in_members_event(ev, self_pk):
    """True/False when ev is a 39002 whose p-tags answer membership for
    self_pk; None when ev is not a 39002 (no answer)."""
    if not ev or ev.get("kind") != KIND_GROUP_MEMBERS:
        return None
    return any(
        len(t) >= 2 and t[0] == "p" and t[1] == self_pk
        for t in ev.get("tags") or []
    )


def groups_list_filter(self_pk):
    return {"kinds": [KIND_GROUPS_LIST], "authors": [self_pk], "limit": 1}


def sub_id_for(kind_prefix, group_id):
    """gtl_/gmeta_/gmem_ + first 8 chars of the group id (spec §3 table)."""
    return f"{kind_prefix}_{group_id[:8]}"


def build_join_event(pubkey, group_id, invite=None, reason="", created_at=None):
    """9021 join request (spec §2.3). The code tag ONLY when the naddr
    carried ?invite= (spec vector: 'optional ["code",…] only when invite')."""
    tags = [["h", group_id]]
    if invite:
        tags.append(["code", invite])
    return {
        "pubkey": pubkey,
        "created_at": created_at if created_at is not None else int(time.time()),
        "kind": KIND_GROUP_JOIN_REQUEST,
        "tags": tags,
        "content": reason or "",
    }


def build_leave_event(pubkey, group_id, created_at=None):
    """9022 leave — identical to 9021 minus the code tag."""
    return {
        "pubkey": pubkey,
        "created_at": created_at if created_at is not None else int(time.time()),
        "kind": KIND_GROUP_LEAVE,
        "tags": [["h", group_id]],
        "content": "",
    }


def previous_tags(events, self_pk=None, relay=None, max_refs=3, window=50):
    """NIP-29 §Timeline references, best-effort: first-8-hex ids of the <=3
    most recent events BY OTHERS within the last `window` seen events (spec
    §3 posting rule). hzrd149 enforces nothing, so absence never blocks
    (spec §0 probe: 0/30 carry previous)."""
    out = []
    for ev in (events or [])[:window]:
        eid = ev.get("id") or ""
        if not _HEX64.match(eid):
            continue
        if self_pk and ev.get("pubkey") == self_pk:
            continue
        if relay and ev.get("_relay") and ev["_relay"] != relay:
            continue
        out.append(eid[:8])
        if len(out) >= max_refs:
            break
    return out


def build_group_post_event(pubkey, group_id, text, previous=None, created_at=None):
    """Group post: kind 1 + h tag + best-effort previous tags (P3-IMPL-B
    consumer; builder lives here so the templates stay in one place)."""
    tags = [["h", group_id]]
    for pid in previous or []:
        tags.append(["previous", pid])
    return {
        "pubkey": pubkey,
        "created_at": created_at if created_at is not None else int(time.time()),
        "kind": 1,
        "tags": tags,
        "content": text,
    }


# --- NIP-11 ------------------------------------------------------------------


def nip11_url(relay_url):
    """ws(s)://host -> http(s)://host (NIP-11 fetch target)."""
    if not relay_url:
        return None
    scheme = {"ws": "http", "wss": "https"}.get(relay_url.split(":", 1)[0])
    if not scheme:
        return None
    return scheme + "://" + relay_url.split("://", 1)[1]


def fetch_nip11(relay_url, opener=None, timeout=10):
    """GET the NIP-11 doc (Accept: application/nostr+json). `opener` is an
    injected urllib opener (proxy_opener in production; None = direct).
    Returns the parsed dict or None on any failure — callers treat None as
    'relay does not support groups' only after checking the doc lacks 29."""
    import urllib.request

    url = nip11_url(relay_url)
    if not url:
        return None
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/nostr+json"}
        )
        ctx = opener or urllib.request.build_opener()
        with ctx.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def nip11_self_pk(doc):
    """NIP-11 'pubkey' field = relay master key (signer of 39000/39001)."""
    if not isinstance(doc, dict):
        return None
    pk = doc.get("pubkey")
    return pk if isinstance(pk, str) and _HEX64.match(pk) else None


def nip11_supports_groups(doc):
    return isinstance(doc, dict) and 29 in (doc.get("supported_nips") or [])


# --- join state machine (spec §2) ---------------------------------------------

JOIN_STATES = (
    "idle",
    "resolving",
    "preview",
    "join_sent",
    "member",
    "pending",
    "rejected",
    "already_member",
    "aborted",
)


class GroupJoinMachine:
    """Preview-gated join flow, pure transitions (I/O lives in the service).

    idle -> resolving -> preview -> join_sent -> member | pending |
    rejected | already_member. Any pre-join failure -> aborted with a reason.
    The gate: on_join() only succeeds from 'preview' AND only when the
    metadata passed on_metadata() was relay-verified and not private — so no
    9021 ever leaves the device for unverified or private groups (v1 rule).
    """

    def __init__(self, group_id, relay, self_pk, invite=None):
        self.group_id = group_id
        self.relay = relay
        self.self_pk = self_pk
        self.invite = invite
        self.state = "idle"
        self.abort_reason = None
        self.rejection_reason = None
        self.meta = None
        self.join_event_id = None

    # -- transitions -------------------------------------------------------

    def begin(self):
        if self.state != "idle":
            return self.state
        self.state = "resolving"
        return self.state

    def on_nip11(self, doc):
        if self.state != "resolving":
            return self.state
        if not nip11_supports_groups(doc):
            return self._abort("relay does not support groups")
        return self.state

    def on_metadata(self, meta):
        """meta: verified GroupMeta (already signer-gated by the caller) or
        None when the relay returned nothing / verification failed."""
        if self.state != "resolving":
            return self.state
        if meta is None:
            return self._abort("group not found on relay")
        if meta.has_flag("private"):
            return self._abort(
                "private groups need relay login (NIP-42) — not supported yet"
            )
        self.meta = meta
        self.state = "preview"
        return self.state

    def closed_without_invite(self):
        """UI hint: closed groups ignore join requests unless an invite code
        was parsed (Join button disabled)."""
        return bool(self.meta) and self.meta.has_flag("closed") and not self.invite

    def on_join(self, join_event_id):
        """Caller has signed + published the 9021 to the group relay ONLY."""
        if self.state != "preview":
            return self.state
        if self.closed_without_invite():
            return self.state
        self.join_event_id = join_event_id
        self.state = "join_sent"
        return self.state

    def on_join_ok(self, accepted, message=""):
        """OK ack for the 9021. NIP-29 quirks: 'duplicate:' -> already
        member; any other false carries the relay's reason verbatim."""
        if self.state != "join_sent":
            return self.state
        if accepted:
            return self.state  # membership probe settles member/pending
        msg = (message or "").lower()
        if msg.startswith("duplicate"):
            self.state = "already_member"
        else:
            self.rejection_reason = message or "rejected"
            self.state = "rejected"
        return self.state

    def on_membership(self, state):
        """state from latest_membership()/39002 probe: member | removed |
        unknown. Membership is authoritative: a member reads as member from
        any live state (except aborted); 'removed' after a join attempt is a
        rejection, 'removed' while joined drops back to preview."""
        if self.state in ("idle", "aborted"):
            return self.state
        if state == "member":
            self.state = "member"
        elif state == "removed":
            if self.state in ("join_sent", "pending"):
                self.state = "rejected"
                self.rejection_reason = "you were removed from this group"
            elif self.state == "member":
                self.state = "preview"
        elif self.state in ("join_sent", "pending"):
            self.state = "pending"
        return self.state

    def _abort(self, reason):
        self.abort_reason = reason
        self.state = "aborted"
        return self.state
