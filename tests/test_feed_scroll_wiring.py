"""Regression guard: the pagination trigger must connect to the signal that
actually fires on scroll.

Gtk.Adjustment.set_value() emits "value-changed" ONLY; "changed" fires solely
for upper/page_size/step changes. The original wiring connected
on_scroll_changed to "changed", so scrolling never paged the feed — and every
existing test called on_scroll_changed(va) directly, never through the real
signal, so nothing caught it. This test pins the connection at source level
(headless; the real signal dispatch is covered by the device harness).
"""
import os
import re

_SRC = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", "gnostr", "ui", "feed_view.py"
    )
)


def test_scroll_pagination_connected_to_value_changed():
    src = open(_SRC, encoding="utf-8").read()
    assert re.search(
        r'connect\(\s*"value-changed"\s*,\s*self\.on_scroll_changed\s*\)', src
    ), "on_scroll_changed must connect to 'value-changed' (set_value emits only that)"
    assert not re.search(
        r'connect\(\s*"changed"\s*,\s*self\.on_scroll_changed\s*\)', src
    ), "'changed' never fires on scroll — do not wire pagination to it"
