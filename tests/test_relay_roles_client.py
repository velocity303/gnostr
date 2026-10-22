"""RELSET-2: relay roles data model wired into NostrClient (spec
relays-and-settings-v1 §A, card RELSET-2).

Tier-1: the real NostrClient methods are ast-extracted from client.py and
bound to a lightweight FakeClient (house pattern — conftest mocks gi, so the
imported class is unusable). Covers the acceptance criteria:

- legacy migration (flat relays list -> entries, persisted immediately)
- relay_urls stays a derived property (enabled entries only)
- role routing: publishes -> write relays, feed REQs -> read relays,
  DMs -> dm relays, else peer-10050, else write relays
- kind-10002 gains NIP-65 read/write markers per entry roles
- enabled=false parking (entry kept, connection closed, never routed)
- reorder + role mutations persist to config
- import-from-profile merge keeps roles and order
- relay activity log lines carry the role tag

GLib stub must be installed AFTER exec(client.py) — client.py's own
`from gi.repository import GLib` rebinds the name to conftest's Mock.
"""

import ast
import collections
import json
import os
import tempfile
import types
from unittest.mock import MagicMock

import gnostr  # real package; carries the relay_entries attribute

_CLIENT_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", "gnostr", "client.py"
    )
)

READ_R = "wss://read-only.example"
WRITE_R = "wss://write-only.example"
BOTH_R = "wss://both.example"
DM_R = "wss://dm.example"
PARK_R = "wss://parked.example"
PEER = "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"


def _client_globals():
    src = open(_CLIENT_PATH, encoding="utf-8").read()
    ns = {"__name__": "gnostr.client", "__package__": "gnostr"}
    exec(compile(ast.parse(src), _CLIENT_PATH, "exec"), ns)
    ns["GLib"] = types.SimpleNamespace(
        idle_add=lambda fn, *a: fn(*a),
        timeout_add=lambda ms, fn, *a: None,
        timeout_add_seconds=lambda s, fn, *a: 1,
        source_remove=lambda t: None,
        get_user_config_dir=lambda: "/tmp/gnostr-test",
    )
    return ns


_NS = _client_globals()


def _unbound_client(method_name):
    src = open(_CLIENT_PATH, encoding="utf-8").read()
    cls = next(
        n
        for n in ast.parse(src).body
        if isinstance(n, ast.ClassDef) and n.name == "NostrClient"
    )
    fns = [
        n
        for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == method_name
    ]
    if not fns:
        raise StopIteration(method_name)
    ns = dict(_NS)
    # Exec ALL same-name defs together: the getter carries @property and the
    # setter @relay_urls.setter, so the decorators chain exactly as in the
    # class body and ns[name] ends up a real property (getter+setter).
    exec(compile(ast.Module(body=list(fns), type_ignores=[]), _CLIENT_PATH, "exec"), ns)
    return ns[method_name]


class FakeRelay:
    def __init__(self, url):
        self.url = url
        self.published = []
        self.requests = []
        self.closed = False

    def publish(self, event):
        self.published.append(event)
        return True

    def subscribe(self, sub_id, filters, snapshot=False):
        self.requests.append((sub_id, filters))

    def request_once(self, sub_id, filters):
        self.requests.append((sub_id, filters))

    def close(self):
        self.closed = True


class FakeClient:
    """Minimal self for the relay-entry methods. Unimplemented names
    auto-bind from client.py source (house FakeClient pattern)."""

    def __init__(self, entries=None, urls=None):
        self.db = MagicMock()
        self.my_pubkey = "a" * 64
        self.my_privkey = "b" * 64
        self.config_file = os.path.join(
            tempfile.mkdtemp(prefix="gnostr-relset2-"), "config.json"
        )
        self.emitted = []
        self.log = []
        self.connected = []
        self.dispatched = []
        self.tracked = []
        self.dm_lookups = []
        self.tor_state = _NS["TorState"].DISABLED
        self.relay_log = collections.deque(maxlen=200)
        self.pending_publishes = {}
        self.active_relays = {}
        self.relay_entries = entries
        if urls is not None:
            self.relay_urls = set(urls)

    # -- property: getter + setter both live in client.py --------------------
    relay_urls = _unbound_client("relay_urls")

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        try:
            fn = _unbound_client(name)
        except StopIteration:
            raise AttributeError(name)
        if isinstance(fn, property):
            bound = fn.getter.__get__(self)
        else:
            bound = fn.__get__(self)
        object.__setattr__(self, name, bound)
        return bound

    # -- stubs ---------------------------------------------------------------
    def emit(self, *a):
        self.emitted.append(a)

    def _log_relay(self, line):
        self.log.append(line)

    def add_relay_connection(self, url):
        self.connected.append(url)

    def probe_tor(self):
        pass

    def _track_publish(self, event, label, **kw):
        self.tracked.append((event["id"], label))

    def _dispatch_signed(self, event, label, after=None):
        self.dispatched.append((event, label))

    def resolve_dm_relays(self, peer, cb):
        self.dm_lookups.append(peer)
        cb(getattr(self, "_peer_relays", None))


def _entries():
    return gnostr.relay_entries.normalize_entries(
        [
            {"url": READ_R, "roles": ["read"]},
            {"url": WRITE_R, "roles": ["write"]},
            {"url": BOTH_R, "roles": ["read", "write"]},
            {"url": DM_R, "roles": ["dm"]},
            {"url": PARK_R, "roles": ["read", "write"], "enabled": False},
        ]
    )


def _client_with_entries():
    c = FakeClient(entries=_entries())
    for u in (READ_R, WRITE_R, BOTH_R, DM_R):
        c.active_relays[u] = FakeRelay(u)
    return c


def _event():
    return {"id": "e" * 64, "kind": 1, "content": "hi", "tags": []}


# -- legacy migration ----------------------------------------------------------


def test_legacy_flat_list_migrates_and_persists():
    c = FakeClient()
    with open(c.config_file, "w") as f:
        json.dump({"relays": ["wss://z.example", "wss://a.example"]}, f)
    c.load_config()
    assert [e["url"] for e in c.relay_entries] == ["wss://a.example", "wss://z.example"]
    assert all(e["roles"] == ["read", "write"] for e in c.relay_entries)
    # migrated immediately: the file now carries relay_entries + legacy mirror
    with open(c.config_file) as f:
        data = json.load(f)
    assert data["relay_entries"][0]["url"] == "wss://a.example"
    assert sorted(data["relays"]) == ["wss://a.example", "wss://z.example"]


def test_entries_config_wins_over_legacy_key():
    c = FakeClient()
    with open(c.config_file, "w") as f:
        json.dump(
            {
                "relays": ["wss://stale.example"],
                "relay_entries": [{"url": "wss://fresh.example", "roles": ["read"]}],
            },
            f,
        )
    c.load_config()
    assert [e["url"] for e in c.relay_entries] == ["wss://fresh.example"]


def test_relay_urls_is_derived_from_enabled_entries():
    c = _client_with_entries()
    assert c.relay_urls == {READ_R, WRITE_R, BOTH_R, DM_R}  # PARK_R excluded
    # legacy set-style assignment keeps the entry model in sync
    c.relay_urls = {READ_R, WRITE_R, BOTH_R, DM_R, "wss://added.example"}
    assert gnostr.relay_entries.entry_for(c.relay_entries, "wss://added.example")
    assert gnostr.relay_entries.entry_for(c.relay_entries, PARK_R)  # parked survives


def test_no_entries_fallback_behaves_like_legacy():
    c = FakeClient(urls=["wss://x.example"])
    assert c.relay_urls == {"wss://x.example"}
    c.active_relays["wss://x.example"] = FakeRelay("wss://x.example")
    c.publish(_event())
    assert len(c.active_relays["wss://x.example"].published) == 1


# -- role routing ----------------------------------------------------------------


def test_publish_routes_to_write_relays_only():
    c = _client_with_entries()
    sent = c.publish(_event())
    assert sent == {WRITE_R, BOTH_R}
    assert c.active_relays[WRITE_R].published and c.active_relays[BOTH_R].published
    assert not c.active_relays[READ_R].published
    assert not c.active_relays[DM_R].published


def test_subscribe_and_request_once_route_to_read_relays_only():
    c = _client_with_entries()
    c.subscribe("s1", {"kinds": [0, 1]})
    c.request_once("s2", {"kinds": [3]})
    for url in (READ_R, BOTH_R):
        assert len(c.active_relays[url].requests) == 2
    assert not c.active_relays[WRITE_R].requests
    assert not c.active_relays[DM_R].requests


def test_parked_entry_never_routes():
    c = _client_with_entries()
    c.active_relays[PARK_R] = FakeRelay(PARK_R)
    c.publish(_event())
    c.subscribe("s", {})
    assert not c.active_relays[PARK_R].published
    assert not c.active_relays[PARK_R].requests


# -- entry API: add / remove / roles / parking / reorder ---------------------------


def test_add_relay_entry_appends_persists_and_connects():
    c = _client_with_entries()
    assert c.add_relay_entry("wss://new.example") is True
    assert c.relay_entries[-1]["url"] == "wss://new.example"
    assert c.relay_entries[-1]["roles"] == ["read", "write"]
    assert "wss://new.example" in c.connected
    assert ("relay-list-updated",) in c.emitted
    with open(c.config_file) as f:
        assert json.load(f)["relay_entries"][-1]["url"] == "wss://new.example"
    # duplicate add is a no-op
    assert c.add_relay_entry("wss://new.example") is False


def test_remove_relay_entry_deletes_and_closes():
    c = _client_with_entries()
    relay = c.active_relays[READ_R]
    assert c.remove_relay_entry(READ_R) is True
    assert gnostr.relay_entries.entry_for(c.relay_entries, READ_R) is None
    assert relay.closed is True
    assert READ_R not in c.relay_urls
    assert c.remove_relay_entry("wss://nope.example") is False


def test_set_relay_roles_normalizes_and_persists():
    c = _client_with_entries()
    assert c.set_relay_roles(BOTH_R, ["dm", "write"]) is True
    assert gnostr.relay_entries.entry_for(c.relay_entries, BOTH_R)["roles"] == [
        "write",
        "dm",
    ]
    with open(c.config_file) as f:
        data = json.load(f)
    entry = [e for e in data["relay_entries"] if e["url"] == BOTH_R][0]
    assert entry["roles"] == ["write", "dm"]


def test_parking_persists_and_closes_connection():
    c = _client_with_entries()
    both = c.active_relays[BOTH_R]
    assert c.set_relay_enabled(BOTH_R, False) is True
    assert gnostr.relay_entries.entry_for(c.relay_entries, BOTH_R)["enabled"] is False
    assert BOTH_R not in c.relay_urls
    assert both.closed is True
    with open(c.config_file) as f:
        data = json.load(f)
    entry = [e for e in data["relay_entries"] if e["url"] == BOTH_R][0]
    assert entry["enabled"] is False
    # legacy mirror drops the parked url
    assert BOTH_R not in data["relays"]
    # unpark reconnects
    assert c.set_relay_enabled(BOTH_R, True) is True
    assert BOTH_R in c.connected
    assert BOTH_R in c.relay_urls


def test_move_relay_entry_persists_order():
    c = _client_with_entries()
    assert c.move_relay_entry(BOTH_R, -1) is True
    after = [e["url"] for e in c.relay_entries]
    assert after == [READ_R, BOTH_R, WRITE_R, DM_R, PARK_R]
    with open(c.config_file) as f:
        assert [e["url"] for e in json.load(f)["relay_entries"]] == after
    # clamped moves are no-ops
    assert c.move_relay_entry(after[0], -1) is False


# -- kind-10002 NIP-65 markers -------------------------------------------------------


def test_publish_relay_list_uses_nip65_markers():
    c = _client_with_entries()
    c.publish_relay_list()
    assert len(c.dispatched) == 1
    event, _label = c.dispatched[0]
    assert event["kind"] == 10002
    assert event["tags"] == [
        ["r", READ_R, "read"],
        ["r", WRITE_R, "write"],
        ["r", BOTH_R],  # read+write -> plain marker
        ["r", DM_R, "write"],  # dm counts as write
    ]  # parked entry absent


def test_publish_relay_list_legacy_shape_without_entries():
    c = FakeClient(urls=["wss://x.example"])
    c.publish_relay_list()
    event, _label = c.dispatched[0]
    assert event["tags"] == [["r", "wss://x.example"]]


# -- import-from-profile merge ----------------------------------------------------------


def test_import_from_profile_keeps_roles_and_order():
    c = _client_with_entries()
    added = c.import_relays_from_profile(
        [READ_R, "wss://imported.example", "http://not-ws.example"]
    )
    assert added == ["wss://imported.example"]
    urls = [e["url"] for e in c.relay_entries]
    assert urls[:4] == [READ_R, WRITE_R, BOTH_R, DM_R]  # order kept
    assert urls[-1] == "wss://imported.example"
    assert gnostr.relay_entries.entry_for(c.relay_entries, READ_R)["roles"] == ["read"]
    assert "wss://imported.example" in c.connected


# -- DM routing chain: dm -> peer 10050 -> write ---------------------------------------


def test_dm_role_relays_take_priority():
    c = _client_with_entries()
    c._publish_dm_targeted(PEER, _event())
    assert c.dm_lookups == []  # explicit dm role skips the 10050 lookup
    assert len(c.active_relays[DM_R].published) == 1
    assert not c.active_relays[WRITE_R].published


def test_dm_falls_back_to_peer_relays_without_dm_role():
    entries = [e for e in _entries() if e["url"] != DM_R]
    c = FakeClient(entries=entries)
    for u in (READ_R, WRITE_R, BOTH_R):
        c.active_relays[u] = FakeRelay(u)
    c._peer_relays = [WRITE_R]
    c._publish_dm_targeted(PEER, _event())
    assert c.dm_lookups == [PEER]
    assert len(c.active_relays[WRITE_R].published) == 1
    assert not c.active_relays[READ_R].published


def test_dm_falls_back_to_write_relays_when_peer_has_no_10050():
    entries = [e for e in _entries() if e["url"] != DM_R]
    c = FakeClient(entries=entries)
    for u in (READ_R, WRITE_R, BOTH_R):
        c.active_relays[u] = FakeRelay(u)
    c._peer_relays = None  # no kind-10050
    c._publish_dm_targeted(PEER, _event())
    assert c.active_relays[WRITE_R].published
    assert c.active_relays[BOTH_R].published
    assert not c.active_relays[READ_R].published
    # fell back, so NO dm-unreachable (the abort only fires with no write relays)
    assert ("dm-unreachable", PEER, "e" * 64) not in c.emitted


def test_dm_aborts_without_entries_model_at_all():
    c = FakeClient()  # relay_entries None: legacy behaviour unchanged
    c._peer_relays = None
    c._publish_dm_targeted(PEER, _event())
    assert ("dm-unreachable", PEER, "e" * 64) in c.emitted


# -- activity log role tags ---------------------------------------------------------------


def test_role_tag_shape():
    c = _client_with_entries()
    assert c._role_tag(READ_R) == "[r]"
    assert c._role_tag(BOTH_R) == "[rw]"
    assert c._role_tag(DM_R) == "[d]"
    assert c._role_tag("wss://unknown.example") == ""
    legacy = FakeClient(urls=["wss://x.example"])
    assert legacy._role_tag("wss://x.example") == ""


def test_status_log_line_carries_role_tag():
    c = _client_with_entries()
    status = types.SimpleNamespace(status="CONNECTED")
    c._handle_status(BOTH_R, status)
    assert any("[rw] " in line and BOTH_R in line for line in c.log)
