"""Feed source abstraction for the infinite-scroll feeds.

Workstream: infinite scroll (phase 1). FeedModel already owns the
keyset cursor state machine (top_key/cursor/exhausted, load_first /
load_older / load_newer) — what it lacked was a pluggable *query*: it
hard-coded ``database.get_feed_following``. A FeedSource binds a feed
kind ('following' | 'global' | 'me') to one page-fetching callable so
all three feeds share the model's pagination, eviction and live-prepend
contracts.

Contract every source honors (mirrors get_feed_following, pinned by
tests/test_feed_pagination.py): keyset on (created_at, id), never
OFFSET; a page shorter than ``limit`` means the DB is exhausted.
"""
from __future__ import annotations


class FeedSource:
    """One feed kind's DB page query behind a uniform interface."""

    def __init__(self, kind, page_fn, accepts=None):
        self.kind = kind
        # page_fn(limit, before=None, after=None) -> list[event rows],
        # newest first.
        self._page_fn = page_fn
        # accepts(row) -> bool: does a LIVE event belong to this feed?
        # (subscriptions are broader than some feeds — 'me' receives
        # every kind-1 event on the global sub.)
        self._accepts = accepts or (lambda row: row.get("kind") == 1)

    def page(self, limit, before=None, after=None):
        return self._page_fn(limit, before=before, after=after)

    def accepts(self, row):
        return self._accepts(row)


def backfill_filter(kind, owner_pubkey, cursor, page_size, db):
    """NIP-01 REQ filter asking relays for history older than the window
    tail (``until`` = tail created_at), or None when there is nothing to
    ask for (no cursor, or a following feed with no contacts). Mirrors
    each feed kind's live-subscription shape so relays see one consistent
    query pattern per feed.
    """
    if cursor is None:
        return None
    base = {"kinds": [1], "until": cursor[0], "limit": page_size}
    if kind == "global":
        return base
    if kind == "me":
        return dict(base, authors=[owner_pubkey]) if owner_pubkey else None
    contacts = db.get_following_list(owner_pubkey)[:300] if db is not None else []
    if not contacts:
        return None
    return dict(base, authors=contacts)


def make_source(kind, db, owner_pubkey):
    """Build the FeedSource for a feed kind.

    kind: 'following' | 'global' | 'me'. Unknown kinds fall back to
    'following' (the historical default).

    Only the cursor the call actually needs is forwarded, so Database
    stand-ins that implement just one direction keep working.
    """
    def _following(limit, before=None, after=None):
        if after is not None:
            return db.get_feed_following(owner_pubkey, limit=limit, after=after)
        return db.get_feed_following(owner_pubkey, limit=limit, before=before)

    def _global(limit, before=None, after=None, pubkey=None):
        kwargs = {"limit": limit}
        if after is not None:
            kwargs["after"] = after
        else:
            kwargs["before"] = before
        if pubkey is not None:
            kwargs["pubkey"] = pubkey
        return db.get_feed_global(**kwargs)

    if kind == "global":
        return FeedSource("global", lambda limit, before=None, after=None:
                          _global(limit, before=before, after=after))
    if kind == "me":
        return FeedSource(
            "me",
            lambda limit, before=None, after=None: _global(
                limit, before=before, after=after, pubkey=owner_pubkey
            ),
            accepts=lambda row: row.get("kind") == 1
            and row.get("pubkey") == owner_pubkey,
        )
    return FeedSource("following", _following)
