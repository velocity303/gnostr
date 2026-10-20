# src/gnostr/markup.py
"""Markdown-lite subset shared by the composer and the renderer.

Single source of truth for the emphasis subset gnostr emits and renders
(spec: ~/Briefcase/gnostr-scopes/editor-scope-spec.md, miku's
gnostr-editor-markup-media-v1). The compose bar and the renderer both read
SUPPORTED_FEATURES, so the bar literally cannot offer what the spec forbids.

Constraints (reference-renderer regex rules, verified against the noteToHtml
lineage so posts render identically in Damus/Amethyst):
- bold ``**x**``, italic ``_x_`` (ecosystem-primary marker; never ``__x__``),
  inline code ``` `x` ```
- emphasis must hug non-whitespace, never nest, never span newlines
- markers inside URLs / nostr: refs are never touched: callers tokenize
  links FIRST and only pass plain-text runs through emphasis_to_pango
- escape FIRST (GLib.markup_escape_text), then apply markers, so user text
  containing ``<`` can never inject markup

Pure stdlib: no GTK imports, unit-testable without a display (same contract
as nostr_utils.py / nip49.py).
"""

import re

# The one place the feature set is written down. Keys are bar button ids;
# values are (open_marker, close_marker). Anything not here is a spec
# non-goal and must not appear in the compose bar.
SUPPORTED_FEATURES = {
    "bold": ("**", "**"),
    "italic": ("_", "_"),
    "code": ("`", "`"),
}

# Emphasis runs: hug non-whitespace on both sides, no newlines inside, no
# nesting (the character classes exclude the marker itself). Applied in a
# single alternation pass so code spans are consumed atomically (markers
# inside `code` stay literal). Underscore emphasis must NOT be intra-word
# (CommonMark rule): snake_case identifiers stay literal — lookarounds on
# word characters, which also covers the string edges.
_EMPHASIS_RE = re.compile(
    r"`(?P<code>[^`\n]+)`"
    r"|\*\*(?P<bold>\S(?:[^*\n]*\S)?)\*\*"
    r"|(?<!\w)_(?P<italic>\S(?:[^_\n]*\S)?)_(?!\w)"
)


def emphasis_to_pango(escaped_text):
    """Convert the markdown-lite subset to Pango markup.

    Input MUST already be markup-escaped (GLib.markup_escape_text). Returns
    the input unchanged when it contains no markers (fast path). Unknown or
    malformed markers (empty emphasis, unbalanced, spanning newlines) are
    left as literal text — never guessed at.
    """
    if not any(m in escaped_text for pair in SUPPORTED_FEATURES.values() for m in pair):
        return escaped_text

    def repl(m):
        if m.group("code") is not None:
            return f"<tt>{m.group('code')}</tt>"
        if m.group("bold") is not None:
            return f"<b>{m.group('bold')}</b>"
        return f"<i>{m.group('italic')}</i>"

    return _EMPHASIS_RE.sub(repl, escaped_text)


def apply_marker(text, start, end, feature):
    """Wrap [start,end) in the feature's markers; caret lands inside when empty.

    Returns (new_text, new_start, new_end) as buffer offsets. Toggling: if the
    selection is already wrapped exactly, the markers are stripped instead —
    the bar button reads as a toggle, matching every rich editor users know.
    """
    open_m, close_m = SUPPORTED_FEATURES[feature]
    # already wrapped exactly -> unwrap
    if (
        start >= len(open_m)
        and text[start - len(open_m) : start] == open_m
        and text[end : end + len(close_m)] == close_m
    ):
        new = text[: start - len(open_m)] + text[start:end] + text[end + len(close_m) :]
        return new, start - len(open_m), end - len(open_m)
    inner = text[start:end]
    new = text[:start] + open_m + inner + close_m + text[end:]
    if inner:
        return new, start, start + len(open_m) + len(inner) + len(close_m)
    # empty selection: caret between the markers
    return new, start + len(open_m), start + len(open_m)


def insert_at(text, pos, snippet):
    """Insert snippet at pos. Returns (new_text, caret_pos_after)."""
    new = text[:pos] + snippet + text[pos:]
    return new, pos + len(snippet)
