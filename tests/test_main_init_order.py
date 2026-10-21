"""Static guard: MainWindow.__init__ must not touch self.feed_view before it
is constructed.

Real bug this guards: the infinite-scroll phase-2 wiring connected the client's
``backfill-done`` signal to ``self.feed_view.on_backfill_done`` in the block
that wires client signals (~line 50), but ``self.feed_view = FeedView(...)`` is
assigned ~50 lines later. The mocked test suite constructs MainWindow from
parts and never ran the real __init__, so every real launch crashed with
AttributeError before the window appeared — found only on-device.

The check is a source-order scan (house pattern from test_dm_gtk422_api.py):
the first ``self.feed_view`` reference in main.py must be the assignment
itself, not a use.
"""

from pathlib import Path

MAIN_PY = Path(__file__).resolve().parent.parent / "src" / "gnostr" / "main.py"


def test_feed_view_not_used_before_assignment():
    src = MAIN_PY.read_text(encoding="utf-8")
    lines = src.splitlines()
    assign_line = use_line = None
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "self.feed_view" in stripped:
            if assign_line is None and stripped.startswith("self.feed_view ="):
                assign_line = i
            elif assign_line is None:
                use_line = i
                break
    assert assign_line is not None, "no `self.feed_view = ...` assignment found"
    assert use_line is None, (
        f"self.feed_view is used at line {use_line}, before it is assigned at "
        f"line {assign_line} — MainWindow.__init__ will crash at startup. "
        "Move the wiring below the FeedView construction."
    )
