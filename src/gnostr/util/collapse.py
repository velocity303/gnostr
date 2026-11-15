"""Safe content collapse (FEED-TRUNC).

Spammers post very long text; feed cards collapse to a character cap with a
'Show more' row. Post content is a rich widget tree (mention chips, links,
images, video), so collapsing happens on the SOURCE STRING before rendering:
content beyond the cap is never built, which also skips media build cost for
the hidden part.

`safe_truncate` is the one shared cut helper (feed cards AND the thread hero —
THREAD-TRUNC behavior must not regress): it never splits a URL, a bare domain,
a nostr: reference, or a @mention mid-token, because a half-rendered chip/link
is worse than the spam it hides. The cap is a soft target: a token that starts
inside the cap is kept whole (the result may run a little past the cap rather
than break a URL); a token that starts past it is dropped entirely.

GSettings access follows the uploader.settings_get / proxy_settings hard-
fallback contract: a dev run or missing compiled schema reads defaults and
writes return False — the UI must never crash over an unavailable schema.
"""

import re

APP_ID = "tech.livingonlinux.gnostr"

# The one default shared by feed cards and the thread hero (THREAD-TRUNC).
DEFAULT_COLLAPSE_CHARS = 500
# Schema range (feed-collapse-chars). 0 = never collapse.
COLLAPSE_MIN = 200
COLLAPSE_MAX = 5000

ELLIPSIS = "…"

# Tokens that must never be split by a cut: nostr: references (checked first —
# they also start with a scheme-less word), http(s) URLs, bare domains, and
# @mentions (NIP-08 npub/@ form and NIP-27 @pubkey hex form).
_PROTECTED_RE = re.compile(
    r"nostr:[A-Za-z0-9+/=_-]+"
    r"|https?://[^\s<>\"')\]]+"
    r"|(?:www\.)?[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)+"
    r"(?:/[^\s<>\"')\]]*)?"
    r"|@[A-Za-z0-9+.=@_-]+"
)


def normalize_cap(value):
    """Coerce a raw cap to a usable one: 0 = never collapse, anything else is
    clamped into [COLLAPSE_MIN, COLLAPSE_MAX] (a hand-edited gsettings value
    outside the schema range must not make the feed unreadable)."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return DEFAULT_COLLAPSE_CHARS
    if v <= 0:
        return 0
    return max(COLLAPSE_MIN, min(COLLAPSE_MAX, v))


def safe_truncate(content, cap):
    """Cut `content` to roughly `cap` characters at a token boundary.

    Returns the content unchanged when it already fits (or cap <= 0). When a
    cut happens the result ends with an ellipsis. Whitespace runs, words and
    protected tokens are each consumed atomically — the cut always lands on a
    boundary, never inside a URL/mention/domain.
    """
    if not content or cap <= 0 or len(content) <= cap:
        return content
    out = []
    pos = 0
    n = len(content)
    while pos < n:
        m = _PROTECTED_RE.match(content, pos)
        if m:
            chunk = m.group(0)
        elif content[pos].isspace():
            end = pos
            while end < n and content[end].isspace():
                end += 1
            chunk = content[pos:end]
        else:
            end = pos
            while end < n and not content[end].isspace():
                end += 1
            chunk = content[pos:end]
        if out and len(out[-1]) + len(chunk) > cap:
            break
        out.append(chunk)
        pos += len(chunk)
    body = "".join(out).rstrip()
    if not body:
        # Degenerate: the first token alone exceeds the cap — hard-cut rather
        # than render an empty card (nothing can be split mid-token here).
        return content[:cap] + ELLIPSIS
    return body + ELLIPSIS


# -- GSettings access (hard-fallback contract) ---------------------------------


def _settings():
    """Gio.Settings for the app schema, or None when unavailable (dev run,
    unpackaged tree, missing compiled schema)."""
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio

        return Gio.Settings.new(APP_ID)
    except Exception:
        return None


def collapse_chars():
    """Read feed-collapse-chars with a hard fallback, then normalize."""
    s = _settings()
    if s is None:
        return DEFAULT_COLLAPSE_CHARS
    try:
        return normalize_cap(s.get_int("feed-collapse-chars"))
    except Exception:
        return DEFAULT_COLLAPSE_CHARS


def set_collapse_chars(value):
    """Write feed-collapse-chars (normalized). Returns True on success;
    False when the schema is unavailable (dev run) so the UI can tell the
    user the change could not be persisted."""
    s = _settings()
    if s is None:
        return False
    try:
        s.set_int("feed-collapse-chars", normalize_cap(value))
        return True
    except Exception:
        return False
