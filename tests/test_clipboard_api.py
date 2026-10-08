"""Static guard: Gdk.Clipboard has no set_text() in GTK4 (issue #33).

The GTK3 Gtk.Clipboard.set_text call survived the port and made every
"Copy" button in the encrypted-backup flows silently do nothing (the
AttributeError was swallowed by the PyGObject signal handler). MagicMock
-based unit tests can never catch this — a mock happily accepts any
attribute — so the guard is a source scan instead.

Correct GTK4 pattern: clipboard.set_content(Gdk.ContentProvider.new_for_value(text))
"""
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "gnostr"


def test_no_clipboard_set_text_calls():
    offenders = []
    for py in SRC.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            code = stripped.split("#", 1)[0]
            if "clipboard" in code and ".set_text(" in code:
                offenders.append(f"{py.relative_to(SRC.parent)}:{i}: {stripped}")
    assert not offenders, (
        "Gdk.Clipboard.set_text does not exist in GTK4; use "
        "clipboard.set_content(Gdk.ContentProvider.new_for_value(text)).\n"
        + "\n".join(offenders)
    )
