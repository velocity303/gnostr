"""NIPW-P3-FIX-2 regression tests (headless, tier 1).

Two FLX1 device bugs on the group detail page:

1. Join/leave KeyError 'id': groups.build_join_event / build_leave_event
   return UNSIGNED templates (no id); _on_join_clicked read ev["id"] before
   any signing step (dispatch_signed_to_relay signs downstream) -> KeyError
   killed every Join click. Fix: the event id is the SHA-256 of the canonical
   serialization [0,pubkey,created_at,kind,tags,content] — sig-independent —
   so nostr_utils.compute_event_id(template) is byte-identical to the signed
   event's id; the machine gate now receives that id and dispatch still signs
   downstream. Tests drive the REAL extracted methods with a signer-fake
   client and assert machine.join_event_id == signed event's id.

2. First-search NIP-11 race: kind-39000/39001/39002 snapshot events can land
   before the NIP-11 fetch resolves; _nip11_pk() was None so the self-key
   gate dropped them and the preview never populated (second search worked
   because NIP-11 was cached). Fix: buffer while unresolved, replay in
   _on_nip11. Test feeds kind-1 + kind-39000 BEFORE resolution, resolves,
   asserts the preview populates without a second search.

Under the conftest gi mocks `class GroupDetailPage(Gtk.Box)` evaluates
against a Mock base, so the real methods are extracted from source via ast
and bound to a lightweight fake self (house pattern: test_dm_ui_wiring.py).
Real-GTK end-to-end coverage: tests/test_group_timeline_gtk_real.py.
"""

import ast
import json
import os
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

from gnostr import groups, nostr_utils

_PAGE_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "src",
        "gnostr",
        "ui",
        "groups_page.py",
    )
)

RELAY = "wss://relay.example"
GID = "blossom"
RELAY_PRIV, RELAY_PK = nostr_utils.generate_keypair()
USER_PRIV, USER_PK = nostr_utils.generate_keypair()

_METHODS = (
    "_on_nip11",
    "_on_group_event",
    "_process_group_event",
    "_on_join_clicked",
    "_on_leave_clicked",
    "_render_preview",
    "_render_status",
    "_show_abort",
    "_append_timeline_event",
    "_activity_row_content",
    "_nip11_pk",
    "_mark_step_ok",
    "_arm_join_timeout",
    "_cancel_join_timeout",
    "_cancel_leave_timeout",
    "_on_join_timeout",
    "_on_leave_timeout",
    "_arm_resolve_deadline",
    "_on_resolve_deadline",
    "_show_resolve_failed",
)


def _extract(name, is_method=True):
    src = open(_PAGE_PATH, encoding="utf-8").read()
    tree = ast.parse(src)
    if is_method:
        cls = next(
            n
            for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == "GroupDetailPage"
        )
        fn = next(
            n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name
        )
    else:
        fn = next(
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == name
        )
    ns = {
        "Adw": MagicMock(),
        "Gtk": MagicMock(),
        "GLib": MagicMock(),
        "groups": groups,
        "nostr_utils": nostr_utils,
        "time": __import__("time"),
        "_diag": lambda *a, **k: None,
        "_SUB_STEPS": {
            "gmeta": "sub_meta",
            "gmemset": "sub_members",
            "gtl": "sub_timeline",
            "gmem": "sub_membership",
        },
        "JOIN_CONFIRM_TIMEOUT_MS": 10_000,
        "RESOLVE_META_TIMEOUT_MS": 6_000,
    }
    if is_method:
        ns["client_pk"] = _extract("client_pk", is_method=False)
        ns["_short_pk"] = _extract("_short_pk", is_method=False)
        ns["_ago"] = _extract("_ago", is_method=False)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), _PAGE_PATH, "exec"), ns)
    return ns[name]


class _Rec:
    """Records set_text/set_visible/set_sensitive on a fake Gtk widget."""

    def __init__(self, text=""):
        self.text = text
        self.visible = True
        self.sensitive = True

    def set_text(self, t):
        self.text = t

    def set_visible(self, v):
        self.visible = bool(v)

    def set_sensitive(self, s):
        self.sensitive = bool(s)


class _FakeBox:
    def __init__(self):
        self.rows = []

    def append(self, w):
        self.rows.append(w)

    def insert(self, w, pos):
        # NIPW-P3-ACTIVITY: real code inserts newest-first into the ListBox.
        self.rows.insert(pos, w)


class _FakeClient:
    """Signer fake: dispatch_signed_to_relay signs the template the way the
    real client's internal _sign does, and records what was published."""

    def __init__(self):
        self.my_pubkey = USER_PK
        self.dispatched = []

    def dispatch_signed_to_relay(self, event, url, label=""):
        signed = nostr_utils.sign_event(dict(event), USER_PRIV)
        self.dispatched.append((signed, url, label))
        return True

    def subscribe_on_relay(self, *a, **k):
        return False

    def close_sub_on_relay(self, *a):
        pass


def _make_page(self_pk=None, machine=None):
    fake = SimpleNamespace()
    fake.client = _FakeClient()
    fake.main_window = SimpleNamespace(client=fake.client, toasts=[])
    fake.main_window.add_toast = lambda t: fake.main_window.toasts.append(t)
    fake.relay = RELAY
    fake.group_id = GID
    fake.machine = machine or groups.GroupJoinMachine(GID, RELAY, self_pk)
    fake._self_pk = self_pk
    fake._meta = None
    fake._seen_event_ids = set()
    fake._sub_ids = []
    fake._handlers = []
    fake._pre_nip11_events = []
    fake._leave_event_id = None
    fake._step_t0 = {}
    fake._step_logged = set()
    fake._pending_subs = []
    fake._resolve_deadline_id = None
    fake._join_timeout_id = None
    fake._leave_timeout_id = None
    fake.title_lbl = _Rec("Test Group")
    fake.about_lbl = _Rec("Loading group info…")
    fake.flags_lbl = _Rec("")
    fake.status_lbl = _Rec("")
    fake.join_btn = _Rec()
    fake.leave_btn = _Rec()
    fake.retry_btn = _Rec()
    fake.timeline = _FakeBox()
    for name in _METHODS:
        setattr(fake, name, types.MethodType(_extract(name), fake))
    return fake


def _preview_machine():
    m = groups.GroupJoinMachine(GID, RELAY, RELAY_PK)
    m.begin()
    m.on_metadata(
        groups.GroupMeta(RELAY, GID, name="Blossom Garden", about="growing together")
    )
    assert m.state == "preview"
    return m


def _signed(ev, priv):
    return nostr_utils.sign_event(dict(ev), priv)


def _meta_event():
    return _signed(
        {
            "pubkey": RELAY_PK,
            "created_at": 1763000000,
            "kind": groups.KIND_GROUP_METADATA,
            "tags": [
                ["d", GID],
                ["name", "Blossom Garden"],
                ["about", "growing together"],
            ],
            "content": "",
        },
        RELAY_PRIV,
    )


# --- Bug 1: join / leave must gate + record on the SIGNED event id ----------


def test_join_click_gates_machine_on_signed_id():
    """The machine gate must receive the id the signed 9021 carries (the
    OK-ack matches on it) and dispatch must be called. Pre-fix this raised
    KeyError: 'id' on the unsigned template (groups_page _on_join_clicked)."""
    fake = _make_page(self_pk=RELAY_PK, machine=_preview_machine())
    fake._on_join_clicked(None)
    assert len(fake.client.dispatched) == 1
    signed, url, label = fake.client.dispatched[0]
    assert signed["kind"] == groups.KIND_GROUP_JOIN_REQUEST
    assert signed["pubkey"] == USER_PK
    assert [t for t in signed["tags"] if t[0] == "h"] == [["h", GID]]
    assert signed.get("sig"), "dispatched event must be signed"
    # the machine gate received exactly the signed event's id
    assert fake.machine.state == "join_sent"
    assert fake.machine.join_event_id == signed["id"]


def test_join_gate_still_blocks_closed_group_without_invite():
    m = groups.GroupJoinMachine(GID, RELAY, RELAY_PK)
    m.begin()
    m.on_metadata(
        groups.GroupMeta(RELAY, GID, name="Locked", flags=["closed"])
    )
    fake = _make_page(self_pk=RELAY_PK, machine=m)
    fake._on_join_clicked(None)
    assert fake.machine.state == "preview"
    assert fake.client.dispatched == []


def test_leave_click_records_signed_id():
    """Same latent KeyError on the 9022 template: _leave_event_id must equal
    the signed event's id so the publish-result ack can match it."""
    m = _preview_machine()
    m.on_join("a" * 64)
    m.on_join_ok(True, "")
    m.on_membership("member")
    assert m.state == "member"
    fake = _make_page(self_pk=RELAY_PK, machine=m)
    fake._on_leave_clicked(None)
    assert len(fake.client.dispatched) == 1
    signed, url, label = fake.client.dispatched[0]
    assert signed["kind"] == groups.KIND_GROUP_LEAVE
    assert fake._leave_event_id == signed["id"]


# --- Bug 2: first-search NIP-11 race -----------------------------------------


def test_first_search_preview_populates_after_nip11_resolves():
    """kind-1 + kind-39000 arrive BEFORE _nip11_pk resolves. Pre-fix the
    39000 hit `return` at the self-key gate and was dropped forever (preview
    stayed 'Loading group info…' until a second, cached search). Now it is
    buffered and replayed when the pk lands."""
    fake = _make_page(self_pk=None)  # NIP-11 still in flight
    fake.machine.begin()

    tl = _signed(
        {
            "pubkey": USER_PK,
            "created_at": 1763000001,
            "kind": 1,
            "tags": [["h", GID]],
            "content": "first light on blossom",
        },
        USER_PRIV,
    )
    fake._on_group_event(None, RELAY, json.dumps(tl))
    assert len(fake.timeline.rows) == 1  # timeline appends unconditionally

    meta_ev = _meta_event()
    admins_ev = _signed(
        {
            "pubkey": RELAY_PK,
            "created_at": 1763000002,
            "kind": groups.KIND_GROUP_ADMINS,
            "tags": [["d", GID], ["p", USER_PK, "king"]],
            "content": "",
        },
        RELAY_PRIV,
    )
    fake._on_group_event(None, RELAY, json.dumps(meta_ev))
    fake._on_group_event(None, RELAY, json.dumps(admins_ev))
    # buffered, NOT rendered, NOT dropped
    assert fake._meta is None
    assert fake.machine.state == "resolving"
    assert fake.about_lbl.text == "Loading group info…"
    assert len(fake._pre_nip11_events) == 2

    # NIP-11 resolves — first search must now fully populate (no second search)
    fake._on_nip11({"name": "example relay", "supported_nips": [29], "pubkey": RELAY_PK})
    assert fake._self_pk == RELAY_PK
    assert fake.machine.state == "preview"
    assert fake.title_lbl.text == "Blossom Garden"
    assert fake.about_lbl.text == "growing together"
    assert "1 admin(s)" in fake.flags_lbl.text
    assert fake.join_btn.sensitive is True
    assert fake._pre_nip11_events == []
    assert len(fake.timeline.rows) == 1  # replay must not duplicate timeline

    # events arriving AFTER resolution still flow through normally
    late = _signed(
        {
            "pubkey": USER_PK,
            "created_at": 1763000003,
            "kind": 7,
            "tags": [["h", GID], ["e", tl["id"]]],
            "content": "+",
        },
        USER_PRIV,
    )
    fake._on_group_event(None, RELAY, json.dumps(late))
    assert len(fake.timeline.rows) == 2


def test_self_key_gate_still_drops_forged_metadata_after_resolve():
    """Buffering must not weaken the gate: a 39000 NOT signed by the relay
    self key is still never rendered."""
    fake = _make_page(self_pk=None)
    fake.machine.begin()
    forged = _signed(
        {
            "pubkey": USER_PK,  # wrong signer
            "created_at": 1763000000,
            "kind": groups.KIND_GROUP_METADATA,
            "tags": [["d", GID], ["name", "Forged"]],
            "content": "",
        },
        USER_PRIV,
    )
    fake._on_group_event(None, RELAY, json.dumps(forged))
    fake._on_nip11({"supported_nips": [29], "pubkey": RELAY_PK})
    assert fake._meta is None
    assert fake.machine.state == "resolving"
    assert fake.title_lbl.text == "Test Group"


def test_buffer_is_bounded():
    fake = _make_page(self_pk=None)
    fake.machine.begin()
    for i in range(100):
        ev = _signed(
            {
                "pubkey": RELAY_PK,
                "created_at": 1763000000 + i,
                "kind": groups.KIND_GROUP_METADATA,
                "tags": [["d", GID], ["name", f"g{i}"]],
                "content": "",
            },
            RELAY_PRIV,
        )
        fake._process_group_event(ev)
    assert len(fake._pre_nip11_events) == 64
