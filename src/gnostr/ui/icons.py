# src/gnostr/ui/icons.py
"""Icon theme probe shared by widgets (spec: docs/specs/
dm-initiation-and-feed-freshness.md A1).

Hard-coded symbolic names are a known breakage class: a glyph that exists
on the desktop theme can be absent on Phosh/Adwaita-compact (the FAB
`edit-create-symbolic` bug, the footer `chat-bubble-symbolic` bug). The
house pattern is a candidate list probed against the ACTIVE icon theme,
first `has_icon()` hit wins, last candidate is the guaranteed fallback.
Factored out of main.py's `_resolve_fab_icon` so every surface resolves
icons the same way.
"""

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk


def resolve_icon(candidates):
    """First candidate present in the active icon theme; last as fallback.

    `candidates` is an ordered sequence of icon names, best first. The
    final entry must be a name guaranteed present in the base theme set
    (e.g. `list-add-symbolic`) so the return value always renders.
    """
    candidates = tuple(candidates)
    display = Gdk.Display.get_default()
    if display is not None:
        theme = Gtk.IconTheme.get_for_display(display)
        for name in candidates:
            if theme.has_icon(name):
                return name
    return candidates[-1]
