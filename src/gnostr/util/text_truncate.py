"""Token-safe text truncation for collapsed previews (thread hero, feed cards).

A raw ``content[:limit]`` slice can land inside a ``nostr:npub…``/``nostr:note1…``
reference; ContentRenderer then can no longer match it as a mention and renders
the broken fragment literally (bug THREAD-TRUNC). ``safe_truncate`` guarantees
every nostr: token in the output is whole — a mention is either kept complete
or dropped entirely, never cut mid-token.

Pure stdlib (same contract as markup.py / nostr_utils.py): unit-testable
without a display.
"""

import re

# Single source of truth for the collapse limit (chars). thread_view and the
# upcoming FEED-TRUNCATE previews both cut through safe_truncate(default).
SAFE_TRUNCATE_LIMIT = 500

# Shape of a nostr: reference token (NIP-19 bech32 ids: npub/notes/naddr/…).
# Mirrors what ContentRenderer.LINK_REGEX keys on; a cut inside one of these
# turns a mention chip into a literal broken fragment.
_NOSTR_TOKEN = re.compile(r"nostr:[a-z0-9]+")

# How far back from the cut to look for a whitespace boundary.
_WHITESPACE_LOOKBACK = 40


def safe_truncate(content, limit=SAFE_TRUNCATE_LIMIT):
    """Truncate ``content`` to at most ``limit`` chars, never mid-nostr-token.

    - content at or under the limit is returned unchanged
    - if a ``nostr:`` token straddles the cut, back up to before it
    - prefer a nearby whitespace boundary (nicer prose; whitespace can never
      sit inside a nostr: token, so this stays token-safe)

    No ellipsis is appended — callers add their own suffix (``+ "…"``).
    """
    if len(content) <= limit:
        return content

    cut = limit
    for m in _NOSTR_TOKEN.finditer(content):
        if m.start() < cut < m.end():
            # straddles the cut — drop the whole token
            cut = m.start()
            break
        if m.start() >= cut:
            break

    # Prefer ending on a word boundary when one is close.
    start = max(0, cut - _WHITESPACE_LOOKBACK)
    window = content[start:cut]
    ws = max(window.rfind(" "), window.rfind("\n"), window.rfind("\t"))
    if ws != -1:
        cut = start + ws

    return content[:cut].rstrip()
