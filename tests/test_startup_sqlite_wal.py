"""gnostr-startup-2: SQLite WAL + batched ingestion writes.

The startup relay burst (feed backfill + freshness REQ + DM poll + kind-9735
history) used to mean one INSERT+commit (fsync) per event under the single
shared DB lock, stalling the main thread's feed reads behind the write storm.

Covers:
- WAL active on a fresh DB (journal_mode returns 'wal').
- Database.save_events: batch insert is atomic and dedupes via INSERT OR
  IGNORE (inside a batch and against existing rows).
- Contention smoke: a writer thread batch-inserting while a reader queries
  completes without 'database is locked'.
- Client buffer contract: size-threshold flush, flush-before-emit invariant
  (the row is committed BEFORE event-received fires), and no per-event
  commit on the plain ingestion path.
"""

import ast
import os
import threading
import types
from unittest.mock import MagicMock

import pytest

from gi.repository import GLib  # noqa: E402 - resolves to the mock in conftest

from gnostr.database import Database

_CLIENT_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", "gnostr", "client.py"
    )
)

_client_src = open(_CLIENT_PATH, encoding="utf-8").read()


def _client_globals():
    """Exec the whole client.py source once so extracted methods resolve
    module-level names (json, threading, gnostr, EVENT_BATCH_SIZE, ...) at
    call time. gi is mocked in conftest, so GObject/GLib resolve to mocks."""
    src = _client_src
    ns = {"__name__": "gnostr.client", "__package__": "gnostr"}
    exec(compile(ast.parse(src), _CLIENT_PATH, "exec"), ns)
    # Set AFTER exec: the source's own `from gi.repository import GLib`
    # overwrites anything pre-seeded, and conftest's mock GLib never runs
    # idle callbacks. A synchronous stub stands in for the main-loop hop.
    ns["GLib"] = types.SimpleNamespace(idle_add=lambda fn, *a: fn(*a))
    return ns


_NS = _client_globals()
EVENT_BATCH_SIZE = _NS["EVENT_BATCH_SIZE"]


def _unbound(method_name):
    tree = ast.parse(_client_src)
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NostrClient"
    )
    fn = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name
    )
    exec(compile(ast.Module(body=[fn], type_ignores=[]), _CLIENT_PATH, "exec"), _NS)
    return _NS[method_name]


def _mk(ev_id, pubkey="a" * 64, created_at=100, kind=1):
    return {
        "id": ev_id,
        "pubkey": pubkey,
        "created_at": created_at,
        "kind": kind,
        "content": f"content {ev_id}",
        "tags": [],
        "sig": "s",
    }


def _patch_glib_dirs(monkeypatch, tmp_path):
    # Database resolves GLib.get_user_data_dir at construction.
    monkeypatch.setattr(GLib, "get_user_data_dir", lambda: str(tmp_path / "data"))


# --- Database: WAL + save_events ---------------------------------------------


def test_wal_active_on_fresh_db(monkeypatch, tmp_path):
    _patch_glib_dirs(monkeypatch, tmp_path)
    db = Database()
    mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    # synchronous=NORMAL is 1; busy_timeout is per-connection (ms).
    assert db.conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    assert db.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_save_events_batch_atomic_and_dedupe(monkeypatch, tmp_path):
    _patch_glib_dirs(monkeypatch, tmp_path)
    db = Database()
    db.save_event(_mk("existing"))

    dup_in_batch = _mk("dup")
    batch = [
        _mk("existing"),  # already in the table -> INSERT OR IGNORE keeps row
        dup_in_batch,
        dict(dup_in_batch),  # duplicate INSIDE the batch -> ignored
        _mk("fresh1"),
        _mk("fresh2"),
    ]
    db.save_events(batch)

    rows = db.conn.execute("SELECT id FROM events ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["dup", "existing", "fresh1", "fresh2"]
    # The pre-existing row survived the ignored duplicate untouched.
    assert db.get_event_by_id("existing")["content"] == "content existing"

    # Atomicity: a batch whose valid rows land is fully visible after the
    # single call; a malformed row is skipped without losing its siblings.
    bad = {"id": "bad"}  # missing keys
    db.save_events([_mk("after_bad"), bad, _mk("after_bad2")])
    assert db.get_event_by_id("after_bad") is not None
    assert db.get_event_by_id("after_bad2") is not None
    assert db.get_event_by_id("bad") is None

    # Empty batch is a no-op (no commit, no error).
    db.save_events([])


def test_writer_reader_contention_no_locked_error(monkeypatch, tmp_path):
    """Smoke test for the startup-hang mechanism: batched writer storm while
    the reader (main-thread feed query shape) runs. With WAL + busy_timeout
    the reader must never see 'database is locked'."""
    _patch_glib_dirs(monkeypatch, tmp_path)
    db = Database()
    owner = "o" * 64
    authors = [f"{i:064x}" for i in range(5)]
    db.save_contacts(owner, authors)
    for i, a in enumerate(authors):
        db.save_event(_mk(f"seed{i}", pubkey=a, created_at=1000 + i))

    errors = []
    stop = threading.Event()

    def writer():
        try:
            for b in range(20):
                batch = [
                    _mk(
                        f"w{b}-{j}",
                        pubkey=authors[j % len(authors)],
                        created_at=2000 + b * 50 + j,
                    )
                    for j in range(EVENT_BATCH_SIZE)
                ]
                db.save_events(batch)
        except Exception as e:  # pragma: no cover - failure path
            errors.append(("writer", e))

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(300):
            try:
                db.get_feed_following(owner, 50)
                db.get_feed_global(50)
                db.get_replies("seed0")
            except Exception as e:
                errors.append(("reader", e))
    finally:
        t.join(timeout=30)
    assert not stop.is_set()
    assert not errors, errors
    # All writer batches landed.
    count = db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert count == 5 + 20 * EVENT_BATCH_SIZE


# --- Client: buffer contract ---------------------------------------------------


class FakeClient:
    """Minimal self for NostrClient method tests (FakeClient pattern):
    unimplemented names auto-bind from client.py source."""

    def __init__(self, db=None):
        self.db = db if db is not None else MagicMock()
        self.my_pubkey = "a" * 64
        self.my_privkey = "b" * 64
        self.seen_events = set()
        self.metrics = {}
        self._event_buffer = []
        self._event_buffer_lock = threading.Lock()
        self._flush_scheduled = False
        self.emitted = []
        self.idle = []  # recorded (fn, args) when using the recording GLib

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        try:
            fn = _unbound(name)
        except StopIteration:
            raise AttributeError(name)
        bound = fn.__get__(self)
        object.__setattr__(self, name, bound)
        return bound

    def emit(self, signal, *args):
        self.emitted.append((signal, args))


class RecordingGLib:
    """GLib stub: idle_add records callbacks without running them, so tests
    can observe pure buffering behaviour between main-loop hops."""

    def __init__(self):
        self.pending = []

    def idle_add(self, fn, *a):
        self.pending.append((fn, a))
        return 1

    def run(self):
        while self.pending:
            fn, a = self.pending.pop(0)
            fn(*a)


def test_buffer_flushes_at_size_threshold():
    glib = RecordingGLib()
    _NS["GLib"] = glib
    try:
        c = FakeClient()
        for i in range(EVENT_BATCH_SIZE - 1):
            c._buffer_event(_mk(f"e{i}"))
        c.db.save_events.assert_not_called()
        # One coalesced idle was scheduled for the whole quiet-window burst.
        assert len(glib.pending) == 1
        c._buffer_event(_mk(f"e{EVENT_BATCH_SIZE - 1}"))
        # Threshold hit: synchronous flush with the WHOLE batch, one call.
        c.db.save_events.assert_called_once()
        assert len(c.db.save_events.call_args[0][0]) == EVENT_BATCH_SIZE
        assert c._event_buffer == []
    finally:
        _NS["GLib"] = types.SimpleNamespace(idle_add=lambda fn, *a: fn(*a))


def test_flush_event_buffer_empty_is_noop():
    c = FakeClient()
    assert c._flush_event_buffer() is False
    c.db.save_events.assert_not_called()


def test_flush_and_emit_commits_before_emit():
    calls = []
    c = FakeClient()
    c.db.save_events = lambda events: calls.append(("save_events", len(events)))
    c.emit = lambda signal, *a: calls.append(("emit", signal))
    c._event_buffer.append(_mk("x"))
    assert c._flush_and_emit("event-received", "x", "a" * 64, "hi", "[]") is False
    assert calls == [("save_events", 1), ("emit", "event-received")]


def test_handle_event_kind_1_row_committed_before_signal(monkeypatch, tmp_path):
    """The load-bearing invariant: main.on_event_received hydrates the row
    via get_event_by_id — with the batched path the row MUST already be
    committed when the signal fires (synchronous idle stub = main loop hop)."""
    _patch_glib_dirs(monkeypatch, tmp_path)
    db = Database()
    c = FakeClient(db=db)
    seen_row = {}

    def emit(signal, *args):
        c.emitted.append((signal, args))
        if signal == "event-received":
            seen_row["row"] = db.get_event_by_id(args[0])

    c.emit = emit
    ev = _mk("live1", created_at=123)
    _unbound("_handle_event")(c, ev)

    assert ("event-received", ("live1", "a" * 64, "content live1", "[]")) in c.emitted
    assert seen_row["row"] is not None, "row must be committed before emit"
    assert seen_row["row"]["created_at"] == 123


def test_handle_event_plain_path_has_no_per_event_commit(monkeypatch, tmp_path):
    """Acceptance: no per-event commit on the plain ingestion path —
    _handle_event must route through save_events (batched), never the
    single-event save_event."""
    _patch_glib_dirs(monkeypatch, tmp_path)

    class CountingDatabase(Database):
        def __init__(self):
            super().__init__()
            self.single_calls = 0
            self.batch_calls = 0

        def save_event(self, event):
            self.single_calls += 1
            super().save_event(event)

        def save_events(self, events):
            self.batch_calls += 1
            super().save_events(events)

    db = CountingDatabase()
    c = FakeClient(db=db)
    handle = _unbound("_handle_event")
    for i in range(10):
        handle(c, _mk(f"b{i}", created_at=500 + i))
    assert db.single_calls == 0
    assert db.batch_calls >= 1
    assert db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 10
