"""Static guard: DM views must not call GTK/Adw APIs removed in the GTK 4.22
runtime shipped on the Librem 5.

Each of these compiled/imported fine on older GTK but raised AttributeError at
runtime on the phone, killing the DM inbox or thread view on open:

- Adw.Clamp.set_tightening_ratio  (added 1.6; use set_tightening_threshold)
- Gtk.ScrolledWindow.set_scroll_on_focus  (removed 4.20; default was FALSE)
- Gtk.Entry.set_enable_ime  (removed 4.16; IME always enabled)
- Gtk.Widget.get_children  (removed 4.10; walk get_first_child/next_sibling)

Source-scan test per tests/AGENTS.md — no gi import needed.
"""

from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "gnostr" / "ui"

BANNED = [
    ("set_tightening_ratio", "use set_tightening_threshold (px) instead"),
    ("set_scroll_on_focus", "removed in GTK 4.20; default was already FALSE"),
    ("set_enable_ime", "removed in GTK 4.16; IME input is always enabled"),
    (
        ".get_children(",
        "removed in GTK 4.10; walk get_first_child()/get_next_sibling()",
    ),
]

DM_FILES = [
    "dm_inbox_view.py",
    "dm_thread_view.py",
    "dm_recipient_view.py",
    "followers_list_view.py",
    "icons.py",
    "compose_box.py",
]


def test_dm_views_no_removed_gtk_apis():
    problems = []
    for fname in DM_FILES:
        path = SRC / fname
        assert path.exists(), f"missing {path}"
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for needle, hint in BANNED:
                if needle in line:
                    problems.append(f"{fname}:{lineno}: {needle!r} — {hint}")
    assert not problems, "removed GTK/Adw API used:\n" + "\n".join(problems)
