"""Markdown-lite subset tests (rich editor phase 1).

Pure-function tests over gnostr.markup — no GTK needed (same contract as
test_nip_builders). Table-driven to mirror the reference-renderer rules in
miku's spec: emphasis hugs non-whitespace, never nests, never spans newlines,
never fires intra-word for underscores (snake_case stays literal).
"""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)

from gnostr.markup import (SUPPORTED_FEATURES, apply_marker, emphasis_to_pango,
                           insert_at)

# -- emphasis_to_pango -------------------------------------------------------

PANGO_CASES = [
    # (input (already escaped), expected pango)
    ("**bold**", "<b>bold</b>"),
    ("_italic_", "<i>italic</i>"),
    ("`code`", "<tt>code</tt>"),
    ("a **bold** word", "a <b>bold</b> word"),
    ("**not bold **", "**not bold **"),  # must hug non-whitespace
    ("_not italic _", "_not italic _"),
    ("**a** and **b**", "<b>a</b> and <b>b</b>"),
    ("**multi word bold**", "<b>multi word bold</b>"),
    # no newlines inside emphasis
    ("**cross\nline**", "**cross\nline**"),
    # snake_case: intra-word underscores must NOT italicize
    ("my_var_name", "my_var_name"),
    ("snake_case_identifier stays", "snake_case_identifier stays"),
    # word-adjacent underscore still italic (punctuation boundary)
    ("(_emphasis_)", "(<i>emphasis</i>)"),
    # code span consumed atomically: markers inside stay literal
    ("`**not bold**`", "<tt>**not bold**</tt>"),
    # empty emphasis is not emphasis
    ("****", "****"),
    ("__", "__"),
    # unbalanced markers stay literal
    ("**open only", "**open only"),
    ("no *single* asterisk", "no *single* asterisk"),  # single asterisk: non-goal
    # already-escaped entities pass through untouched
    ("&lt;b&gt;**bold**&lt;/b&gt;", "&lt;b&gt;<b>bold</b>&lt;/b&gt;"),
    # combined
    ("**b** `_i_`", "<b>b</b> <tt>_i_</tt>"),
]


def test_emphasis_table():
    for src, want in PANGO_CASES:
        got = emphasis_to_pango(src)
        assert got == want, f"input {src!r}: want {want!r}, got {got!r}"


def test_no_markers_fast_path():
    assert emphasis_to_pango("plain text, nothing here") == "plain text, nothing here"


def test_supported_features_locked():
    # Spec non-goals must be impossible to offer: exactly these three markers.
    assert SUPPORTED_FEATURES == {
        "bold": ("**", "**"),
        "italic": ("_", "_"),
        "code": ("`", "`"),
    }


# -- apply_marker ------------------------------------------------------------


def test_wrap_selection():
    text = "hello world"
    new, s, e = apply_marker(text, 0, 5, "bold")
    assert new == "**hello** world"
    assert new[s:e] == "**hello**"


def test_empty_selection_caret_inside():
    new, s, e = apply_marker("hello", 5, 5, "bold")
    assert new == "hello****"
    assert s == e == 7  # caret between the markers (after the opening **)


def test_toggle_unwraps():
    new, s, e = apply_marker("**hello**", 2, 7, "bold")
    assert new == "hello"
    assert (s, e) == (0, 5)


def test_italic_and_code_markers():
    assert apply_marker("hi", 0, 2, "italic")[0] == "_hi_"
    assert apply_marker("hi", 0, 2, "code")[0] == "`hi`"


# -- insert_at ---------------------------------------------------------------


def test_insert_at():
    new, caret = insert_at("ab", 1, "XY")
    assert new == "aXYb"
    assert caret == 3


# -- renderer wiring guard ----------------------------------------------------


def test_renderer_applies_emphasis_to_plain_text():
    """Source scan: every plain-text fragment in renderer.render() must pass
    through emphasis_to_pango(escape(...)). The mocked suite can't execute the
    real render path, so a dropped call would silently ship raw ** markers."""
    import os

    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", "gnostr", "renderer.py"
    )
    src = open(path, encoding="utf-8").read()
    assert "from .markup import emphasis_to_pango" in src
    # Every text_fragments plain-text append is emphasis-processed. Scan with
    # whitespace collapsed so black's line wrapping can't fool a naive match.
    flat = " ".join(src.split())
    assert "(None, GLib.markup_escape_text(" not in flat, (
        "a raw escaped fragment bypasses the emphasis pass"
    )
    import re

    assert len(re.findall(r"emphasis_to_pango\(\s*GLib\.markup_escape_text\(", flat)) >= 6
