"""VID-DECODE-UX: the 500 ms position tick must be layout-damage-free.

A1b Finding 3 + device evidence: the rhythmic 1-2 Hz "finger-tap" stutter
survived every decode/buffering/main-thread fix because the video card's
own progress UI was redrawing at 2 Hz: a proportional-font time label
whose width changed every second (queue_resize -> scrolled-viewport
relayout) and an unconditional set_text (GTK queues resize+redraw even
for identical text). Guards below are the fix; keep them.
"""

from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "gnostr" / "renderer.py"


def test_time_label_uses_tabular_digits():
    src = SRC.read_text()
    assert 'position_label.add_css_class("numeric")' in src


def test_label_text_only_written_on_change():
    src = SRC.read_text()
    assert "position_label.get_text() != txt" in src


def test_scale_updates_stay_quiet():
    # The timer's programmatic set_value must not re-enter the seek handler.
    src = SRC.read_text()
    assert "_seek_on_drag" in src
    assert "position_scale._quiet = True" in src
