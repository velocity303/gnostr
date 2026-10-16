"""Key-health store round-trip + backup reminder display logic (issue #16, P3)."""

import ast
import textwrap
import time

import pytest

from gnostr import key_health


@pytest.fixture
def health_store(tmp_path, monkeypatch):
    """Point key_health's state file at a temp dir (GLib is mocked in conftest)."""
    monkeypatch.setattr(key_health.GLib, "get_user_data_dir", lambda: str(tmp_path))
    return tmp_path


def test_store_roundtrip(health_store):
    assert key_health.last_backup_verified() is None
    ts = key_health.mark_backup_verified()
    assert key_health.last_backup_verified() == ts
    # second backup overwrites
    time.sleep(0.01)
    ts2 = key_health.mark_backup_verified()
    assert key_health.last_backup_verified() == ts2
    assert ts2 >= ts


def test_store_holds_no_key_material(health_store):
    key_health.mark_backup_verified()
    raw = (health_store / "gnostr" / key_health.STATE_FILENAME).read_text()
    assert "nsec" not in raw
    assert "priv" not in raw.lower()


def test_corrupt_store_reads_as_none(health_store):
    path = health_store / "gnostr" / key_health.STATE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json")
    assert key_health.last_backup_verified() is None


def test_format_last_backup(health_store):
    ts = 1760000000
    text = key_health.format_last_backup(ts)
    assert time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) == text


# --- backup reminder display logic (sidebar._refresh_key_health) ---
# conftest mocks gi, so the Sidebar class is unconstructable; extract the
# real method via ast and bind it to a FakeView (pattern from test_follows_view).


def _extract_method(name):
    """Compile one FunctionDef out of sidebar.py (gi is mocked, class is
    unconstructable — same ast pattern as test_follows_view.py)."""
    src = open("src/gnostr/ui/sidebar.py").read()
    tree = ast.parse(src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    mod = ast.Module(body=[fn], type_ignores=[])
    # __package__ lets the method's `from .. import key_health` resolve to
    # the real gnostr.key_health module (the one the fixture patches).
    ns = {"__package__": "gnostr.ui"}
    exec(compile(mod, "src/gnostr/ui/sidebar.py", "exec"), ns)
    return ns[name]


class FakeLabel:
    def __init__(self):
        self.text = None

    def set_text(self, t):
        self.text = t


class FakeView:
    def __init__(self):
        self.key_health_label = FakeLabel()


def test_reminder_prompt_when_no_backup(health_store):
    view = FakeView()
    _extract_method("_refresh_key_health")(view)
    assert "No backup verified" in view.key_health_label.text


def test_reminder_shows_timestamp_after_backup(health_store):
    ts = key_health.mark_backup_verified()
    view = FakeView()
    _extract_method("_refresh_key_health")(view)
    assert key_health.format_last_backup(ts) in view.key_health_label.text
    assert "Re-verify" in view.key_health_label.text
