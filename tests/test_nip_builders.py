"""Protocol-layer tests for the social-action NIP builders.

nostr_utils is GTK-free, so these tests import the real module and verify the
exact tag layout each NIP mandates. These are the contract for the protocol
elements — if a relay/client rejects an action, it's a tag-structure bug here.
"""

import json

from gnostr import nostr_utils


def test_build_reaction_event_nip25():
    ev = nostr_utils.build_reaction_event("pk", "evt", "author", "+")
    assert ev["kind"] == 7
    assert ev["content"] == "+"
    assert ev["tags"] == [["e", "evt"], ["p", "author"]]
    assert ev["pubkey"] == "pk"
    assert "created_at" in ev


def test_build_reaction_undo_uses_minus():
    ev = nostr_utils.build_reaction_event("pk", "evt", "author", "-")
    assert ev["content"] == "-"


def test_build_repost_event_nip18():
    original = {"id": "o1", "kind": 1, "content": "hi", "tags": [],
                "pubkey": "apk", "created_at": 1, "sig": "s"}
    ev = nostr_utils.build_repost_event("pk", "o1", "apk", 1, original)
    assert ev["kind"] == 6
    assert ev["tags"] == [["k", "1"], ["e", "o1"], ["p", "apk"]]
    embedded = json.loads(ev["content"])
    assert embedded["id"] == "o1" and embedded["sig"] == "s"


def test_build_reply_event_nip01_root_first_parent_last():
    ev = nostr_utils.build_reply_event("pk", "my reply",
                                       "R1", "rp", "P1", "pp")
    assert ev["kind"] == 1
    etags = [t for t in ev["tags"] if t[0] == "e"]
    ptags = [t for t in ev["tags"] if t[0] == "p"]
    # First e-tag = thread root, last e-tag = direct parent.
    assert etags == [["e", "R1"], ["e", "P1"]]
    # p-tags name root + parent authors.
    assert ["p", "rp"] in ptags and ["p", "pp"] in ptags
