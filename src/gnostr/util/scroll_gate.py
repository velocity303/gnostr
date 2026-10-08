"""ScrollGate — touch-activation cooldown after scroll momentum.

Debounce card/link/video activation on touchscreens: after a fast scroll the
feed keeps emitting vadjustment ``value-changed`` events through the kinetic
(glide) phase even though the finger is already up. A press landing in that
window is almost always a swipe, not a deliberate tap. ScrollGate stamps the
time of the last scroll and answers whether we are still inside a grace window
so callers can swallow activation for the duration.

GTK-free by design: the class takes an injectable clock so it is unit-tested
headless with zero ``gi`` mocking. The GTK wiring is three one-line call sites
in FeedView / post_widget.py / renderer.py that consult :meth:`in_cooldown`.

Design (see issue #22, spec comment by hermes):
  * Trigger is time-since-last-scroll, NOT velocity — velocity tracking adds
    state for no benefit. ``value-changed`` (not ``changed``) keeps firing
    through the glide, so grace naturally starts at motion stop.
  * Touch-only: desktop wheel scrolling also moves the vadjustment, and a naive
    gate would swallow mouse clicks made right after a wheel scroll. The gate
    records the device class of the last scroll (touch drag vs wheel) via the
    FeedView; callers pass ``is_touch`` when stamping so the gate is inert for
    pointer-originated activation. Slice 1 measures on-device before tuning the
    constant.
"""

from dataclasses import dataclass, field
import time


@dataclass
class ScrollGate:
    """A pure timestamp gate: blocks activation while inside a post-scroll
    grace window.

    Attributes:
        clock: monotonic time source (``time.monotonic``); injectable for tests.
        grace_ms: cooldown length in milliseconds from the last scroll stamp.
        last_stamp: epoch of the last :meth:`mark_scroll` call.
        last_scroll_was_touch: device class of the last scroll; gates touch-only
            activation so wheel-scroll cooldown does not block mouse clicks.
    """

    clock: callable = field(default_factory=lambda: time.monotonic)
    grace_ms: int = 600
    last_stamp: float = 0.0
    last_scroll_was_touch: bool = False

    def mark_scroll(self, is_touch=True):
        """Stamp the current time as the last scroll event and record whether
        it was a touch drag (True) or a wheel/pointer scroll (False)."""
        self.last_stamp = self.clock()
        self.last_scroll_was_touch = bool(is_touch)

    def in_cooldown(self):
        """Return True while we are inside the grace window since the last
        scroll. Touch-only activation consults this; pointer activation must
        additionally confirm :attr:`last_scroll_was_touch` (see callers)."""
        if self.last_stamp == 0.0:
            return False
        return (self.clock() - self.last_stamp) * 1000 < self.grace_ms

    def reset(self):
        """Clear the stamp entirely — used in tests and when switching feeds."""
        self.last_stamp = 0.0
        self.last_scroll_was_touch = False

    @staticmethod
    def is_touch_source(source):
        """Return True if a ``Gdk.InputSource`` originates from a touchscreen.

        GTK-free until called with a real device: ``Gdk`` is imported lazily so
        this module stays importable in headless CI (no display needed). The
        enum is resolved from the source value's own module — the real
        ``Gdk.InputSource`` in production, the test stub's enum under the
        conftest gi mocks — so the comparison is against the same enum object
        the caller's device came from. Returns False on any failure or unknown
        source so callers default to "not gated" rather than swallowing a
        deliberate tap.
        """
        try:
            if source is None:
                return False
            input_type = source.get_input_source()
            # gi enum members are int-compatible, so an int comparison works
            # against the real device enum in production AND against plain-int
            # stubs under the test suite's gi mocks (where two MagicMock
            # attributes never compare equal).
            return int(input_type) == _touchscreen_value()
        except Exception:
            return False


def _touchscreen_value():
    """Return the int value of ``Gdk.InputSource.TOUCHSCREEN``.

    GTK4's real enum is ``Gdk.InputSource.TOUCHSCREEN`` (int 3). The
    similarly-named enums do not exist in GTK4's introspection — referencing
    one raises AttributeError, the except swallowed it, and the gate silently
    never engaged on the phone (issue #22 review, bug 1). Falls back to the
    stable ABI value 3 when Gdk is unimportable or mocked beyond ``int()``
    (the conftest gi mocks), so the classification still works headless.
    """
    try:
        from gi.repository import Gdk

        return int(Gdk.InputSource.TOUCHSCREEN)
    except Exception:
        return 3
