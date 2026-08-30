"""FeedModel — widget-agnostic feed data window (workstream #0, task 2).

Owns the ordered event-id list of the active feed, the keyset cursor
state, the end-of-DB sentinel, the bounded-window eviction policy, and
the live-prepend buffer. Knows no widgets and no GTK: the only
dependency is a Database-like object (``get_feed_following``), which is
why it is unit-testable headless.

Contracts (TRACKING workstream #0):

* Cursor pagination — ``cursor`` is always ``(created_at, id)`` of the
  OLDEST event in the window; ``load_older()`` passes it as ``before=``
  to fetch the next page strictly older. No OFFSET, no re-fetching.
* Anchor stability — eviction happens at the NEWEST end (top of the
  window) as the user scrolls down and loads older posts; the oldest
  end (the cursor anchor) is never touched by ``evict_newest``.
* End-of-DB sentinel — a page shorter than ``page_size`` (or an empty
  page) sets ``exhausted``; further ``load_older()`` calls are no-ops.
* Widget state from data — the model stores ids only; the view
  hydrates rows via ``database.get_event(id)`` (the DB is the source
  of truth). The window is a bounded view, not a cache.
* Live events — unseen events are buffered in ``new_ids`` (the "N new
  posts" pill); ``flush_new_at_top()`` prepends them when the UI
  decides to apply them (user at top / explicit jump).
"""


class FeedModel:
    """Ordered feed window + keyset cursor. GTK-free, unit-testable."""

    def __init__(self, owner_pubkey, database, page_size=50, max_window=50):
        self.owner = owner_pubkey
        self._db = database
        self.page_size = page_size
        self.max_window = max_window
        self.event_ids = []  # newest first
        self.new_ids = []  # buffered live events, newest first
        self._rows = {}  # id -> row dict (for created_at lookups)
        self.cursor = None  # (created_at, id) of oldest in-window event
        self.exhausted = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def reset(self):
        """Clear window, cursor, buffer, and sentinel. (No fetch.)"""
        self.event_ids = []
        self.new_ids = []
        self._rows = {}
        self.cursor = None
        self.exhausted = True

    def load_first(self):
        """Fetch and install the first (newest) page.

        Any buffered live events are implicitly superseded by the fresh
        page. Returns the window's event ids, newest first.
        """
        self.reset()
        rows = self._db.get_feed_following(
            self.owner, limit=self.page_size, before=None
        )
        for row in rows:
            self._append_window(row)
        self._bound_window()
        self.exhausted = len(rows) < self.page_size
        self.cursor = self._tail_cursor()
        return list(self.event_ids)

    def load_older(self):
        """Fetch the next page, strictly older than the window tail.

        Returns the ids actually appended, or None while ``exhausted``.
        """
        if self.exhausted or self.cursor is None:
            return None
        rows = self._db.get_feed_following(
            self.owner, limit=self.page_size, before=self.cursor
        )
        added = []
        for row in rows:
            if self._append_window(row):
                added.append(row["id"])
        self._bound_window()
        if len(rows) < self.page_size:
            self.exhausted = True
        self.cursor = self._tail_cursor()
        return added

    # ------------------------------------------------------------------
    # Live events
    # ------------------------------------------------------------------

    def prepend_new(self, events):
        """Buffer newly arrived events (list of event dicts).

        Unseen events are inserted into ``new_ids`` in newest-first
        order (matching the DB's ``created_at DESC, id DESC``) and are
        NOT added to the visible window yet — the "N new posts" pill.
        Dedups against the window (a live event already rendered is not
        buffered). Returns the ids actually buffered.
        """
        inserted = []
        for event in events:
            if self._insert(event):
                inserted.append(event["id"])
        return inserted

    def _insert(self, row):
        """Insert one live event row into the ``new_ids`` buffer.

        Returns True when the id was new (buffered), False when already
        known (window or buffer) — the dedup that prevents a live
        double-render of a DB-backfilled event.
        """
        if row["id"] in self._rows:
            return False
        self._rows[row["id"]] = row
        pos = len(self.new_ids)
        for i, eid in enumerate(self.new_ids):
            if self._is_newer(row, self._rows[eid]):
                pos = i
                break
        self.new_ids.insert(pos, row["id"])
        return True

    def flush_new_at_top(self):
        """Prepend the buffered live events to the window.

        Called by the view when the user is at the top (or tapped the
        "N new posts" pill). The cursor (oldest in window) is
        unchanged — the new posts are all newer. Returns the ids now at
        the window top (newest first).
        """
        if not self.new_ids:
            return []
        top = list(self.new_ids)
        self.event_ids = self.new_ids + self.event_ids
        self.new_ids = []
        return top

    # ------------------------------------------------------------------
    # Eviction
    # ------------------------------------------------------------------

    def evict_newest(self, n):
        """Drop up to ``n`` newest (window-top) events.

        Bounding for the scroll-down-to-load-older flow: the newest
        posts scroll off the top and are released. The cursor (oldest
        end) is untouched, so ``load_older()`` continues unchanged.
        Returns the evicted ids (newest first).
        """
        n = min(n, len(self.event_ids))
        evicted = self.event_ids[:n] if n else []
        if n:
            self.event_ids = self.event_ids[n:]
            for eid in evicted:
                self._rows.pop(eid, None)
        if not self.event_ids:
            # Fully evicted: nothing left to anchor a next page from.
            self.cursor = None
            self.exhausted = True
        return evicted

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _append_window(self, row):
        """Append a page row to the window (pages arrive already sorted
        newest-first and strictly ordered, so a tail append preserves
        order). Dedups. Returns True when the id was new."""
        if row["id"] in self._rows:
            return False
        self._rows[row["id"]] = row
        self.event_ids.append(row["id"])
        return True

    def _bound_window(self):
        """Keep the window at most ``max_window`` rows by evicting the
        newest (top) overflow."""
        overflow = len(self.event_ids) - self.max_window
        if overflow > 0:
            self.evict_newest(overflow)

    @staticmethod
    def _is_newer(a, b):
        """True when event ``a`` sorts before ``b`` newest-first
        (``created_at DESC, id DESC`` — mirrors the DB feed query)."""
        if a["created_at"] != b["created_at"]:
            return a["created_at"] > b["created_at"]
        return a["id"] > b["id"]

    def _tail_cursor(self):
        """(created_at, id) of the oldest in-window event, or None."""
        if not self.event_ids:
            return None
        row = self._rows[self.event_ids[-1]]
        return (row["created_at"], row["id"])
