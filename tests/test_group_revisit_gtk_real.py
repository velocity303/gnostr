"""Real-GTK revisit harness (NIPW-P3-FIX-3, FLX1 round-3 device log).

FLX1 cycle on repeat visits to Wisp:

  sub_open x4 ok -> nip11 ok ms=0 -> sub_meta result=fail -> subs re-opened
  (three cycles) — although the group's metadata was ALREADY upserted into
  the gateway DB on the first visit.

Harness (real GTK4 + real GObject signal machinery + REAL SQLite gateway):

1. A pre-populated gateway row renders the FULL preview synchronously at
   page construction — before any live 39000, any EOSE, any pump. That is
   the "render meta WITHOUT waiting on sub_meta" acceptance line.
2. EOSE-with-no-events on the metadata snapshot is classified once, as
   'empty' (background refresh found nothing) when the page already has
   cached content — never as a failure, never a re-REQ sweep.
3. Without a cache the empty snapshot still ends in ONE explicit
   resolve_failed + Retry (no loop).
4. join OK-ack shows 'Join requested — awaiting approval' (pending), NOT a
   silent member/pending ambiguity; a 39002 push carrying us flips status to
   '✓ Member' with [GROUP] step=member_confirm; absence says member_pending.
5. Persisted 'pending' membership survives a revisit as REQUESTED.

Runs standalone (NOT under the pytest suite: conftest mocks gi). Skips with
exit 77 when real GTK4/Adw or a display is unavailable.

Usage:  xvfb-run -a python3 tests/test_group_revisit_gtk_real.py
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import types

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib, GObject
except Exception as e:
    print("SKIP: real GTK4/Adw unavailable (%s)" % e)
    sys.exit(77)

if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    print("SKIP: no display (run under xvfb-run)")
    sys.exit(77)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src", "gnostr")

criticals = []


def _log_handler(domain, level, message, user_data):
    criticals.append("%s: %s" % (domain, message.strip()))


GLib.log_set_handler(
    None,
    GLib.LogLevelFlags.LEVEL_ERROR | GLib.LogLevelFlags.LEVEL_CRITICAL,
    _log_handler,
    None,
)

# Stub the gnostr package so real modules load without gnostr/__init__
# (which needs a compiled GResource).
_gp = types.ModuleType("gnostr")
_gp.__path__ = [SRC]
sys.modules["gnostr"] = _gp
for _pkg in ("ui", "gateway"):
    _m = types.ModuleType("gnostr." + _pkg)
    _m.__path__ = [os.path.join(SRC, _pkg)]
    sys.modules["gnostr." + _pkg] = _m

import gnostr.groups as groups  # noqa: E402
import gnostr.nostr_utils as nostr_utils  # noqa: E402
from gnostr.gateway.groups_gateway import SQLiteGroupsGateway  # noqa: E402


def _load_groups_page():
    spec = importlib.util.spec_from_file_location(
        "gnostr.ui.groups_page", os.path.join(SRC, "ui", "groups_page.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gnostr.ui.groups_page"] = mod
    spec.loader.exec_module(mod)
    return mod


groups_page = _load_groups_page()

RELAY = "wss://relay.invalid"
GID = "test-group"
RELAY_PRIV, RELAY_PK = nostr_utils.generate_keypair()
USER_PRIV, USER_PK = nostr_utils.generate_keypair()
OTHER_PK = "d" * 64
NIP11_DOC = {"name": "harness relay", "supported_nips": [29], "pubkey": RELAY_PK}

_good_nip11 = lambda relay_url, opener=None, timeout=10: dict(NIP11_DOC)
groups.fetch_nip11 = _good_nip11  # most checks resolve NIP-11 successfully


def _signed(ev, priv):
    return nostr_utils.sign_event(dict(ev), priv)


def _meta_event():
    return _signed(
        {
            "pubkey": RELAY_PK,
            "created_at": 1763000000,
            "kind": groups.KIND_GROUP_METADATA,
            "tags": [["d", GID], ["name", "Blossom Garden"],
                     ["about", "growing together"]],
            "content": "",
        },
        RELAY_PRIV,
    )


def _members_event(members, created_at):
    return _signed(
        {
            "pubkey": RELAY_PK,
            "created_at": created_at,
            "kind": groups.KIND_GROUP_MEMBERS,
            "tags": [["d", GID]] + [["p", m] for m in members],
            "content": "",
        },
        RELAY_PRIV,
    )


GMETA_SUB = groups.sub_id_for("gmeta", GID)


class FakeClient(GObject.Object):
    """Real GObject: connect()/emit() exercise real signal machinery,
    including the new 'sub-eose' signal the page must receive."""

    __gsignals__ = {
        "group-event-received": (GObject.SignalFlags.RUN_LAST, None, (str, str)),
        "publish-result": (
            GObject.SignalFlags.RUN_LAST, None, (str, bool, str, str, str)
        ),
        "relay-connected": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "sub-eose": (GObject.SignalFlags.RUN_LAST, None, (str, str)),
    }

    def __init__(self, db_path):
        super().__init__()
        self.my_pubkey = USER_PK
        self.dispatched = []
        self.subs_opened = []
        self.sub_open_ok = True  # page keys _sub_ids (and EOSE matching) on this
        self._groups_gateway = SQLiteGroupsGateway(db_path)
        self._groups_gateway.init_db()

    def subscribe_on_relay(self, url, sub_id, filters, snapshot=False):
        self.subs_opened.append((sub_id, snapshot))
        return self.sub_open_ok

    def dispatch_signed_to_relay(self, event, url, label=""):
        self.dispatched.append(nostr_utils.sign_event(dict(event), USER_PRIV))
        return True

    def publish_groups_list(self, refs):
        # Real client implements this (client.py:2559); harness only needs
        # the member-confirm registry sync to not blow up.
        return True


def _mw(client):
    return types.SimpleNamespace(
        client=client,
        pub_key=USER_PK,
        content_nav=None,
        toasts=[],
        add_toast=lambda t: None,
    )


def _pump(seconds=0.3):
    deadline = time.time() + seconds
    ctx = GLib.MainContext.default()
    while time.time() < deadline:
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.01)


def _seed(gw, name=None, about=None, membership=None):
    gw.upsert_group(
        RELAY, GID,
        name=name or "Wisp Garden",
        about=about or "the quiet corner of the relay",
        picture=None, banner=None, flags=["public"], admins=[],
        member_count=2, supported_kinds=[1, 39000],
        fetched_at=int(time.time()) - 120,
    )
    if membership:
        gw.set_membership(RELAY, GID, membership)


def _capture():
    """contextlib.redirect_stdout wrapper capturing [GROUP] diag lines."""
    buf = io.StringIO()

    @contextlib.contextmanager
    def ctx():
        with contextlib.redirect_stdout(buf):
            yield

    return ctx, buf


def _diags(buf, step):
    hits = [ln for ln in buf.getvalue().splitlines()
            if "step=" + step + " " in ln]
    return hits


# --- checks -------------------------------------------------------------------


def check_revisit_renders_cached_meta_synchronously():
    """Acceptance line: second page-entry with a pre-populated gateway row
    renders the description IMMEDIATELY — constructed with zero pumps, zero
    relay events, zero EOSE. (FLX1: revisit cycled sub_meta fail -> re-open.)"""
    tmp = tempfile.mkdtemp()
    client = FakeClient(os.path.join(tmp, "gnostr.db"))
    _seed(client._groups_gateway)
    ctx, buf = _capture()
    with ctx():
        page = groups_page.GroupDetailPage(_mw(client), RELAY, GID, name="Wisp")
    assert page.about_lbl.get_text() == "the quiet corner of the relay", (
        "cached about not rendered at construction: %r"
        % page.about_lbl.get_text()
    )
    assert page.title_lbl.get_text() == "Wisp Garden"
    assert not _diags(buf, "sub_meta"), "sub_meta settled before the live probe"
    assert _diags(buf, "cached_meta"), "cached_meta diag missing"
    # live 39000 continues as a BACKGROUND refresh: EOSE-with-nothing must
    # be 'empty', never a failure, and must not arm any re-REQ.
    with ctx():
        _pump(0.3)  # NIP-11 idle hand-back
        client.emit("sub-eose", GMETA_SUB, RELAY)
        _pump(0.1)
    empty = _diags(buf, "sub_meta")
    assert empty and "result=empty" in empty[0], empty
    assert not _diags(buf, "re.req") and "re-open" not in buf.getvalue()
    assert "result=fail" not in "".join(_diags(buf, "sub_meta"))
    assert not page.retry_btn.get_visible()
    print("PASS: revisit renders cached meta at construction; empty EOSE = "
          "background-refresh miss, no failure, no re-REQ")


def check_revisit_membership_requested_survives():
    """A persisted join-request (pending) re-enters as REQUESTED — never
    preview, never member. Status text must say requested-vs-confirmed."""
    tmp = tempfile.mkdtemp()
    client = FakeClient(os.path.join(tmp, "gnostr.db"))
    _seed(client._groups_gateway, membership="pending")
    ctx, buf = _capture()
    with ctx():
        page = groups_page.GroupDetailPage(_mw(client), RELAY, GID, name="Wisp")
    assert page.machine.state == "pending", page.machine.state
    assert page.status_lbl.get_text() == "Join requested — awaiting approval", (
        page.status_lbl.get_text()
    )
    assert not page.join_btn.get_sensitive()
    with ctx():
        _pump(0.3)
        client.emit("sub-eose", GMETA_SUB, RELAY)
        _pump(0.1)
    # a 39002 push carrying us confirms the request (khatru: no 9000 audit)
    with ctx():
        client.emit("group-event-received", RELAY,
                    json.dumps(_members_event([USER_PK], 1763000100)))
        _pump(0.1)
    assert page.machine.state == "member", page.machine.state
    assert page.status_lbl.get_text() == "✓ Member"
    assert _diags(buf, "member_confirm")
    assert client._groups_gateway.get_membership(RELAY, GID) == "member"
    print("PASS: persisted requested-state survives revisit; 39002 push flips "
          "to member with member_confirm diag + persistence")


def check_revisit_member_state_persists():
    tmp = tempfile.mkdtemp()
    client = FakeClient(os.path.join(tmp, "gnostr.db"))
    _seed(client._groups_gateway, membership="member")
    ctx, buf = _capture()
    with ctx():
        page = groups_page.GroupDetailPage(_mw(client), RELAY, GID, name="Wisp")
    assert page.machine.state == "member", page.machine.state
    assert page.status_lbl.get_text() == "✓ Member"
    # a stale 39002 WITHOUT us (relayed history) must not downgrade member
    with ctx():
        _pump(0.3)
        client.emit("group-event-received", RELAY,
                    json.dumps(_members_event([OTHER_PK], 1763000050)))
    assert page.machine.state == "member", page.machine.state
    print("PASS: cached member membership survives revisit and stale 39002")


def check_fresh_empty_eose_fails_once_with_retry():
    """No cache -> empty snapshot IS an explicit failure: exactly ONE
    resolve_failed + Retry, no sweep, no loop."""
    tmp = tempfile.mkdtemp()
    client = FakeClient(os.path.join(tmp, "gnostr.db"))
    ctx, buf = _capture()
    with ctx():
        page = groups_page.GroupDetailPage(_mw(client), RELAY, GID)
        _pump(0.3)  # NIP-11 ok, no cached row: still resolving
        client.emit("sub-eose", GMETA_SUB, RELAY)
        _pump(0.1)
    assert page.machine.state == "resolve_failed", page.machine.state
    assert page.retry_btn.get_visible()
    fails = _diags(buf, "sub_meta")
    assert len([l for l in fails if "result=fail" in l]) == 1, fails
    assert not _diags(buf, "re.req") and "re-open" not in buf.getvalue()
    # the resolve deadline was CANCELLED by classification: pumping well past
    # 8s must not produce a second failure line
    with ctx():
        _pump(0.4)
    assert len([l for l in _diags(buf, "sub_meta") if "result=fail" in l]) == 1
    print("PASS: empty snapshot without cache = one explicit retryable "
          "failure, deadline cancelled, no re-REQ loop")


def check_join_ok_is_requested_then_confirmed():
    """Device-test contract (blossom): OK-ack shows REQUESTED explicitly;
    only the 39002 carrying us says Member; absence logs member_pending."""
    tmp = tempfile.mkdtemp()
    client = FakeClient(os.path.join(tmp, "gnostr.db"))
    ctx, buf = _capture()
    with ctx():
        page = groups_page.GroupDetailPage(_mw(client), RELAY, GID)
        _pump(0.3)
        client.emit("group-event-received", RELAY, json.dumps(_meta_event()))
    assert page.machine.state == "preview", page.machine.state
    page.join_btn.emit("clicked")
    assert page.machine.state == "join_sent"
    signed = client.dispatched[0]
    with ctx():
        client.emit("publish-result", signed["id"], True, "", RELAY, "Group join")
    assert page.machine.state == "pending", page.machine.state
    assert page.status_lbl.get_text() == "Join requested — awaiting approval"
    assert _diags(buf, "member_pending"), "join_ok accepted but pending not logged"
    assert client._groups_gateway.get_membership(RELAY, GID) == "pending"
    # approval-gated relay: a members push WITHOUT us keeps 'requested'
    with ctx():
        client.emit("group-event-received", RELAY,
                    json.dumps(_members_event([OTHER_PK], 1763000200)))
        _pump(0.05)
    assert page.machine.state == "pending", page.machine.state
    assert page.status_lbl.get_text() == "Join requested — awaiting approval"
    # admin approves: 39002 now carries us
    with ctx():
        client.emit("group-event-received", RELAY,
                    json.dumps(_members_event([OTHER_PK, USER_PK], 1763000300)))
        _pump(0.1)
    assert page.machine.state == "member", page.machine.state
    assert page.status_lbl.get_text() == "✓ Member"
    assert _diags(buf, "member_confirm")
    assert client._groups_gateway.get_membership(RELAY, GID) == "member"
    print("PASS: join_ok=requested (persisted), absence stays requested, "
          "39002-with-us=member_confirm")


def main():
    checks = [
        check_revisit_renders_cached_meta_synchronously,
        check_revisit_membership_requested_survives,
        check_revisit_member_state_persists,
        check_fresh_empty_eose_fails_once_with_retry,
        check_join_ok_is_requested_then_confirmed,
    ]
    failed = 0
    for c in checks:
        try:
            c()
        except Exception as e:
            failed += 1
            import traceback

            traceback.print_exc()
            print("FAIL: %s: %s" % (c.__name__, e))
    if criticals:
        for line in criticals:
            print("GTK-CRITICAL: %s" % line)
        failed += 1
    print("%d/%d checks passed" % (len(checks) - failed, len(checks)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
