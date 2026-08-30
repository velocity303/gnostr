"""FeedModel — widget-agnostic feed data window (workstream #0, task 2).

Owns the ordered event-id list of the active feed, the keyset cursor
state, the end-of-DB sentinel, the bounded-window eviction policy, and
the live-prepend buffer. Knows no widgets and no GTK: the only
dependency is a Database-like object (``get_feed_following``), which is
why it is unit-testable headless.

Contracts (TRACKING workstream #0):

* Cursor pagination — the window is bounded between a NEWEST key
  (``top_key``) and an OLDEST key (``cursor``), both ``(created_at,
  id)``. ``load_older()`` passes ``cursor`` as ``before=`` to fetch the
  next page strictly older; ``load_newer()`` passes ``top_key`` as
  ``after=`` to re-pull the evicted top when the user scrolls back up.
  No OFFSET, no re-fetching.
* Viewport-relative eviction — the window stays at most ``max_window``
  rows by evicting whichever end is away from the viewport. Scrolling
  down (loading older) evicts the NEWEST end; scrolling up (re-pulling)
  evicts the OLDEST end. ``evict_newest`` / ``evict_oldest`` expose both.
* End-of-DB sentinel — a page shorter than ``page_size`` (or an empty
  page) sets ``exhausted``; further ``load_older()`` calls are no-ops.
  ``at_top`` is the symmetric flag for the newest end.
* Widget state from data — the model stores ids + (created_at) only; the
  view hydrates full rows via ``database.get_event(id)`` (the DB is the
  source of truth). The window is a bounded view, not a cache.
* Live events — unseen events are buffered in ``new_ids`` (the "N new
  posts" pill); ``flush_new_at_top()`` prepends them when the UI decides
  to apply them (user at top / explicit jump).
"""


class FeedModel:
    """Ordered feed window + bidirectional keyset cursors. GTK-free,
    unit-testable."""

    def __init__(self, owner_pubkey, database, page_size=50, max_window=50):
        self.owner = owner_pubkey
        self._db = database
        self.page_size = page_size
        self.max_window = max_window
        self.event_ids = []  # newest first
        self.new_ids = []  # buffered live events, newest first
        self._rows = {}  # id -> row dict (for created_at lookups)
        self.cursor = None  # (created_at, id) of OLDEST in-window event
        self.top_key = None  # (created_at, id) of NEWEST in-window event
        self.exhausted = False  # reached the oldest end (end-of-DB)
        self.at_top = False  # the window's top is the newest known event

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def reset(self):
        """Clear window, cursors, buffer, and sentinels. (No fetch.)"""
        self.event_ids = []
        self.new_ids = []
        self._rows = {}
        self.cursor = None
        self.top_key = None
        self.exhausted = True
        self.at_top = False

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
        # Initial page: the viewport is at the TOP (newest), so bounding
        # evicts the OLDEST overflow — the newest posts are always shown.
        # We hold the newest known posts either way, so we are at the top.
        self._bound_window(end="oldest")
        self.exhausted = len(rows) < self.page_size
        self.cursor = self._tail_cursor()
        self.top_key = self._head_cursor()
        self.at_top = True
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
        if len(rows) < self.page_size:
            self.exhausted = True
        self.cursor = self._tail_cursor()
        # The top is unchanged by appending older posts, but the window
        # grew at the bottom — bound by evicting the NEWEST overflow (the
        # rows scrolled off the top when the user is near the bottom).
        self._bound_window(end="newest")
        self.top_key = self._head_cursor()
        return added

    def load_newer(self):
        """Re-pull the page NEWER than the current window top.

        Used for the sliding window's re-pull: after the user scrolled
        down and the newest posts were evicted off the top, scrolling
        back up calls this to re-fetch them from the DB. Returns the ids
        prepended (newest first), or None when we're already at the top.
        """
        if self.at_top or self.top_key is None:
            return None
        rows = self._db.get_feed_following(
            self.owner, limit=self.page_size, after=self.top_key
        )
        added = []
        # rows arrive newest-first; prepend reversed so the newest lands at
        # index 0 LAST (insert(0) each time).
        for row in reversed(rows):
            if self._prepend_window(row):
                added.insert(0, row["id"])
        if len(rows) < self.page_size:
            # A short page means we reached the absolute newest.
            self.at_top = True
        self.top_key = self._head_cursor()
        # The window grew at the top — bound by evicting the OLDEST overflow
        # (the rows scrolled off the bottom when the user is near the top).
        self._bound_window(end="oldest")
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
        "N new posts" pill). The cursor (oldest) is unchanged — the new
        posts are all newer. Returns the ids now at the window top
        (newest first).
        """
        if not self.new_ids:
            return []
        top = list(self.new_ids)
        self.event_ids = self.new_ids + self.event_ids
        self.new_ids = []
        self.top_key = self._head_cursor()
        # The live events are the newest known — we're at the top now.
        self.at_top = True
        self._bound_window(end="oldest")
        self.cursor = self._tail_cursor()
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
            self.top_key = self._head_cursor()
            # We dropped the newest end — there are newer events to re-pull.
            self.at_top = False
            if not self.event_ids:
                self.cursor = None
                self.top_key = None
                self.exhausted = True
        return evicted

    def evict_oldest(self, n):
        """Drop up to ``n`` oldest (window-tail) events.

        Bounding for the scroll-up-to-re-pull flow: the oldest posts
        scroll off the bottom and are released. The top_key (newest
        end) is untouched, so ``load_newer()`` continues unchanged.
        Returns the evicted ids (oldest first).
        """
        n = min(n, len(self.event_ids))
        evicted = self.event_ids[-n:] if n else []
        if n:
            self.event_ids = self.event_ids[:-n]
            for eid in evicted:
                self._rows.pop(eid, None)
            self.cursor = self._tail_cursor()
            # We dropped the oldest end — there are older events to
            # load, so the feed is no longer exhausted.
            if self.cursor is not None:
                self.exhausted = False
            if not self.event_ids:
                self.top_key = None
                self.cursor = None
                self.at_top = False
        return evicted

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _append_window(self, row):
        """Append a page row to the window tail (pages arrive sorted
        newest-first and strictly ordered, so a tail append preserves
        order). Dedups. Returns True when the id was new."""
        if row["id"] in self._rows:
            return False
        self._rows[row["id"]] = row
        self.event_ids.append(row["id"])
        return True

    def _prepend_window(self, row):
        """Prepend a re-pulled row to the window top (re-pulled pages
        arrive newest-first, strictly newer than the current top — the
        caller must pass them reversed so the newest lands at index 0
        LAST). Dedups. Returns True when the id was new.

        A row that was BUFFERED as a live event (in ``new_ids``) is
        promoted here at its correct position instead of being dropped:
        the re-pull page is ordered, so the live event lands exactly
        where it belongs and leaves the buffer.
        """
        if row["id"] in self._rows:
            if row["id"] in self.new_ids:
                self.new_ids.remove(row["id"])
                self._insert_row_sorted(row)
            return False
        self._rows[row["id"]] = row
        self.event_ids.insert(0, row["id"])
        return True

    def _insert_row_sorted(self, row):
        """Insert a row into the window at its sorted (newest-first)
        position. Used when a buffered live event is re-pulled from the
        DB: the re-pull page is strictly newer than the current top, so
        the event lands at the top, and the buffer loses it."""
        pos = 0
        for i, eid in enumerate(self.event_ids):
            if self._is_newer(row, self._rows[eid]):
                pos = i
                break
        else:
            pos = len(self.event_ids)
        self.event_ids.insert(pos, row["id"])

    def _bound_window(self, end="newest"):
        """Keep the window at most ``max_window`` rows by evicting the
        overflow from ``end`` ("newest" or "oldest") — the end away from
        the viewport for the active scroll direction."""
        overflow = len(self.event_ids) - self.max_window
        if overflow <= 0:
            return
        if end == "oldest":
            self.evict_oldest(overflow)
        else:
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

    def _head_cursor(self):
        """(created_at, id) of the newest in-window event, or None."""
        if not self.event_ids:
            return None
        row = self._rows[self.event_ids[0]]
        return (row["created_at"], row["id"])

    @property
    def newest_id(self):
        """event id at the newest end of the window, or None."""
        return self.event_ids[0] if self.event_ids else None

    @property
    def oldest_id(self):
        """event id at the oldest end of the window, or None."""
        return self.event_ids[-1] if self.event_ids else None

    def can_load_newer(self):
        """True when there are (evicted) newer posts to re-pull."""
        return (not self.at_top) and self.top_key is not None
