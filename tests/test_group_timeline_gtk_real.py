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
groups.fetch_nip11 = lambda relay_url, opener=None, timeout=10: None

RELAY = "wss://relay.invalid"
SELF_PK = "a" * 64
AUTHOR_PK = "b" * 64


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
    }

    def __init__(self):
        super().__init__()
        self.my_pubkey = SELF_PK

    def subscribe_on_relay(self, *args, **kwargs):
        return False

    def dispatch_signed_to_relay(self, *args, **kwargs):
        return False


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
    mw = types.SimpleNamespace(client=client, pub_key=SELF_PK, content_nav=None)
    page = groups_page.GroupDetailPage(mw, RELAY, "test-group", name="Test Group")
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
    Adw.ActionRow per event in the timeline (the FLX1 bug appended zero)."""
    client, page = _build_page()
    client.emit("group-event-received", RELAY, json.dumps(_ev("a1", 1)))
    client.emit("group-event-received", RELAY, json.dumps(_ev("a2", 7, "+")))
    client.emit("group-event-received", RELAY, json.dumps(_ev("a3", 9, "")))
    rows = _timeline_rows(page)
    assert len(rows) == 3, "expected 3 timeline rows, got %d" % len(rows)
    assert all(isinstance(r, Adw.ActionRow) for r in rows)
    assert rows[0].get_title() == _short(AUTHOR_PK)
    assert rows[0].get_subtitle() == "hello group"
    assert rows[1].get_subtitle() == "❤ reacted"
    assert rows[2].get_subtitle() == "⚡ zapped"
    # The 4-line cap the old (crashing) subtitle-label calls intended.
    assert rows[0].get_subtitle_lines() == 4, "subtitle line cap not applied"
    print("PASS: kind-1/7/9 emissions append 3 rows with 4-line subtitles")


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


def main():
    Adw.init()
    checks = [
        check_timeline_appends_rows,
        check_dedup_and_bad_events_ignored,
        check_long_subtitle_row_survives,
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
