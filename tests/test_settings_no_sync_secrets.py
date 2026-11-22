"""Settings-open freeze regression (FuriPhone 2026-11-08).

Opening Settings froze the whole app: _build_wallet_group ran
NWCUriManager.load_uri() — a synchronous libsecret D-Bus call — on the
GTK main thread. Same class of bug as the startup hang fixed in PR 51,
just in the settings path. The phone's keyring is slow enough to make
the freeze user-visible; a desktop keyring answers fast enough to hide
it, which is why CI never caught it.

Tier-1 AST guards (house pattern for dialogs.py — the gi mocks make the
window unconstructable):
- no sync secret/KDF call sits in a UI-callback body; each must be inside
  a nested worker function or passed to run_off_main
- the Received-zaps clear loop no longer removes the group's internal
  GtkBox (Adw CRITICAL "tried to remove non-child")
- AdwPreferencesGroup titles are Pango markup: no bare '&'

Plus a functional test of run_off_main itself under a stub gi.
"""

import ast
import os
import threading

_SRC = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)
_DIALOGS = os.path.join(_SRC, "gnostr", "dialogs.py")
_ZAPS_UI = os.path.join(_SRC, "gnostr", "zaps", "ui.py")

_SYNC_LOOKUPS = {
    "load_uri",          # NWCUriManager — libsecret
    "load_key",          # KeyManager — libsecret
    "load_session",      # BunkerSessionManager — libsecret
    "nip49_encrypt",     # scrypt KDF — CPU-bound, seconds on mobile
}


def _parse(path):
    return ast.parse(open(path, encoding="utf-8").read())


def _method(tree, cls_name, meth_name):
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name
    )
    return next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == meth_name
    )


def _calls_in(node):
    """(call_name, parent_function_name) for every Call in node's body."""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.FunctionDef):
            continue
        if isinstance(n, ast.Call):
            fn = n.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name:
                out.append(name)
    return out


def _nested_fn_names(node):
    return {n.name for n in ast.walk(node) if isinstance(n, ast.FunctionDef)}


def _top_level_calls(node):
    """Call names that are NOT inside a nested def (i.e. run on click)."""
    nested = {
        id(n) for n in ast.walk(node) if isinstance(n, ast.FunctionDef)
    }
    out = []

    def visit(n):
        for c in ast.iter_child_nodes(n):
            if id(c) in nested and isinstance(c, ast.FunctionDef):
                continue
            if isinstance(c, ast.Call):
                name = getattr(c.func, "attr", None) or getattr(c.func, "id", None)
                if name:
                    out.append(name)
            visit(c)

    visit(node)
    return out


# ---------------------------------------------------------------------------
# 1. no sync secret lookup in a UI-callback top-level body
# ---------------------------------------------------------------------------


def test_wallet_group_build_does_not_read_keyring_sync():
    tree = _parse(_DIALOGS)
    node = _method(tree, "RelayPreferencesWindow", "_build_wallet_group")
    top = _top_level_calls(node)
    for banned in _SYNC_LOOKUPS:
        assert banned not in top, (
            f"_build_wallet_group calls {banned}() on the main thread — "
            "wrap it in run_off_main (settings-open freeze regression)"
        )
    assert "run_off_main" in top, "prefill must go through run_off_main"


def test_on_test_nwc_keyring_fallback_is_in_worker():
    tree = _parse(_DIALOGS)
    node = _method(tree, "RelayPreferencesWindow", "on_test_nwc")
    top = _top_level_calls(node)
    assert "load_uri" not in top, (
        "on_test_nwc reads libsecret on click; the fallback belongs in work()"
    )


def test_on_encrypt_key_and_scrypt_are_off_thread():
    tree = _parse(_DIALOGS)
    node = _method(tree, "ExportKeyDialog", "on_encrypt")
    top = _top_level_calls(node)
    for banned in ("load_key", "nip49_encrypt"):
        assert banned not in top, (
            f"on_encrypt runs {banned}() on the main thread"
        )
    assert "run_off_main" in top


def test_start_zap_backend_build_is_off_thread():
    tree = _parse(_ZAPS_UI)
    fn = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "start_zap"
    )
    top = _top_level_calls(fn)
    assert "wallet_backend_for" not in top or "run_off_main" in top, (
        "start_zap must build the backend via run_off_main when uncached"
    )


# ---------------------------------------------------------------------------
# 2. Received-zaps clear loop: no get_first_child/remove churn (CRITICAL)
# ---------------------------------------------------------------------------


def test_zap_received_clear_uses_tracked_rows():
    tree = _parse(_DIALOGS)
    node = _method(tree, "RelayPreferencesWindow", "_refresh_zap_received")
    src = ast.dump(node)
    assert "get_first_child" not in src, (
        "clearing an AdwPreferencesGroup via get_first_child removes its "
        "internal GtkBox and spams Adw CRITICALs; track added rows instead"
    )
    assert "_zap_received_rows" in src


# ---------------------------------------------------------------------------
# 3. AdwPreferencesGroup titles are Pango markup — no bare '&'
# ---------------------------------------------------------------------------


def test_preferences_group_titles_have_escaped_ampersands():
    for path in (_DIALOGS,):
        tree = _parse(path)
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            fname = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
            if fname != "PreferencesGroup":
                continue
            for kw in n.keywords:
                if kw.arg in ("title", "description") and isinstance(
                    kw.value, ast.Constant
                ):
                    val = kw.value.value
                    if isinstance(val, str) and "&" in val:
                        assert "&amp;" in val, (
                            f"unescaped & in PreferencesGroup.{kw.arg}: {val!r}"
                        )


# ---------------------------------------------------------------------------
# 4. functional: run_off_main (key_manager imports for real under the
#    conftest gi mocks — no GTK classes in that module)
# ---------------------------------------------------------------------------


def test_run_off_main_delivers_result_and_errors(monkeypatch):
    import gnostr.key_manager as km

    monkeypatch.setattr(km.GLib, "idle_add", lambda fn, *a: fn(*a), raising=False)

    got = []
    t = km.run_off_main(lambda: 42, got.append)
    t.join(5)
    assert got == [42]

    got_err = []
    t2 = km.run_off_main(
        lambda: (_ for _ in ()).throw(RuntimeError("boom")), got_err.append
    )
    t2.join(5)
    assert got_err == [None], "exceptions must collapse to None"
