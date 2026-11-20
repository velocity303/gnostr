"""Static guard: never call Adw.ActionRow accessors that do not exist.

Adw.ActionRow exposes NO get_subtitle_label()/get_title_label() — the
subtitle label is private. groups_page._append_timeline_event called
row.get_subtitle_label() on every timeline event; the AttributeError fired
before the append, so the FLX1 group timeline was always empty while the
mocked suite stayed green (NIPW-P3-FIX). The line cap API is
set_subtitle_lines(n) (wraps by default, ellipsizes at the cap).

CI runs only `pytest tests/` — the *_gtk_real harnesses are meson-only —
so this source scan is what keeps CI meaningful for this bug class.
"""

from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "gnostr"

BANNED = [
    (
        ".get_subtitle_label(",
        "Adw.ActionRow has no subtitle-label accessor; use set_subtitle_lines(n)",
    ),
    (
        ".get_title_label(",
        "Adw.ActionRow has no title-label accessor; use set_title_lines(n)",
    ),
]


def test_no_nonexistent_actionrow_accessors():
    problems = []
    for path in sorted(SRC.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for needle, hint in BANNED:
                if needle in line:
                    problems.append(
                        f"{path.relative_to(SRC)}:{lineno}: {needle!r} — {hint}"
                    )
    assert not problems, "nonexistent Adw.ActionRow API used:\n" + "\n".join(problems)
