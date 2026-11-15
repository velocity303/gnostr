"""FEED-TRUNC: feed cards collapse long posts behind 'Show more'.

Guards (house pattern: source-level + AST-extracted method tests):
  - GSettings key feed-collapse-chars registered with default 500, range
    200-5000, 0 = never collapse (sentinel handled in code, not schema).
  - the shared safe-truncate helper is used at ALL slice sites: feed cards
    (main.py) and the thread hero (thread_view.py) — no hardcoded [:500]
    slices may come back (THREAD-TRUNC must not regress).
  - PostWidget keeps the FULL content for copy/open-thread and only renders
    the cut; the Show-more row is a >=44px flat start-aligned button and
    expands in place via set_content.
  - the preferences dialog exposes exactly one row wired to the helper.
"""

import ast
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
GSCHEMA = REPO / "data" / "tech.livingonlinux.gnostr.gschema.xml"
MAIN = REPO / "src" / "gnostr" / "main.py"
THREAD_VIEW = REPO / "src" / "gnostr" / "ui" / "thread_view.py"
POST_WIDGET = REPO / "src" / "gnostr" / "ui" / "post_widget.py"
DIALOGS = REPO / "src" / "gnostr" / "dialogs.py"

from gnostr.util import collapse  # noqa: E402


# -- helper unit tests ----------------------------------------------------------


def test_fits_returns_unchanged():
    assert collapse.safe_truncate("short post", 500) == "short post"
    assert collapse.safe_truncate("", 500) == ""
    # cap 0 = never collapse
    long_text = "word " * 300
    assert collapse.safe_truncate(long_text, 0) == long_text


def test_cut_lands_on_word_boundary_with_ellipsis():
    content = " ".join(f"word{i}" for i in range(200))
    out = collapse.safe_truncate(content, 500)
    assert out.endswith("…")
    body = out[:-1]
    # every emitted word is whole (no 'wor' / 'd12' fragments)
    for piece in body.split():
        assert piece.startswith("word")


def test_never_splits_url_mention_or_nostr_ref():
    url = "https://example.com/some/deep/path?query=1&x=2"
    content = f"spam spam {url} and @npub1abc def " * 40
    out = collapse.safe_truncate(content, 500)
    # every URL that survived is present WHOLE (count matches intact copies);
    # a half-URL would leave a fragment the intact count cannot explain
    intact = content.count(url)
    emitted = out.count(url)
    assert emitted <= intact
    # no truncated URL tail may exist: strip the ellipsis, then no token that
    # starts with the scheme may differ from the full URL
    for token in out.rstrip("…").split():
        if token.startswith("http"):
            assert token == url
        if token.startswith("@"):
            assert token == "@npub1abc"
    # a nostr: ref is protected too
    content2 = "x " * 490 + " nostr:note1abcDEF_ tail"
    out2 = collapse.safe_truncate(content2, 500)
    assert "nostr:note1abcDEF_" in out2 or "nostr:note" not in out2


def test_long_first_token_hard_cut_not_empty():
    url = "https://example.com/" + "a" * 900
    out = collapse.safe_truncate(url + " tail", 500)
    assert out.endswith("…")
    assert len(out) > 1  # never an empty card


def test_normalize_cap():
    assert collapse.normalize_cap(0) == 0  # sentinel: never collapse
    assert collapse.normalize_cap(-5) == 0
    assert collapse.normalize_cap(500) == 500
    assert collapse.normalize_cap(150) == collapse.COLLAPSE_MIN
    assert collapse.normalize_cap(99999) == collapse.COLLAPSE_MAX
    assert collapse.normalize_cap("junk") == collapse.DEFAULT_COLLAPSE_CHARS
    assert collapse.DEFAULT_COLLAPSE_CHARS == 500


def test_settings_hard_fallback_without_gi(monkeypatch):
    """Dev-run contract: no Gio -> defaults on read, False on write (the UI
    must never crash over an unavailable schema)."""
    import sys

    monkeypatch.setitem(sys.modules, "gi", None)
    assert collapse.collapse_chars() == collapse.DEFAULT_COLLAPSE_CHARS
    assert collapse.set_collapse_chars(800) is False


# -- GSettings registration ------------------------------------------------------


def _schema_key():
    root = ET.parse(GSCHEMA).getroot()
    for schema in root.iter("schema"):
        for key in schema.iter("key"):
            if key.get("name") == "feed-collapse-chars":
                return key
    return None


def test_collapse_key_registered_with_default():
    key = _schema_key()
    assert key is not None, "feed-collapse-chars missing from gschema"
    assert key.get("type") == "i"
    default = key.find("default")
    assert default is not None and default.text.strip() == "500"
    rng = key.find("range")
    assert rng is not None
    assert rng.get("min") == "200" and rng.get("max") == "5000"


# -- helper used at ALL slice sites ----------------------------------------------


def test_main_wires_collapse_cap_at_both_feed_sites():
    src = MAIN.read_text()
    assert "collapse_util.collapse_chars()" in src
    # both PostWidget construction sites in main.py (live insert + hydration)
    # pass the cap
    assert src.count("collapse_cap=collapse_util.collapse_chars()") == 2


def test_thread_hero_uses_shared_helper_no_hard_slice():
    src = THREAD_VIEW.read_text()
    # every hero cut routes through the shared token-safe helper
    assert "collapse_util.safe_truncate(content, _HERO_CAP)" in src
    assert "_HERO_CAP = collapse_util.DEFAULT_COLLAPSE_CHARS" in src
    # THREAD-TRUNC regression guard: the hardcoded slice must stay gone
    assert '[:500]' not in src


def test_collapse_module_has_no_top_level_gi_import():
    """util/ layer contract: Gio is imported lazily inside the try-block."""
    tree = ast.parse((REPO / "src" / "gnostr" / "util" / "collapse.py").read_text())
    for node in tree.body:
        assert not (
            isinstance(node, (ast.Import, ast.ImportFrom)) and "gi" in ast.dump(node)
        ), "gi must not be imported at module level in util/"


# -- PostWidget contract ----------------------------------------------------------


def _extract_method(path, class_name, method_name):
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == method_name:
                    sub.decorator_list.clear()
                    module = ast.Module(body=[sub], type_ignores=[])
                    ast.fix_missing_locations(module)
                    ns = {}
                    exec(compile(module, str(path), "exec"), ns)
                    return ns[method_name]
    raise AssertionError(f"{class_name}.{method_name} not found in {path}")


def test_postwidget_collapse_contract_in_source():
    src = POST_WIDGET.read_text()
    assert "collapse_cap=0" in src
    assert 'Gtk.Button(label="Show more", css_classes=["flat"])' in src
    # touch-target floor + start alignment (mirror thread_view._hero_toggle)
    assert "set_size_request(-1, 44)" in src
    assert "set_halign(Gtk.Align.START)" in src
    # full content survives for copy + open-thread
    assert "self._full_content" in src
    assert "show_thread(\n            event_id, pubkey, self._full_content, tags\n        )" in src or "self._full_content, tags" in src
    assert "Gdk.ContentProvider.new_for_value(self._full_content)" in src
    # the toggle row is excluded from the open-thread gesture walk
    assert '_collapse_toggle", None' in src


def test_collapse_toggle_expands_in_place():
    toggle = _extract_method(POST_WIDGET, "PostWidget", "_on_collapse_toggle")
    full = "full post content " * 100
    removed = []

    class Btn:
        def get_parent(self):
            return SimpleNamespace(remove=lambda w: removed.append(w))

    btn = Btn()
    fake = SimpleNamespace()
    fake._collapsed = True
    fake._full_content = full
    fake._collapse_toggle = btn
    fake.set_content = lambda c: setattr(fake, "content", c)
    toggle(fake, None)
    assert fake._collapsed is False
    assert fake.content == full  # set_content got the FULL string
    assert removed == [btn]
    assert fake._collapse_toggle is None
    # second call is a no-op (one-way expand)
    toggle(fake, None)
    assert removed == [btn]


# -- preferences row ---------------------------------------------------------------


def test_preferences_exposes_one_collapse_row():
    src = DIALOGS.read_text()
    assert "_build_feed_display_group(self.media_page)" in src
    assert "Adw.SpinRow.new_with_range(0, 5000, 100)" in src
    assert "collapse_util.collapse_chars()" in src
    assert "collapse_util.set_collapse_chars(" in src
