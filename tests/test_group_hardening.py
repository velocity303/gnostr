"""NIPW-P3-HARDEN: group workflow reliability contracts (no GTK, no network).

Every async step in the group detail flow gets a deadline, a retry, or an
explicit failure — no silent stalls (FLX1: feeds sometimes loaded, sometimes
not; description sometimes missing; join sometimes stuck on 'Join requested').

Contracts:
- groups.fetch_nip11: retries transient failures (timeout + 2 retries),
  caches successes per relay (TTL), never caches failures, force bypasses.
- GroupJoinMachine: NIP-11 fetch failure is a RETRYABLE resolve_failed state
  (not the old terminal 'aborted: relay does not support groups'); Retry
  round-trips resolving -> resolve_failed -> resolving; a join with no OK
  reverts join_sent -> preview.
- client.is_group_sub_id / sub_closed_reopenable: CLOSED-reason triage —
  idle/timeout sweeps reopen, policy refusals (rate-limit) never do (issue
  #26 ban-spiral rule).
- NostrRelay.handle_sub_closed (extracted, fake self): live group sub closed
  with 'closed: timeout' is re-REQ'd with the same filter, bounded; refused
  and snapshot subs are not.
- GroupDetailPage steps (extracted, fake selves): resolve deadline fires the
  failure state exactly once; step-ok diag logs once and cancels the
  deadline; failed sub opens queue for relay-connected; join timeout reverts.
- Structured diagnostics: every [GROUP] step line matches the device-log
  contract `step=<name> relay=<url> result=ok|fail`.
"""

import ast
import json
import os
import time
import types

import pytest

import gnostr.groups as groups
from gnostr.groups import GroupJoinMachine

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src", "gnostr"))
_CLIENT_PATH = os.path.join(_SRC, "client.py")
_PAGE_PATH = os.path.join(_SRC, "ui", "groups_page.py")


def _extract(path, target, method_name, extra_ns=None):
    """Extract a real function from source (class method or module-level
    function) without importing the module under the mocked GObject."""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == target:
            fn = next(
                (
                    n
                    for n in node.body
                    if isinstance(n, ast.FunctionDef) and n.name == method_name
                ),
                None,
            )
            break
        if isinstance(node, ast.FunctionDef) and node.name == method_name:
            fn = node
            break
    if fn is None:
        raise LookupError(f"{target}.{method_name} not found in {path}")
    ns = {"json": json, "time": time}
    if extra_ns:
        ns.update(extra_ns)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), path, "exec"), ns)
    return ns[method_name]


def _diag_ns():
    """Namespace fragment + captured [GROUP] log lines for extracted code."""
    lines = []

    def _diag(step, result, relay="", ms=None):
        ms_txt = f" ms={int(ms)}" if ms is not None else ""
        lines.append(f"[GROUP] step={step} relay={relay} result={result}{ms_txt}")

    return {"_diag": _diag, "time": time}, lines


# --- fetch_nip11: retries + TTL cache ---------------------------------------


class _Opener:
    """urllib-opener stub: yields the queued results in order (Exception
    entries raise); once the queue drains, `results_keep` repeats forever."""

    def __init__(self, results, results_keep=None):
        self.results = list(results)
        self.results_keep = results_keep
        self.calls = 0

    def open(self, req, timeout=None):
        self.calls += 1
        r = self.results.pop(0) if self.results else self.results_keep
        if isinstance(r, Exception):
            raise r

        class _R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(r).encode("utf-8")

        return _R()


@pytest.fixture(autouse=True)
def _nip11_cache_clear():
    groups.nip11_cache_clear()
    yield
    groups.nip11_cache_clear()


def test_fetch_nip11_retries_transient_failures():
    opener = _Opener(
        [OSError("boom"), OSError("boom"), {"supported_nips": [29]}]
    )
    doc = groups.fetch_nip11("wss://relay.example", opener=opener, sleep=lambda s: None)
    assert doc == {"supported_nips": [29]}
    assert opener.calls == 3  # two failures absorbed, third succeeded


def test_fetch_nip11_gives_up_after_attempts_returns_none():
    opener = _Opener([], OSError("down"))
    doc = groups.fetch_nip11(
        "wss://relay.example", opener=opener, attempts=3, sleep=lambda s: None
    )
    assert doc is None
    assert opener.calls == 3


def test_fetch_nip11_caches_success_and_skips_refetch():
    opener = _Opener([{"supported_nips": [29]}], OSError("must not be called"))
    assert groups.fetch_nip11("wss://relay.example", opener=opener) is not None
    assert groups.fetch_nip11("wss://relay.example", opener=opener) is not None
    assert opener.calls == 1  # TTL cache hit — no second network call


def test_fetch_nip11_force_bypasses_cache():
    opener = _Opener([{"a": 1}, {"a": 2}])
    assert groups.fetch_nip11("wss://relay.example", opener=opener)["a"] == 1
    assert groups.fetch_nip11("wss://relay.example", opener=opener, force=True)["a"] == 2


def test_fetch_nip11_failures_never_cached():
    bad = _Opener([], OSError("down"))
    assert (
        groups.fetch_nip11("wss://relay.example", opener=bad, sleep=lambda s: None)
        is None
    )
    good = _Opener([{"supported_nips": [29]}])
    assert groups.fetch_nip11("wss://relay.example", opener=good) is not None


# --- GroupJoinMachine: resolve_failed + join timeout --------------------------


def _machine():
    return GroupJoinMachine("g1", "wss://relay.example", "a" * 64)


def _meta():
    return groups.GroupMeta("wss://relay.example", "g1", name="X")


def test_nip11_fetch_failure_is_retryable_not_abort():
    m = _machine()
    m.begin()
    assert m.on_nip11(None) == "resolve_failed"
    assert m.state == "resolve_failed"
    assert m.abort_reason is None  # NOT the old terminal abort
    assert "NIP-11" in m.resolve_reason


def test_nip11_no_groups_support_stays_terminal_abort():
    m = _machine()
    m.begin()
    assert m.on_nip11({"supported_nips": [1, 9]}) == "aborted"


def test_resolve_failed_retry_roundtrip():
    m = _machine()
    m.begin()
    m.on_nip11(None)
    assert m.retry_resolve() == "resolving"
    assert m.resolve_reason is None
    # a recovered fetch proceeds normally
    assert m.on_nip11({"supported_nips": [29]}) == "resolving"


def test_metadata_arriving_after_resolve_failed_recovers():
    m = _machine()
    m.begin()
    m.on_nip11(None)
    assert m.on_metadata(_meta()) == "preview"


def test_join_timeout_reverts_to_preview():
    m = _machine()
    m.begin()
    m.on_nip11({"supported_nips": [29]})
    m.on_metadata(_meta())
    m.on_join("e" * 64)
    assert m.state == "join_sent"
    assert m.on_join_timeout() == "preview"
    assert m.join_event_id is None
    assert "timeout" in m.rejection_reason
    # idempotent from non-join_sent states
    assert m.on_join_timeout() == "preview"


def test_resolve_failed_in_join_states():
    assert "resolve_failed" in groups.JOIN_STATES


# --- client CLOSED triage helpers ----------------------------------------------


def _clamp():
    return _extract(_CLIENT_PATH, None, "nip01_sub_id")


def _is_group_sub():
    return _extract(_CLIENT_PATH, None, "is_group_sub_id")


def _reopenable():
    return _extract(_CLIENT_PATH, None, "sub_closed_reopenable")


def test_is_group_sub_id():
    f = _is_group_sub()
    for sid in ("gmeta_abc", "gmemset_abc", "gtl_abc", "gmem_abc", "glist_x"):
        assert f(sid), sid
    for sid in ("sub_global", "dm_sub", "thread_abc", "", None, 7):
        assert not f(sid), sid


def test_sub_closed_reopenable_triage():
    f = _reopenable()
    assert f("closed: timeout")
    assert f("Closed: Timeout")
    assert f("idle timeout")
    assert not f("rate-limited: slow down")
    assert not f("payment required")
    assert not f("")
    assert not f(None)


# --- NostrRelay.handle_sub_closed (extracted, fake self) ----------------------


def _handle_closed():
    ns = {
        "nip01_sub_id": _clamp(),
        "is_group_sub_id": _is_group_sub(),
        "sub_closed_reopenable": _reopenable(),
    }
    return _extract(_CLIENT_PATH, "NostrRelay", "handle_sub_closed", ns)


def _relay_self():
    return types.SimpleNamespace(
        url="wss://relay.example",
        active_sub_ids=set(),
        snapshot_ids=set(),
        live_subs={},
        request_queue=[],
        queued_sub_ids=set(),
        _closed_notified=False,
        _reopen_counts={},
        enqueued=[],
        _enqueue=lambda sid, filt: None,  # replaced per test
    )


def test_closed_timeout_reopens_live_group_sub():
    h = _handle_closed()
    r = _relay_self()
    filt = [{"kinds": [30023], "#a": ["wss://relay.example:xyz"]}]
    r.live_subs["gtl_xyz"] = filt
    r.active_sub_ids.add("gtl_xyz")
    r._enqueue = lambda sid, f: r.enqueued.append((sid, f))
    assert h(r, "gtl_xyz", "closed: timeout") is True
    assert r.enqueued == [("gtl_xyz", filt)]  # SAME filter re-REQ'd
    assert r._reopen_counts["gtl_xyz"] == 1


def test_closed_policy_refusal_never_reopens():
    h = _handle_closed()
    r = _relay_self()
    r.live_subs["gtl_xyz"] = [{}]
    r._enqueue = lambda sid, f: r.enqueued.append((sid, f))
    assert h(r, "gtl_xyz", "rate-limited: slow down") is False
    assert r.enqueued == []
    assert r._closed_notified is True


def test_closed_snapshot_sub_not_reopened():
    h = _handle_closed()
    r = _relay_self()
    r.snapshot_ids.add("gmeta_xyz")  # not in live_subs
    r._enqueue = lambda sid, f: r.enqueued.append((sid, f))
    assert h(r, "gmeta_xyz", "closed: timeout") is False
    assert r.enqueued == []


def test_closed_reopen_bounded():
    h = _handle_closed()
    r = _relay_self()
    r._enqueue = lambda sid, f: r.enqueued.append((sid, f))
    for _i in range(7):
        r.live_subs["gtl_xyz"] = [{}]
        h(r, "gtl_xyz", "closed: timeout")
    assert len(r.enqueued) == 5  # bounded: stop hammering a sweeping relay


def test_closed_logs_group_diag(capsys):
    h = _handle_closed()
    r = _relay_self()
    r.live_subs["gtl_xyz"] = [{}]
    r._enqueue = lambda sid, f: None
    h(r, "gtl_xyz", "closed: timeout")
    out = capsys.readouterr().out
    assert "[GROUP] step=sub_close" in out
    assert "[GROUP] step=sub_reopen" in out
    assert "relay=wss://relay.example" in out


# --- GroupDetailPage extracted steps -------------------------------------------


class _GLibStub:
    def __init__(self):
        self.removed = []

    def source_remove(self, tid):
        self.removed.append(tid)
        return True


def _page_self(machine=None):
    return types.SimpleNamespace(
        relay="wss://relay.example",
        machine=machine or _machine(),
        client=None,
        _sub_ids=[],
        _pending_subs=[],
        _step_t0={},
        _step_logged=set(),
        _resolve_deadline_id=None,
        _join_timeout_id=None,
        _leave_timeout_id=None,
        _shown=[],
        _show_resolve_failed=lambda: None,
        _render_status=lambda: None,
        main_window=types.SimpleNamespace(add_toast=lambda t: None),
    )


def test_resolve_deadline_fires_once_from_resolving():
    ns, lines = _diag_ns()
    fn = _extract(_PAGE_PATH, "GroupDetailPage", "_on_resolve_deadline", ns)
    page = _page_self()
    shown = []
    page._show_resolve_failed = lambda: shown.append("failed")
    page.machine.state = "resolving"
    fn(page)
    assert page.machine.state == "resolve_failed"
    assert shown == ["failed"]
    assert any("step=sub_meta" in ln and "result=fail" in ln for ln in lines)
    # from preview it is a no-op
    page.machine.state = "preview"
    fn(page)
    assert page.machine.state == "preview"
    assert len(shown) == 1


def test_mark_step_ok_logs_once_and_cancels_deadline():
    glib = _GLibStub()
    ns, lines = _diag_ns()
    ns["GLib"] = glib
    fn = _extract(_PAGE_PATH, "GroupDetailPage", "_mark_step_ok", ns)
    page = _page_self()
    page._resolve_deadline_id = 42
    page._step_t0["sub_meta"] = time.monotonic() - 0.5
    fn(page, "sub_meta")
    fn(page, "sub_meta")  # second arrival: silent
    meta_lines = [ln for ln in lines if "step=sub_meta" in ln]
    assert len(meta_lines) == 1
    assert "result=ok" in meta_lines[0] and "ms=" in meta_lines[0]
    assert glib.removed == [42]
    assert page._resolve_deadline_id is None


def test_failed_sub_open_queues_for_relay_up():
    ns, lines = _diag_ns()
    ns["_SUB_STEPS"] = {"gtl": "sub_timeline"}
    fn = _extract(_PAGE_PATH, "GroupDetailPage", "_open_sub", ns)
    page = _page_self()
    page.client = types.SimpleNamespace(subscribe_on_relay=lambda *a, **k: False)
    filt = [{"kinds": [1]}]
    fn(page, "gtl_g1", filt, False)
    assert page._pending_subs == [("gtl_g1", filt, False)]
    assert any("step=sub_timeline" in ln and "result=fail" in ln for ln in lines)
    # success path records the sub + timing anchor
    page.client = types.SimpleNamespace(subscribe_on_relay=lambda *a, **k: True)
    fn(page, "gtl_g1", filt, False)
    assert page._sub_ids == ["gtl_g1"]
    assert "sub_timeline" in page._step_t0


def test_relay_connected_flushes_pending_subs():
    fn = _extract(_PAGE_PATH, "GroupDetailPage", "_on_relay_connected")
    page = _page_self()
    opened = []
    page._open_sub = lambda sid, f, s: opened.append((sid, f, s))
    page._pending_subs = [("gtl_g1", [{}], False)]
    fn(page, None, "wss://other.relay")  # wrong relay: untouched
    assert opened == []
    fn(page, None, "wss://relay.example")
    assert opened == [("gtl_g1", [{}], False)]
    assert page._pending_subs == []


def test_join_timeout_reverts_and_toasts():
    ns, lines = _diag_ns()
    ns["Adw"] = types.SimpleNamespace(Toast=lambda title=None: title)
    fn = _extract(_PAGE_PATH, "GroupDetailPage", "_on_join_timeout", ns)
    m = _machine()
    m.begin()
    m.on_nip11({"supported_nips": [29]})
    m.on_metadata(_meta())
    m.on_join("e" * 64)
    page = _page_self(m)
    toasts = []
    page.main_window = types.SimpleNamespace(add_toast=lambda t: toasts.append(t))
    assert fn(page) is False
    assert page.machine.state == "preview"
    assert toasts  # loud failure, not a silent stall
    assert any("step=join_ok" in ln and "result=fail" in ln for ln in lines)
    # already-settled state: silent no-op
    page.machine.state = "member"
    assert fn(page) is False
    assert len(toasts) == 1
