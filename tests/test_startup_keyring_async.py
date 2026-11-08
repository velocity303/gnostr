"""Startup-hang fix: no synchronous libsecret lookup may run on the GTK main
thread at startup.

Secret.password_lookup_sync blocks the caller until the D-Bus secret service
answers — multi-second on cold boot, INDEFINITELY on a locked keyring (the
unlock prompt waits on the user), worse through the Flatpak portal. The old
MainWindow.__init__ called KeyManager.load_key() (sync) and then
_try_restore_bunker_session() (a second sync lookup) inline, which is the
primary suspect for "app frequently hangs on initial startup".

Under the conftest gi mocks `class MainWindow(Adw.ApplicationWindow)`
evaluates against a Mock base, so the imported class is a Mock (same
situation as test_dm_ui_wiring.py) — logic tests extract the real methods
from source via ast and bind them to a lightweight fake self.

Guards:
- AST: MainWindow.__init__ performs no Secret sync-API call and no
  KeyManager.load_key()/BunkerSessionManager.load_session(); it schedules
  load_key_async instead.
- AST: _try_restore_bunker_session's load_session() call lives INSIDE the
  worker function, not at method top level.
- Behaviour: load_key_async (real module function — key_manager.py defines
  no GTK classes, so it imports for real) runs the lookup on a worker
  thread, returns immediately even when the lookup blocks, and hands the
  result back through GLib.idle_add (house pattern).
- Behaviour: MainWindow._on_startup_key auto-logs-in on a saved key, falls
  back to bunker restore without one, and is inert once a manual login
  superseded it (double-activation guard).
- Behaviour: _try_restore_bunker_session reads the secret on the worker
  thread, never on the caller's thread.
"""

import ast
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import gnostr.key_manager as km
import gnostr.signer as signer_mod
from gnostr.key_manager import BunkerSessionManager, load_key_async

MAIN_PY = Path(__file__).resolve().parent.parent / "src" / "gnostr" / "main.py"

# Any Secret call matching these names blocks the main loop until the keyring
# daemon (or an unlock prompt) answers. None of them may appear in the
# startup path.
BLOCKING_SECRET_CALLS = {
    "password_lookup_sync",
    "password_store_sync",
    "password_clear_sync",
    "password_lookup",
}


def _tree():
    return ast.parse(MAIN_PY.read_text(encoding="utf-8"))


def _method(name):
    for node in _tree().body:
        if isinstance(node, ast.ClassDef) and node.name == "MainWindow":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return item
    raise AssertionError(f"MainWindow.{name} not found in main.py")


def _call_names(node):
    """Called names (bare and attribute) of every Call inside node."""
    names = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Attribute):
                names.add(n.func.attr)
            elif isinstance(n.func, ast.Name):
                names.add(n.func.id)
    return names


def _toplevel_call_names(fn):
    """Call names in a function body EXCLUDING nested function defs."""
    names = set()
    for stmt in fn.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names |= _call_names(stmt)
    return names


def _main_method(name, extra_ns=None):
    """Extract the real MainWindow.<name> function from source (house
    pattern, cf. test_dm_ui_wiring._class_method). __package__ lets the
    method's relative imports resolve against the real gnostr package."""
    fn = _method(name)
    ns = {"__package__": "gnostr", "GLib": Mock()}
    if extra_ns:
        ns.update(extra_ns)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(MAIN_PY), "exec"), ns)
    return ns[name], ns


# --------------------------------------------------------------------- AST


def test_init_performs_no_sync_secret_lookup():
    init = _method("__init__")
    calls = _call_names(init)
    offenders = calls & (BLOCKING_SECRET_CALLS | {"load_key", "load_session"})
    assert not offenders, (
        f"MainWindow.__init__ calls blocking keyring API {sorted(offenders)} "
        "on the main thread — schedule the lookup with load_key_async instead."
    )
    assert "load_key_async" in calls, (
        "MainWindow.__init__ must schedule the keyring lookup via "
        "load_key_async (startup-hang fix)."
    )


def test_bunker_restore_reads_session_inside_worker():
    meth = _method("_try_restore_bunker_session")
    workers = [
        n
        for n in ast.walk(meth)
        if isinstance(n, ast.FunctionDef) and n.name == "_worker"
    ]
    assert workers, "_try_restore_bunker_session lost its _worker thread"
    assert "load_session" in _call_names(workers[0]), (
        "BunkerSessionManager.load_session() must run inside the worker "
        "thread (startup-hang fix deliverable 3)."
    )
    assert "load_session" not in _toplevel_call_names(meth), (
        "BunkerSessionManager.load_session() must not be called at the top "
        "level of _try_restore_bunker_session — that is the main thread."
    )


# -------------------------------------------------------------- load_key_async


def test_load_key_async_runs_lookup_off_main_thread(monkeypatch):
    main_ident = threading.get_ident()
    lookups = []

    def fake_lookup(schema, attrs, cancellable):
        lookups.append(threading.get_ident())
        return "nsec-test"

    dispatched = []
    monkeypatch.setattr(km.Secret, "password_lookup_sync", fake_lookup, raising=False)
    monkeypatch.setattr(
        km.GLib,
        "idle_add",
        lambda fn, *a: dispatched.append((fn, a)) or False,
        raising=False,
    )

    received = []
    t = load_key_async(received.append)
    t.join(5)
    assert not t.is_alive(), "worker thread did not finish"
    assert (
        len(lookups) == 1 and lookups[0] != main_ident
    ), "the sync lookup must run on a worker thread, not the caller's"
    assert len(dispatched) == 1, "result must be dispatched via GLib.idle_add"
    fn, args = dispatched[0]
    fn(*args)  # simulate the main loop delivering the idle callback
    assert received == ["nsec-test"]


def test_load_key_async_returns_while_lookup_blocks(monkeypatch):
    """The constructor property: a slow (or indefinitely blocked) keyring must
    never delay the caller — load_key_async returns immediately."""

    def slow_lookup(schema, attrs, cancellable):
        time.sleep(0.4)
        return None

    monkeypatch.setattr(km.Secret, "password_lookup_sync", slow_lookup, raising=False)
    monkeypatch.setattr(km.GLib, "idle_add", lambda fn, *a: False, raising=False)

    t0 = time.monotonic()
    t = load_key_async(lambda nsec: None)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.2, f"load_key_async blocked the caller {elapsed:.2f}s"
    t.join(5)


# ------------------------------------------------------- _on_startup_key flow


class FakeWindow:
    """Minimal self for the extracted MainWindow methods."""

    def __init__(self):
        self.pub_key = None
        self.priv_key = None
        self._startup_login_superseded = False
        self.logins = []
        self.bunker_attempts = 0
        self.confirm_sign_request = lambda *a, **k: None

    def perform_login(self, priv_hex):
        self.logins.append(priv_hex)

    def perform_bunker_login(self, backend):
        self.bunker_logins = getattr(self, "bunker_logins", [])
        self.bunker_logins.append(backend)

    def _try_restore_bunker_session(self):
        self.bunker_attempts += 1


class CapturingGLib:
    def __init__(self):
        self.scheduled = []

    def idle_add(self, fn, *args):
        self.scheduled.append((fn, args))
        return False


def test_startup_key_logs_in_and_schedules_feed():
    glib = CapturingGLib()
    on_startup_key, _ = _main_method("_on_startup_key", {"GLib": glib})
    w = FakeWindow()
    on_startup_key(w, "nsec-saved")
    assert w.logins == ["nsec-saved"]
    assert w.bunker_attempts == 0
    assert len(glib.scheduled) == 1, "feed switch must be scheduled after login"


def test_startup_key_absent_falls_back_to_bunker_restore():
    glib = CapturingGLib()
    on_startup_key, _ = _main_method("_on_startup_key", {"GLib": glib})
    w = FakeWindow()
    on_startup_key(w, None)
    assert w.logins == []
    assert w.bunker_attempts == 1
    assert glib.scheduled == []


def test_late_callback_never_clobbers_manual_login():
    """Double-activation guard: user clicked Login while the lookup was in
    flight — the late callback must not perform_login over it."""
    on_startup_key, _ = _main_method("_on_startup_key", {"GLib": CapturingGLib()})
    w = FakeWindow()
    w._startup_login_superseded = True
    on_startup_key(w, "nsec-saved")
    assert w.logins == []
    assert w.bunker_attempts == 0

    # A completed manual login (keys already loaded) blocks it too.
    w2 = FakeWindow()
    w2.pub_key = "abcd"
    on_startup_key(w2, "nsec-saved")
    assert w2.logins == []


def test_manual_entry_points_arm_the_guard():
    """Every manual login entry point must set the supersede flag BEFORE the
    async callback can land (source-level: the assignment is in the handler)."""
    for name in (
        "on_login_clicked",
        "on_create_account_clicked",
        "on_connect_signer_clicked",
        "on_derive_key_clicked",
    ):
        src = ast.unparse(_method(name))
        assert "_startup_login_superseded = True" in src, (
            f"{name} must set _startup_login_superseded so the late startup "
            "callback cannot clobber a manual login."
        )


# ------------------------------------------------ _try_restore_bunker_session


_RealThread = threading.Thread


class _JoiningThread:
    """threading.Thread stand-in that runs the target on a real thread and
    joins it, so the test can observe the worker's effects. Uses the
    pre-captured _RealThread — threading.Thread is patched during the test."""

    def __init__(self, target=None, daemon=None, **kw):
        self._target = target

    def start(self):
        t = _RealThread(target=self._target, daemon=True)
        t.start()
        t.join(5)


def test_bunker_restore_reads_secret_on_worker_thread(monkeypatch):
    main_ident = threading.get_ident()
    read_idents = []
    monkeypatch.setattr(
        BunkerSessionManager,
        "load_session",
        staticmethod(lambda: read_idents.append(threading.get_ident()) or None),
    )
    monkeypatch.setattr(threading, "Thread", _JoiningThread)

    restore, _ = _main_method("_try_restore_bunker_session", {"GLib": CapturingGLib()})
    w = FakeWindow()
    restore(w)
    assert read_idents, "load_session was never called"
    assert (
        read_idents[0] != main_ident
    ), "the bunker session secret read must run on the worker thread"


def test_bunker_restore_reconnects_and_dispatches_login(monkeypatch):
    monkeypatch.setattr(
        BunkerSessionManager, "load_session", staticmethod(lambda: {"uri": "x"})
    )
    monkeypatch.setattr(threading, "Thread", _JoiningThread)
    reconnects = []
    sentinel = object()

    def fake_reconnect(client, session, on_sign_request=None, timeout=None):
        reconnects.append(session)
        return sentinel

    monkeypatch.setattr(signer_mod, "reconnect_from_session", fake_reconnect)
    glib = CapturingGLib()
    restore, _ = _main_method("_try_restore_bunker_session", {"GLib": glib})

    w = FakeWindow()
    w.client = object()
    restore(w)
    assert reconnects == [{"uri": "x"}]
    assert glib.scheduled and glib.scheduled[0][1] == (
        sentinel,
    ), "perform_bunker_login must be dispatched with the backend via idle_add"
