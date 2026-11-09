"""Zap receipt flood lockup regression (FuriPhone 2026-11-08).

The 9735 branch in _handle_event spawned ONE THREAD PER RECEIPT with no
dedupe (the branch returns before seen_events.add, so relays re-sending
the receipt window on every reconnect reprocessed the whole backlog).
Each thread ran pure-Python schnorr verify + bolt11 decode (GIL-bound)
plus an uncached LNURL metadata fetch with a 10s timeout — unreachable
servers meant every spam receipt paid the full timeout. Dozens of such
threads saturated the GIL: the UI froze for as long as the flood lasted,
then spontaneously recovered.

Fix: single worker thread + event-id dedupe + 15-min negative metadata
cache + rate-limited rejection logging.

House pattern (client.py defines GObject subclasses that don't evaluate
against the conftest mocks): methods are extracted via ast and bound to
a fake self.
"""

import ast
import collections
import queue
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import gnostr.zaps.lnurl as lnurl_mod

CLIENT_PY = Path(__file__).resolve().parent.parent / "src" / "gnostr" / "client.py"


def _method(name):
    tree = ast.parse(CLIENT_PY.read_text(encoding="utf-8"))

    def find(node):
        for n in ast.walk(node):
            if isinstance(n, ast.FunctionDef) and n.name == name:
                return n
        return None

    fn = find(tree)
    assert fn is not None, f"{name} not found in client.py"
    ns = {
        "collections": collections,
        "queue": queue,
        "threading": threading,
        "time": time,
        "GLib": MagicMock(),
        "__package__": "gnostr",
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(CLIENT_PY), "exec"), ns)
    return ns[name]


class FakeClient:
    def __init__(self):
        self._zap_queue = queue.Queue()
        self._zap_worker = None
        self._zap_worker_lock = threading.Lock()
        self._zap_seen = collections.deque(maxlen=4096)
        self._zap_seen_set = set()
        self._zap_meta_negative = {}
        self._zap_reject_logged = {}
        self.db = MagicMock()
        self.processed = []
        self._zap_metadata_lookup = lambda pk: None

    def _process_zap_receipt(self, ev):
        self.processed.append(ev)


# ---------------------------------------------------------------------------
# enqueue: dedupe, single worker, bounded memory
# ---------------------------------------------------------------------------


def test_enqueue_dedupes_by_event_id():
    fc = FakeClient()
    fc._zap_worker_loop = lambda: None  # don't actually spin a worker
    enqueue = _method("_enqueue_zap_receipt")
    enqueue(fc, {"id": "a" * 64})
    enqueue(fc, {"id": "a" * 64})
    enqueue(fc, {"id": "b" * 64})
    assert fc._zap_queue.qsize() == 2, "re-delivered receipts must not requeue"


def test_enqueue_skips_idless_and_starts_worker_once():
    fc = FakeClient()
    started = []

    def fake_loop():
        started.append(1)

    fc._zap_worker_loop = fake_loop
    enqueue = _method("_enqueue_zap_receipt")
    enqueue(fc, {})  # no id -> dropped
    assert fc._zap_queue.qsize() == 0 and fc._zap_worker is None
    enqueue(fc, {"id": "c" * 64})
    enqueue(fc, {"id": "d" * 64})
    time.sleep(0.05)
    assert len(started) == 1, "exactly one worker for the whole stream"


def test_enqueue_dedupe_set_is_bounded():
    fc = FakeClient()
    fc._zap_worker_loop = lambda: None
    fc._zap_seen = collections.deque(maxlen=4)
    enqueue = _method("_enqueue_zap_receipt")
    for i in range(10):
        enqueue(fc, {"id": f"{i:064x}"})
    assert len(fc._zap_seen) == 4
    assert len(fc._zap_seen_set) == 4, "evicted ids must leave the lookup set"


def test_worker_loop_survives_bad_receipts():
    fc = FakeClient()
    boom = {"id": "x" * 64}

    def proc(ev):
        if ev is boom:
            raise RuntimeError("bad receipt")
        fc.processed.append(ev)

    fc._process_zap_receipt = proc
    good = {"id": "y" * 64}
    fc._zap_queue.put(boom)
    fc._zap_queue.put(good)
    loop = _method("_zap_worker_loop")

    def run():
        loop(fc)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    for _ in range(100):
        if fc.processed:
            break
        time.sleep(0.01)
    assert fc.processed == [good], "worker must keep draining after a crash"


# ---------------------------------------------------------------------------
# negative metadata cache: one 10s timeout per bad recipient, not per receipt
# ---------------------------------------------------------------------------


def test_metadata_negative_cache_blocks_refetch(monkeypatch):
    calls = []

    def boom(lnurl, *a, **k):
        calls.append(lnurl)
        raise RuntimeError("LNURL server unreachable")

    monkeypatch.setattr(lnurl_mod, "resolve_target", lambda prof: "LNURL1ABC")
    monkeypatch.setattr(lnurl_mod, "fetch_metadata", boom)

    fc = FakeClient()
    fc.db.get_profile.return_value = {"lnurl": "LNURL1ABC"}
    lookup = _method("_zap_metadata_lookup")

    assert lookup(fc, "aa") is None
    assert lookup(fc, "aa") is None
    assert len(calls) == 1, "negative result must be cached (no 10s re-timeout)"

    # expiry lets the next attempt through
    fc._zap_meta_negative["aa"] = time.monotonic() - 1
    assert lookup(fc, "aa") is None
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# rejection logging: one line per reason per minute, not one per receipt
# ---------------------------------------------------------------------------


def test_rejection_logging_rate_limited(monkeypatch, capsys):
    import json as _json

    monkeypatch.setattr(
        "gnostr.zaps.receipt.process_9735",
        lambda ev, db, metadata_lookup=None: (None, "rule 1: spam reason"),
    )
    fc = FakeClient()
    fc.processed = None
    proc = _method("_process_zap_receipt")
    for _ in range(50):
        proc(fc, {"id": "z" * 64})
    out = capsys.readouterr().out
    assert out.count("spam reason") == 1, (
        f"50 identical rejections printed {out.count('spam reason')} lines"
    )
