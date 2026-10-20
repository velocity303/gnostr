"""Unit tests for ScrollGate — the touch-activation cooldown after scroll
momentum (issue #22). GTK-free by design: an injectable clock lets these run
headless with zero ``gi`` mocking.

The gate is the pure decision layer; the GTK wiring (FeedView stamping on the
vadjustment's ``value-changed``, post_widget/renderer consulting it) lives in
separate modules and is exercised under xvfb by the existing real-GTK suite.
"""

import time

from gnostr.util.scroll_gate import SWIPE_THRESHOLD_PX, ScrollGate


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


# ---------------------------------------------------------------------------
# Slice 3: geometric swipe guard (press -> release travel)
# ---------------------------------------------------------------------------
# The cooldown above is temporal and cannot catch a swipe that starts where the
# feed cannot scroll (top of feed, rubber-band): no value-changed fires, the
# gate is never stamped, in_cooldown() stays False, and the swipe's release
# opens the thread. These tests cover the position-independent guard.


def _fake_clock(box):
    def clock():
        return box[0]

    return clock


def test_no_press_recorded_never_reports_swipe():
    """A caller that never wired 'pressed' behaves exactly as before rather
    than swallowing every tap."""
    g = ScrollGate(clock=time.monotonic, grace_ms=600)
    assert g.swipe_since_press(500, 900) is False


def test_tap_in_place_is_not_a_swipe():
    clock = [1000.0]
    g = ScrollGate(clock=_fake_clock(clock), grace_ms=600)
    g.note_press(360, 700)
    assert g.swipe_since_press(362, 704) is False


def test_vertical_swipe_is_a_swipe():
    """The exact failure: press at the top of the feed, drag up, release. The
    feed could not scroll (already at top) so the cooldown never engaged."""
    clock = [1000.0]
    g = ScrollGate(clock=_fake_clock(clock), grace_ms=600)
    g.note_press(360, 1200)
    assert g.in_cooldown() is False  # never stamped — temporal gate is inert
    assert g.swipe_since_press(360, 800) is True


def test_horizontal_swipe_is_a_swipe():
    clock = [1000.0]
    g = ScrollGate(clock=_fake_clock(clock), grace_ms=600)
    g.note_press(200, 500)
    assert g.swipe_since_press(260, 505) is True


def test_travel_at_threshold_is_not_a_swipe():
    """Strict '>' comparison: exactly threshold travel is still a tap."""
    g = ScrollGate(clock=time.monotonic, grace_ms=600)
    g.note_press(100, 100)
    assert g.swipe_since_press(100 + SWIPE_THRESHOLD_PX, 100) is False
    assert g.swipe_since_press(100 + SWIPE_THRESHOLD_PX + 1, 100) is True


def test_reset_clears_press_origin():
    """Switching feeds must not carry a stale press origin into the new page."""
    g = ScrollGate(clock=time.monotonic, grace_ms=600)
    g.note_press(100, 100)
    g.reset()
    assert g.swipe_since_press(900, 900) is False


def test_swipe_guard_is_independent_of_cooldown():
    """The two guards are complementary, not redundant: a swipe with travel is
    caught by the geometric guard even when the cooldown has expired, and a
    stationary tap right after a scroll is caught by the cooldown alone."""
    clock = [1000.0]
    g = ScrollGate(clock=_fake_clock(clock), grace_ms=600)
    # Case A: cooldown expired, but the gesture travelled -> swipe guard fires.
    g.mark_scroll(is_touch=True)
    clock[0] = 1002.0
    g.note_press(360, 1200)
    assert g.in_cooldown() is False
    assert g.swipe_since_press(360, 600) is True
    # Case B: no travel, inside the grace window -> cooldown fires, swipe guard
    # must not (a deliberate tap must still work right after the glide).
    g.reset()
    g.mark_scroll(is_touch=True)
    clock[0] = 1000.3
    g.note_press(360, 700)
    assert g.in_cooldown() is True
    assert g.swipe_since_press(361, 702) is False
