"""Profile header must never force the page wider than its allocation.

Device report: a profile whose display name is a long emoji run broke the
frame on the FLX1. Emoji runs (and base32 npubs / URLs) contain no word
boundaries, so Pango measures them as one unbreakable word; a plain
wrapping label then requests that full natural width and the page widens
past the window. The header labels must use CHAR wrapping or ellipsize
with a capped max_width_chars. Source-level guards (conftest mocks gi).
"""

from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "gnostr" / "ui" / "profile_view.py"


def test_pango_imported():
    src = SRC.read_text()
    assert any(
        l.startswith("from gi.repository") and "Pango" in l
        for l in src.splitlines()
    )


def test_display_name_char_wraps_with_cap():
    src = SRC.read_text()
    assert "lbl_name.set_wrap(True)" in src
    assert "lbl_name.set_wrap_mode(Pango.WrapMode.CHAR)" in src
    assert "lbl_name.set_max_width_chars(" in src


def test_about_char_wraps_with_cap():
    src = SRC.read_text()
    assert "lbl_about.set_wrap_mode(Pango.WrapMode.CHAR)" in src
    assert "lbl_about.set_max_width_chars(" in src


def test_npub_ellipsizes_with_cap():
    src = SRC.read_text()
    assert "lbl_npub.set_ellipsize(Pango.EllipsizeMode.END)" in src
    assert "lbl_npub.set_max_width_chars(" in src


def test_website_and_lud16_ellipsize():
    src = SRC.read_text()
    # Button has no Python `.child` attribute — must use get_child().
    assert "btn_website.get_child()" in src
    assert "btn_website.child." not in src
    assert "site_lbl.set_ellipsize(Pango.EllipsizeMode.END)" in src
    assert "lbl_lud16.set_ellipsize(Pango.EllipsizeMode.END)" in src
