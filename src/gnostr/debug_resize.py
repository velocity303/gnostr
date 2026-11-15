"""Debug-only queue_resize tracer (enable: GNOSTR_DEBUG_RESIZE=1).

VID-DECODE-UX: the FLX1 stutter survived every decode/main-loop/media fix.
Device logs showed full-tree measure passes at 0.2-1 s intervals (the
"AdwToastOverlay exceeds MainWindow width: requested 1833 px" warnings
fire on every full-tree measure — the 1833 px is the feed's natural
width, not a toast). This tracer names the Python caller of every
queue_resize so the periodic invalidator is identified from a device log
instead of guessed at. Python-level patch only sees Python-originated
calls — which is exactly the suspect class; internal GTK relayouts stay
silent.
"""

import os
import time
import traceback

from gi.repository import Gtk

_MIN_INTERVAL_S = 0.05


def _widget_path(widget):
    names = []
    w = widget
    while w is not None and len(names) < 4:
        try:
            names.append(w.get_css_name())
        except Exception:
            break
        w = w.get_parent()
    return "<-".join(names)


def install(window=None):
    if os.environ.get("GNOSTR_DEBUG_RESIZE") == "1":
        _install_resize_tracer()
    if window is not None and os.environ.get("GNOSTR_DEBUG_PAINT") == "1":
        _install_paint_probe(window)


def _install_resize_tracer():
    orig = Gtk.Widget.queue_resize
    last = [0.0]

    def traced(self):
        now = time.monotonic()
        if now - last[0] >= _MIN_INTERVAL_S:
            last[0] = now
            frames = [
                f
                for f in traceback.extract_stack()
                if "/gnostr/" in f.filename
                and "debug_resize" not in f.filename
            ]
            tail = "; ".join(
                f"{os.path.basename(f.filename)}:{f.lineno} {f.name}"
                for f in frames[-3:]
            )
            print(f"[RESIZE] {_widget_path(self)} :: {tail}", flush=True)
        return orig(self)

    Gtk.Widget.queue_resize = traced


def _install_paint_probe(window):
    """Log every >60 ms gap between painted frames (frame clock, µs).

    VID-DECODE-UX: quantifies the on-device stutter rhythm without a
    rebuild-per-guess loop: regular ~500 ms/1 s gaps name a timer;
    irregular bursts name event-driven content churn."""
    clock = window.get_frame_clock()
    if clock is None:
        return
    state = {"last": 0}

    def on_tick(_fc, frame_time_us):
        last = state["last"]
        state["last"] = frame_time_us
        if last:
            gap = (frame_time_us - last) / 1000.0
            if gap > 60.0:
                print(f"[PAINT] gap {gap:.0f}ms", flush=True)

    clock.connect("paint-tick", on_tick)
