"""THREAD-TRUNC regression: hero collapse must never cut a nostr: URI mid-token.

Bug: thread_view truncated with a raw ``content[:500]`` slice; when the cut
landed inside a ``nostr:npub…``/``nostr:note1…`` token, ContentRenderer could
no longer match it as a mention and rendered the broken fragment literally.

Pure-function tests (no GTK) for util.text_truncate.safe_truncate, plus one
source-level guard that thread_view.py no longer slices raw.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gnostr.util.text_truncate import (  # noqa: E402
    SAFE_TRUNCATE_LIMIT,
    safe_truncate,
)

# A realistic npub (63 chars after "nostr:")
MENTION = "nostr:" + "n" + "q" * 62
NOSTR_RE = re.compile(r"nostr:[a-z0-9]+")


def _content_with_mention_at(pos):
    """Long text whose single nostr: mention starts exactly at `pos`."""
    filler = "lorem ipsum dolor sit amet "
    p = filler * (pos // len(filler) + 2)
    prefix = p[: pos - 1] + " "  # length pos, ends with the space before mention
    assert len(prefix) == pos and prefix[-1] == " "
    return prefix + MENTION + " " + "tail text that must be dropped by the cut " * 5


class TestSafeTruncateContract:
    def test_short_content_returned_unchanged(self):
        text = "hello " * 10
        assert safe_truncate(text) == text

    def test_exactly_at_limit_returned_unchanged(self):
        text = "x" * SAFE_TRUNCATE_LIMIT
        assert safe_truncate(text, SAFE_TRUNCATE_LIMIT) == text

    def test_long_content_is_truncated(self):
        text = "word " * 500
        out = safe_truncate(text, SAFE_TRUNCATE_LIMIT)
        assert len(out) < len(text)
        assert len(out) <= SAFE_TRUNCATE_LIMIT


class TestMentionNeverCutMidToken:
    """The regression: a mention straddling the cut comes back whole or gone."""

    @pytest.mark.parametrize("mention_pos", range(480, 541, 5))
    def test_mention_around_limit_is_whole_or_absent(self, mention_pos):
        content = _content_with_mention_at(mention_pos)
        out = safe_truncate(content, SAFE_TRUNCATE_LIMIT)

        assert len(out) <= SAFE_TRUNCATE_LIMIT
        if MENTION in out:
            # kept — must be the complete token, not a prefix of it
            assert out.count("nostr:") == 1
        else:
            # dropped — no partial fragment may survive anywhere
            assert "nostr:" not in out

    @pytest.mark.parametrize("mention_pos", range(480, 541))
    def test_no_partial_token_at_any_offset(self, mention_pos):
        """Exhaustive sweep: no output may end inside a nostr: token."""
        content = _content_with_mention_at(mention_pos)
        out = safe_truncate(content, SAFE_TRUNCATE_LIMIT)
        # every "nostr:" in the output must be followed by a complete token
        for m in NOSTR_RE.finditer(out):
            assert m.group(0) == MENTION
        # a dangling "nostr:" prefix (token cut) is the bug — forbid it
        tail = out.rsplit("nostr:", 1)
        if len(tail) == 2:
            assert "nostr:" + tail[1] == MENTION

    def test_huge_token_at_char_zero(self):
        """Degenerate: token starts at 0 and straddles the cut — dropped."""
        content = "nostr:" + "q" * 600 + " tail"
        out = safe_truncate(content, SAFE_TRUNCATE_LIMIT)
        assert "nostr:" not in out

    def test_whitespace_boundary_preferred(self):
        words = "alpha bravo charlie delta echo " * 100
        out = safe_truncate(words, 50)
        # cut lands on a word boundary, not mid-word
        assert words[len(out)] == " "

    def test_nostr_token_before_limit_survives_intact(self):
        content = "hi " + MENTION + " " + "x" * 600
        out = safe_truncate(content, SAFE_TRUNCATE_LIMIT)
        assert MENTION in out


class TestThreadViewUsesHelper:
    """Source-level guard (house pattern): no raw [:500] slices left."""

    def test_thread_view_has_no_raw_slice_truncation(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "gnostr"
            / "ui"
            / "thread_view.py"
        ).read_text(encoding="utf-8")
        assert "[:500]" not in src
        assert src.count("safe_truncate(") >= 3
        assert "SAFE_TRUNCATE_LIMIT" in src
