"""VID-DECODE-UX: playbin3 builds must be staggered, not burst.

Main-loop lag probe (FLX1, 2026-11-13) proved the perceived 1-2 Hz stutter
is the GLib loop blocking 881-1625 ms while a burst of card renders builds
pipelines back-to-back on the main thread (fps_dropped=0 ruled out decode).
_schedule_media_build spaces builds >=200 ms apart; a spinner is already
visible so the stagger is invisible, the stall was not.
"""

from gnostr import renderer


class _FakeGLib:
    def __init__(self):
        self.now = 0.0
        self.timeouts = []  # (delay_ms, callback)

    def get_monotonic_time(self):
        return self.now * 1000.0  # microseconds

    def timeout_add(self, ms, cb):
        self.timeouts.append((ms, cb))
        return len(self.timeouts)


class _FakeContainer:
    def __init__(self, parent=True):
        self._parent = object() if parent else None

    def get_parent(self):
        return self._parent


def _wire(monkeypatch):
    fake = _FakeGLib()
    calls = []
    monkeypatch.setattr(renderer, "GLib", fake)
    monkeypatch.setattr(
        renderer.VideoPlayer,
        "load_and_play",
        staticmethod(lambda *a, **k: calls.append(a)),
    )
    monkeypatch.setattr(
        renderer,
        "_media_build_state",
        {"last": -renderer._MEDIA_BUILD_MIN_GAP_MS},
    )
    return fake, calls


def test_first_build_is_immediate(monkeypatch):
    fake, calls = _wire(monkeypatch)
    renderer._schedule_media_build("u", _FakeContainer(), None, None)
    assert len(calls) == 1
    assert fake.timeouts == []


def test_burst_is_staggered(monkeypatch):
    fake, calls = _wire(monkeypatch)
    renderer._schedule_media_build("a", _FakeContainer(), None, None)
    # Same instant: the next two builds must queue, not run.
    renderer._schedule_media_build("b", _FakeContainer(), None, None)
    renderer._schedule_media_build("c", _FakeContainer(), None, None)
    assert len(calls) == 1
    assert len(fake.timeouts) == 2
    # Firing the queued callbacks builds them (parent still alive).
    for _, cb in list(fake.timeouts):
        cb()
    assert len(calls) == 3


def test_queued_build_skips_dead_container(monkeypatch):
    fake, calls = _wire(monkeypatch)
    renderer._schedule_media_build("a", _FakeContainer(), None, None)
    dead = _FakeContainer(parent=False)
    renderer._schedule_media_build("b", dead, None, None)
    for _, cb in list(fake.timeouts):
        cb()
    # Only the live build ran; the scrolled-away card was dropped.
    assert len(calls) == 1


def test_gap_advances_after_each_build(monkeypatch):
    fake, calls = _wire(monkeypatch)
    renderer._schedule_media_build("a", _FakeContainer(), None, None)  # immediate
    renderer._schedule_media_build("b", _FakeContainer(), None, None)  # queued
    fake.now = 0.2
    _, cb = fake.timeouts[0]
    cb()  # build b runs, advancing the stamp to now
    assert len(calls) == 2
    # Immediately after b, a new request must defer again.
    renderer._schedule_media_build("d", _FakeContainer(), None, None)
    assert len(calls) == 2
