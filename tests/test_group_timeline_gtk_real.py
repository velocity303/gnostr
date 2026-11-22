"""Real-GTK group timeline harness (NIPW-P3-FIX, FLX1 device log).

The FLX1 log showed, on EVERY group timeline event:

  AttributeError: 'ActionRow' object has no attribute 'get_subtitle_label'

Adw.ActionRow has no subtitle-label accessor; the call died BEFORE
self.timeline.append(row), so the timeline was always empty. The 52 mocked
tier-1 tests never built a real row, so the suite stayed green (third
device-only bug in three cards that a real-widget test would have caught —
same class as NIPW-P1 Bug A / NIPW-P2 notify::).

Harness: construct the REAL GroupDetailPage under real GTK with a real
GObject client stub, then emit the real 'group-event-received' signal with
kind-1/7/9 events (full path: signal signature + handler + row build +
append). Assert rows landed in the timeline with the 4-line subtitle cap.
A swallowed AttributeError inside the handler means zero rows appended —
the check fails on the count.

Runs standalone (NOT under the pytest suite: conftest mocks gi).
Skips cleanly with exit code 77 (meson "skip") when real GTK4/Adw or a
display is unavailable.

Usage:  xvfb-run -a python3 tests/test_group_timeline_gtk_real.py
"""

import importlib.util
import json
import os
import sys
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

# GTK criticals/warnings captured through the log handler (4-arg form —
# the 1-arg binding raises TypeError in PyGObject).
criticals = []


def _log_handler(domain, level, message, user_data):
    criticals.append("%s: %s" % (domain, message.strip()))


GLib.log_set_handler(
    None,
    GLib.LogLevelFlags.LEVEL_ERROR | GLib.LogLevelFlags.LEVEL_CRITICAL,
    _log_handler,
    None,
)

# Stub the gnostr package so the REAL groups.py / groups_page.py load
# without running gnostr/__init__ (which needs a compiled GResource).
_gp = types.ModuleType("gnostr")
_gp.__path__ = [SRC]
sys.modules["gnostr"] = _gp
_ui = types.ModuleType("gnostr.ui")
_ui.__path__ = [os.path.join(SRC, "ui")]
sys.modules["gnostr.ui"] = _ui

import gnostr.groups as groups  # noqa: E402  (real logic layer, GTK-free)
import gnostr.nostr_utils as nostr_utils  # noqa: E402


def _load_groups_page():
    spec = importlib.util.spec_from_file_location(
        "gnostr.ui.groups_page", os.path.join(SRC, "ui", "groups_page.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gnostr.ui.groups_page"] = mod
    spec.loader.exec_module(mod)
    return mod


groups_page = _load_groups_page()

# No network in the harness: the resolve worker gets an instant None doc
# (machine aborts; the timeline path is independent of the preview gate).


def _no_nip11(relay_url, opener=None, timeout=10):
    return None


groups.fetch_nip11 = _no_nip11

RELAY = "wss://relay.invalid"
SELF_PK = "a" * 64
AUTHOR_PK = "b" * 64

# Real keypairs for the NIPW-P3-FIX-2 checks: the 39000 must really verify
# against the NIP-11 self pubkey, and the join 9021 must really sign.
RELAY_PRIV, RELAY_PK = nostr_utils.generate_keypair()
USER_PRIV, USER_PK = nostr_utils.generate_keypair()
NIP11_DOC = {"name": "harness relay", "supported_nips": [29], "pubkey": RELAY_PK}


def _signed(ev, priv):
    return nostr_utils.sign_event(dict(ev), priv)


def _meta_event():
    return _signed(
        {
            "pubkey": RELAY_PK,
            "created_at": 1763000000,
            "kind": groups.KIND_GROUP_METADATA,
            "tags": [["d", "test-group"], ["name", "Blossom Garden"],
                     ["about", "growing together"]],
            "content": "",
        },
        RELAY_PRIV,
    )


class FakeClient(GObject.Object):
    """Real GObject so connect()/emit() exercise real signal machinery —
    a plain-object stub cannot catch a handler-signature mismatch."""

    __gsignals__ = {
        "group-event-received": (GObject.SignalFlags.RUN_LAST, None, (str, str)),
        "publish-result": (
            GObject.SignalFlags.RUN_LAST,
            None,
            (str, bool, str, str, str),
        ),
        "relay-connected": (GObject.SignalFlags.RUN_LAST, None, (str,)),
    }

    def __init__(self):
        super().__init__()
        self.my_pubkey = SELF_PK
        self.dispatched = []
        self.subs_opened = []
        self.sub_open_ok = False  # flip to True to simulate a live relay

    def subscribe_on_relay(self, url, sub_id, filters, snapshot=False):
        self.subs_opened.append((sub_id, snapshot))
        return self.sub_open_ok

    def dispatch_signed_to_relay(self, event, url, label=""):
        # Signer fake: sign the template the way the real client's internal
        # _sign does, so tests can assert on the SIGNED event's id.
        self.dispatched.append(nostr_utils.sign_event(dict(event), USER_PRIV))
        return True


def _mw(client):
    return types.SimpleNamespace(
        client=client,
        pub_key=SELF_PK,
        content_nav=None,
        toasts=[],
        add_toast=lambda t: None,
    )


def _pump(seconds=0.3):
    """Let the resolve worker's GLib.idle_add hand-back run."""
    deadline = time.time() + seconds
    ctx = GLib.MainContext.default()
    while time.time() < deadline:
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.01)


def _build_page():
    client = FakeClient()
    page = groups_page.GroupDetailPage(_mw(client), RELAY, "test-group", name="Test Group")
    _pump()
    return client, page


def _timeline_rows(page):
    rows = []
    child = page.timeline.get_first_child()
    while child is not None:
        rows.append(child)
        child = child.get_next_sibling()
    return rows


def _ev(eid, kind, content="hello group"):
    return {"id": eid, "kind": kind, "pubkey": AUTHOR_PK, "content": content}


def _short(pk):
    return f"{pk[:8]}…{pk[-4:]}"


def check_timeline_appends_rows():
    """Real signal emission of kind-1/7/9 events must land one
    Adw.ActionRow per event in the timeline (the FLX1 bug appended zero).
    NIPW-P3-ACTIVITY: rows are verb-labelled and NEWEST FIRST (insert 0),
    so the live tail is under the "Recent activity" header on a phone."""
    client, page = _build_page()
    client.emit("group-event-received", RELAY, json.dumps(_ev("a1", 1)))
    client.emit("group-event-received", RELAY, json.dumps(_ev("a2", 7, "+")))
    client.emit("group-event-received", RELAY, json.dumps(_ev("a3", 9, "")))
    rows = _timeline_rows(page)
    assert len(rows) == 3, "expected 3 timeline rows, got %d" % len(rows)
    assert all(isinstance(r, Adw.ActionRow) for r in rows)
    author = _short(AUTHOR_PK)
    assert rows[0].get_title() == "📁 shared a file · %s" % author
    assert rows[1].get_title() == "❤ reacted · %s" % author
    assert rows[2].get_title() == "posted · %s" % author
    assert rows[2].get_subtitle() == "hello group"
    # The 4-line cap the old (crashing) subtitle-label calls intended.
    assert rows[2].get_subtitle_lines() == 4, "subtitle line cap not applied"
    print("PASS: kind-1/7/9 emit verb-labelled rows, newest first")


def check_activity_kinds_from_live_audit():
    """NIPW-P3-ACTIVITY: the kinds the field-test groups actually use
    (nak audit 2026-11-22: blossom = kind-9 Blossom file events, wisp =
    kind-11 videos + kind-1111 comments; zaps = 9734) must render labelled
    rows with a relative timestamp — the empty-page complaint's root cause."""
    client, page = _build_page()
    now = int(time.time())
    evs = [
        {"id": "k6", "kind": 6, "pubkey": AUTHOR_PK, "created_at": now - 7200,
         "content": "reposted note text"},
        {"id": "k9", "kind": 9, "pubkey": AUTHOR_PK, "created_at": now - 3600,
         "content": json.dumps({"file": {"name": "release.iso", "size": 42},
                                "blobs": []})},
        {"id": "k11", "kind": 11, "pubkey": AUTHOR_PK, "created_at": now - 600,
         "tags": [["h", "test-group"], ["title", "jensen huang dancing"]],
         "content": "https://r2a.primal.net/uploads2/7/a4/2e/clip.mp4"},
        {"id": "k1111", "kind": 1111, "pubkey": AUTHOR_PK, "created_at": now - 30,
         "content": "this is really good!"},
        {"id": "kzap", "kind": 9734, "pubkey": AUTHOR_PK, "created_at": now - 10,
         "content": "lnbc1...invoice-never-echoed"},
    ]
    for e in evs:
        client.emit("group-event-received", RELAY, json.dumps(e))
    rows = _timeline_rows(page)
    assert len(rows) == 5, "activity kinds emitted %d rows" % len(rows)
    titles = [r.get_title() for r in rows]
    # newest first: zap(10s) at top ... repost(2h) at bottom
    assert titles[0].startswith("⚡ zapped ·") and "just now" in titles[0], titles[0]
    assert titles[1].startswith("💬 commented ·") and titles[1].endswith("just now")
    assert titles[2].startswith("🎥 shared a video ·") and "10m ago" in titles[2]
    assert rows[2].get_subtitle() == "jensen huang dancing"
    assert titles[3].startswith("📁 shared a file ·")
    assert rows[3].get_subtitle() == "release.iso"
    assert titles[4].startswith("🔁 reposted ·") and "2h ago" in titles[4], titles[4]
    assert "invoice" not in titles[0] + rows[0].get_subtitle(), "9734 invoice leaked"
    print("PASS: kind 6/9/11/1111/9734 render labelled rows, newest first, invoice hidden")


def check_dedup_and_bad_events_ignored():
    """Re-delivery of a seen id adds nothing; an id-less event is skipped."""
    client, page = _build_page()
    client.emit("group-event-received", RELAY, json.dumps(_ev("b1", 1)))
    client.emit("group-event-received", RELAY, json.dumps(_ev("b1", 1)))
    client.emit("group-event-received", RELAY, json.dumps(_ev("", 1)))
    client.emit("group-event-received", RELAY, "{not json")
    client.emit("group-event-received", "wss://other.relay", json.dumps(_ev("b2", 1)))
    rows = _timeline_rows(page)
    assert len(rows) == 1, "dedup/relay-filter failed, rows=%d" % len(rows)
    print("PASS: dedup, malformed JSON, and foreign-relay events ignored")


def check_long_subtitle_row_survives():
    """A 500-char post body (the [:500] slice) builds without error —
    the crash path the device hit on every real post."""
    client, page = _build_page()
    client.emit("group-event-received", RELAY, json.dumps(_ev("c1", 1, "x" * 600)))
    rows = _timeline_rows(page)
    assert len(rows) == 1
    assert len(rows[0].get_subtitle()) == 500, "content not capped at 500"
    print("PASS: long post body renders capped at 500 chars")


def check_join_click_publishes_signed_join():
    """NIPW-P3-FIX-2 Bug 1 (FLX1: KeyError 'id' on every Join click). Real
    button click on a real preview-gated page: the machine gate must receive
    the id the SIGNED 9021 carries (OK-ack matches on it) and dispatch must
    fire exactly once."""
    groups.fetch_nip11 = lambda relay_url, opener=None, timeout=10: dict(NIP11_DOC)
    try:
        client, page = _build_page()
        assert page.machine.state == "resolving", page.machine.state
        client.my_pubkey = USER_PK
        client.emit(
            "group-event-received", RELAY, json.dumps(_meta_event())
        )
        assert page.machine.state == "preview", page.machine.state
        assert page.join_btn.get_sensitive(), "join button not enabled in preview"
        page.join_btn.emit("clicked")
        assert len(client.dispatched) == 1, "dispatch not called on Join click"
        signed = client.dispatched[0]
        assert signed["kind"] == groups.KIND_GROUP_JOIN_REQUEST
        assert signed["pubkey"] == USER_PK and signed.get("sig")
        assert page.machine.state == "join_sent"
        assert page.machine.join_event_id == signed["id"], (
            "machine gate got %r, signed event carries %r"
            % (page.machine.join_event_id, signed["id"])
        )
        # OK-ack for that exact id must be recognized. NIPW-P3-FIX-3 (Bug C):
        # accepted != member — the status must say REQUESTED, awaiting the
        # 39002 confirmation, never a silent member/pending ambiguity.
        client.emit("publish-result", signed["id"], True, "", RELAY, "Group join")
        assert page.machine.state == "pending", page.machine.state
        assert page.status_lbl.get_text() == "Join requested — awaiting approval", (
            page.status_lbl.get_text()
        )
        print("PASS: Join click gates on signed 9021 id + dispatch fires")
    finally:
        groups.fetch_nip11 = _no_nip11


def check_first_search_preview_race():
    """NIPW-P3-FIX-2 Bug 2 (FLX1: first search half-rendered). kind-1 +
    kind-39000 snapshot events land BEFORE the NIP-11 idle hand-back; they
    must be buffered, not dropped, and the preview must fully populate when
    the pk resolves — no second search."""
    groups.fetch_nip11 = lambda relay_url, opener=None, timeout=10: dict(NIP11_DOC)
    try:
        client = FakeClient()
        page = groups_page.GroupDetailPage(
            _mw(client), RELAY, "test-group", name="Test Group"
        )
        # No pump yet: the worker's GLib.idle_add hand-back has not run.
        client.emit("group-event-received", RELAY, json.dumps(_ev("d1", 1)))
        client.emit("group-event-received", RELAY, json.dumps(_meta_event()))
        assert page.machine.state == "resolving"
        assert page.about_lbl.get_text() == "Loading group info…", (
            "preview populated before NIP-11 resolved (gate leak)"
        )
        _pump(0.4)  # NIP-11 hand-back runs -> buffered events replay
        assert page.machine.state == "preview"
        assert page.title_lbl.get_text() == "Blossom Garden"
        assert page.about_lbl.get_text() == "growing together"
        assert page.join_btn.get_sensitive()
        assert len(_timeline_rows(page)) == 1, "replay duplicated timeline rows"
        print("PASS: first-search events buffered + replayed on NIP-11 resolve")
    finally:
        groups.fetch_nip11 = _no_nip11


def check_resolve_failure_shows_retry_state():
    """NIPW-P3-HARDEN (FLX1: description sometimes never loaded, page stuck
    at 'resolving'): a failed NIP-11 fetch must end in an explicit
    resolve_failed state with a visible Retry button — never a silent stall,
    and never the wrong terminal 'relay does not support groups'."""
    client, page = _build_page()  # _no_nip11 -> None
    assert page.machine.state == "resolve_failed", page.machine.state
    assert page.retry_btn.get_visible(), "Retry button not shown on failure"
    assert "NIP-11" in page.status_lbl.get_text(), page.status_lbl.get_text()
    assert not page.join_btn.get_sensitive()
    print("PASS: NIP-11 failure -> resolve_failed + Retry (no silent stall)")


def check_retry_recovers_preview():
    """Retry after a transient failure re-resolves and reaches preview once
    the metadata event lands."""
    client, page = _build_page()
    assert page.machine.state == "resolve_failed"
    groups.fetch_nip11 = lambda relay_url, opener=None, timeout=10: dict(NIP11_DOC)
    try:
        page.retry_btn.emit("clicked")
        _pump(0.4)
        assert page.machine.state == "resolving", page.machine.state
        client.my_pubkey = USER_PK
        client.emit("group-event-received", RELAY, json.dumps(_meta_event()))
        assert page.machine.state == "preview", page.machine.state
        assert not page.retry_btn.get_visible()
        print("PASS: Retry recovers: resolve_failed -> resolving -> preview")
    finally:
        groups.fetch_nip11 = _no_nip11


def check_join_timeout_reverts_ui():
    """Join with no OK/39002 inside the confirm window must revert to
    preview with the Join button live again — never stuck on
    'Join requested…' forever (FLX1: join sometimes never activated)."""
    groups.fetch_nip11 = lambda relay_url, opener=None, timeout=10: dict(NIP11_DOC)
    try:
        client = FakeClient()
        mw = _mw(client)
        toasts = []
        mw.add_toast = lambda t: toasts.append(t)
        page = groups_page.GroupDetailPage(
            mw, RELAY, "test-group", name="Test Group"
        )
        _pump()
        client.my_pubkey = USER_PK
        client.emit("group-event-received", RELAY, json.dumps(_meta_event()))
        assert page.machine.state == "preview"
        page.join_btn.emit("clicked")
        assert page.machine.state == "join_sent"
        assert page.status_lbl.get_text() == "Join requested…"
        # Fire the confirm deadline directly (no 10s wait in a test).
        assert page._on_join_timeout() is False
        assert page.machine.state == "preview"
        assert page.join_btn.get_sensitive(), "Join not re-enabled after timeout"
        assert toasts, "no failure toast on join timeout"
        print("PASS: join timeout reverts to preview + toast")
    finally:
        groups.fetch_nip11 = _no_nip11


def check_subs_queued_and_flushed_on_relay_up():
    """Sub opens that fail while the relay is down queue and flush the moment
    relay-connected fires — the FLX1 cold-open silently lost the 39000 REQ
    when the page opened before the relay finished connecting."""
    client, page = _build_page()
    assert client.subs_opened, "subs were not even attempted"
    assert page._pending_subs, "failed subs were silently dropped"
    client.sub_open_ok = True
    client.emit("relay-connected", RELAY)
    assert page._pending_subs == [], "relay-connected did not flush the queue"
    print("PASS: failed subs queued and flushed on relay-connected")


def main():
    Adw.init()
    checks = [
        check_timeline_appends_rows,
        check_activity_kinds_from_live_audit,
        check_dedup_and_bad_events_ignored,
        check_long_subtitle_row_survives,
        check_join_click_publishes_signed_join,
        check_first_search_preview_race,
        check_resolve_failure_shows_retry_state,
        check_retry_recovers_preview,
        check_join_timeout_reverts_ui,
        check_subs_queued_and_flushed_on_relay_up,
    ]
    failed = 0
    for check in checks:
        try:
            check()
        except Exception as e:
            failed += 1
            print("FAIL: %s: %s" % (check.__name__, e))
    if criticals:
        failed += 1
        print("FAIL: GTK criticals captured: %r" % criticals)
    if failed:
        return 1
    print("PASS: all %d group timeline checks green" % len(checks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
