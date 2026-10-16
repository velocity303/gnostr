"""Recipient picker decision-table tests (spec Phase A2, headless).

Under the conftest gi mocks, `class DmRecipientView(Gtk.Box)` evaluates
against a Mock base (same situation as DMInboxView in
test_dm_ui_wiring.py), so the decision logic is extracted from source
via ast and bound to a lightweight fake self. Covers:

- paste-resolver table: hex / npub / nostr:npub / nprofile resolve;
  nsec (maps to OUR key) is rejected; NIP-05 addresses and junk do not
  resolve (P2 not landed);
- filter: case-insensitive substring on display name OR npub;
- own-key pinning: resolving to the owner's own pubkey pins nothing.

Real rendering is verified offscreen in tests/test_dm_recipient_gtk_real.py.
"""

import ast
import os
from unittest.mock import MagicMock

import pytest

from gnostr import nostr_utils
from gnostr.nostr_utils import (
    bech32_encode,
    convertbits,
    generate_keypair,
    hex_to_nsec,
    hex_to_npub,
)

_UI_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "src",
        "gnostr",
        "ui",
        "dm_recipient_view.py",
    )
)


def _class_method(method_name, extra_ns=None):
    """Extract the real DmRecipientView.<method> from source, unbound."""
    src = open(_UI_PATH, encoding="utf-8").read()
    tree = ast.parse(src)
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "DmRecipientView"
    )
    fn = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name
    )
    ns = {"Adw": MagicMock(), "Gtk": MagicMock(), "Pango": MagicMock()}
    if extra_ns:
        ns.update(extra_ns)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), _UI_PATH, "exec"), ns)
    return ns[method_name]


_NS = {"resolve_profile_identifier": nostr_utils.resolve_profile_identifier}
_resolve_paste = _class_method("_resolve_paste", _NS)
_matches = _class_method("_matches")

# Deterministic keys (fixed secrets so npub vectors are stable).
OWN_PRIV = "11" * 32
OWN_PUB = nostr_utils.get_public_key(OWN_PRIV)
PEER_PRIV = "22" * 32
PEER_PUB = nostr_utils.get_public_key(PEER_PRIV)


class FakeView:
    """Fake self: only the attributes the decision methods touch."""

    def __init__(self, own_pk=OWN_PUB):
        self.main_window = MagicMock()
        self.main_window.pub_key = own_pk


def _nprofile(pubkey_hex):
    """Encode a minimal nprofile (TLV type 0 = pubkey), same scheme the
    decoder in nostr_utils expects."""
    tlv = bytes([0, 32]) + bytes.fromhex(pubkey_hex)
    return bech32_encode("nprofile", convertbits(tlv, 8, 5))


# ---------------------------------------------------------------------------
# paste-resolver decision table
# ---------------------------------------------------------------------------


def test_paste_hex_resolves():
    view = FakeView()
    assert _resolve_paste(view, PEER_PUB) == PEER_PUB


def test_paste_npub_resolves():
    view = FakeView()
    assert _resolve_paste(view, hex_to_npub(PEER_PUB)) == PEER_PUB


def test_paste_nostr_prefixed_npub_resolves():
    view = FakeView()
    assert _resolve_paste(view, "nostr:" + hex_to_npub(PEER_PUB)) == PEER_PUB


def test_paste_nprofile_resolves():
    view = FakeView()
    assert _resolve_paste(view, _nprofile(PEER_PUB)) == PEER_PUB


def test_paste_own_nsec_rejected():
    """nsec resolves to OUR OWN pubkey (resolver maps it); the picker must
    drop it — DM-to-self is a no-op."""
    view = FakeView()
    assert _resolve_paste(view, hex_to_nsec(OWN_PRIV)) is None


def test_paste_own_npub_rejected():
    view = FakeView()
    assert _resolve_paste(view, hex_to_npub(OWN_PUB)) is None


def test_paste_nip05_not_resolved_yet():
    """NIP-05 addresses resolve only when P2 (lookup_nip05) lands."""
    view = FakeView()
    assert _resolve_paste(view, "alice@example.com") is None


def test_paste_junk_rejected():
    view = FakeView()
    for junk in ("", "   ", "hello world", "npub1short", "00" * 31):
        assert _resolve_paste(view, junk) is None


# ---------------------------------------------------------------------------
# follows filter
# ---------------------------------------------------------------------------


def test_filter_matches_name_case_insensitive():
    """_render lowercases the query before calling _matches (caller
    contract); case-insensitivity is exercised end-to-end in the real-GTK
    harness. Here: substring on name."""
    view = FakeView()
    entry = (PEER_PUB, "Alice Liddell", hex_to_npub(PEER_PUB))
    assert _matches(view, entry, "alice")
    assert _matches(view, entry, "liddell")
    assert not _matches(view, entry, "bob")


def test_filter_matches_npub_prefix():
    view = FakeView()
    npub = hex_to_npub(PEER_PUB)
    entry = (PEER_PUB, "Alice", npub)
    assert _matches(view, entry, npub[5:13].lower())
    assert not _matches(view, entry, "zzzz")


def test_generated_keypairs_are_distinct():
    """Guard the fixture itself: own != peer."""
    assert OWN_PUB != PEER_PUB
    assert generate_keypair()[0] != generate_keypair()[0]
