"""Unit tests for ScrollGate — the touch-activation cooldown after scroll
momentum (issue #22). GTK-free by design: an injectable clock lets these run
headless with zero ``gi`` mocking.

The gate is the pure decision layer; the GTK wiring (FeedView stamping on the
vadjustment's ``value-changed``, post_widget/renderer consulting it) lives in
separate modules and is exercised under xvfb by the existing real-GTK suite.
"""

import time

from gnostr.util.scroll_gate import ScrollGate


def test_fresh_gate_is_not_in_cooldown():
    """No scroll yet -> nothing to debounce."""
    g = ScrollGate(clock=time.monotonic, grace_ms=600)
    assert g.in_cooldown() is False


def test_in_cooldown_within_grace_window():
    """A press inside the grace window after a scroll is swallowed."""
    clock = [1000.0]

    def fake_clock():
        return clock[0]

    g = ScrollGate(clock=fake_clock, grace_ms=600)
    g.mark_scroll(is_touch=True)
    # 300ms after the scroll stamp -> still cooling down.
    clock[0] = 1000.3
    assert g.in_cooldown() is True


def test_not_in_cooldown_after_grace_window():
    """A deliberate tap lands well after the glide ends -> activate normally."""
    clock = [1000.0]

    def fake_clock():
        return clock[0]

    g = ScrollGate(clock=fake_clock, grace_ms=600)
    g.mark_scroll(is_touch=True)
    # 700ms after the scroll stamp -> past the 600ms grace window.
    clock[0] = 1000.7
    assert g.in_cooldown() is False


def test_grace_boundary_is_inclusive_of_grace():
    """Exactly at grace_ms is NOT in cooldown (strict '<' comparison)."""
    clock = [1000.0]

    def fake_clock():
        return clock[0]

    g = ScrollGate(clock=fake_clock, grace_ms=600)
    g.mark_scroll(is_touch=True)
    clock[0] = 1000.6  # exactly 600ms later
    assert g.in_cooldown() is False


def test_reset_clears_cooldown():
    """reset() clears the stamp so a subsequent press activates."""
    clock = [1000.0]

    def fake_clock():
        return clock[0]

    g = ScrollGate(clock=fake_clock, grace_ms=600)
    g.mark_scroll(is_touch=True)
    clock[0] = 1000.3
    assert g.in_cooldown() is True
    g.reset()
    assert g.in_cooldown() is False


def test_reset_clears_last_scroll_was_touch():
    """reset() also clears the recorded device class."""
    g = ScrollGate(clock=time.monotonic, grace_ms=600)
    g.mark_scroll(is_touch=True)
    assert g.last_scroll_was_touch is True
    g.reset()
    assert g.last_scroll_was_touch is False


def test_mark_scroll_records_touch_class():
    """mark_scroll records whether the scroll was touch-originated."""
    g = ScrollGate(clock=time.monotonic, grace_ms=600)
    g.mark_scroll(is_touch=False)  # wheel/pointer scroll
    assert g.last_scroll_was_touch is False


def test_grace_ms_is_respected():
    """The grace length is honored: shorter grace -> sooner activation."""
    clock = [1000.0]

    def fake_clock():
        return clock[0]

    g = ScrollGate(clock=fake_clock, grace_ms=600)
    g.mark_scroll(is_touch=True)
    clock[0] = 1000.3  # 300ms after -> still cooling down (grace is 600ms)
    assert g.in_cooldown() is True
    clock[0] = 1000.7  # 700ms after -> past the grace window
    assert g.in_cooldown() is False


def test_zero_stamp_is_not_in_cooldown():
    """A gate that has never been stamped is inert (no accidental gating)."""
    g = ScrollGate(clock=time.monotonic, grace_ms=600)
    g.last_stamp = 0.0
    assert g.in_cooldown() is False


def test_is_touch_source_returns_false_without_gdk():
    """is_touch_source must never raise in a headless context (no Gdk)."""
    # With gi/Gdk unavailable the lazy import fails -> default False.
    assert ScrollGate.is_touch_source(None) is False
