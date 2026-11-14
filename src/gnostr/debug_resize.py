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


def install():
    if os.environ.get("GNOSTR_DEBUG_RESIZE") != "1":
        return
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
