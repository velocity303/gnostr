"""Tests for NIP-19/NIP-21 nostr type decoding + addressable-event lookup.

nostr_utils is GTK-free, so these tests import the real module and verify the
decode layer against the authoritative spec examples (NIP-19 / NIP-21). The
database test follows test_following.py's pattern: import the mocked GLib and
patch get_user_data_dir to a temp dir.
"""

from gnostr import nostr_utils

# Real examples from the NIP-19 / NIP-21 specs (nostr-protocol/nips master).
# These bech32 strings must stay exact — noqa: E501 (line length).
NPROFILE = "nprofile1qqsrhuxx8l9ex335q7he0f09aej04zpazpl0ne2cgukyawd24mayt8gpp4mhxue69uhhytnc9e3k7mgpz4mhxue69uhkg6nzv9ejuumpv34kytnrdaksjlyr9p"  # noqa: E501
NADDR = "naddr1qqyrzwrxvc6ngvfkqyghwumn8ghj7enfv96x5ctx9e3k7mgzyqalp33lewf5vdq847t6te0wvnags0gs0mu72kz8938tn24wlfze6qcyqqq823cph95ag"  # noqa: E501
NEVENT = "nevent1qqstna2yrezu5wghjvswqqculvvwxsrcvu7uc0f78gan4xqhvz49d9spr3mhxue69uhkummnw3ez6un9d3shjtn4de6x2argwghx6egpr4mhxue69uhkummnw3ez6ur4vgh8wetvd3hhyer9wghxuet5nxnepm"  # noqa: E501


def test_decode_nprofile_nip19_example():
    pubkey, relays = nostr_utils.decode_nprofile(NPROFILE)
    assert pubkey == "3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d"
    assert relays == ["wss://r.x.com", "wss://djbas.sadkb.com"]


def test_decode_naddr_nip21_example():
    kind, pubkey, d_tag, relays = nostr_utils.decode_naddr(NADDR)
    assert kind == 30023  # long-form article (NIP-23)
    assert pubkey == "3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d"
    assert d_tag == "18ff5416"
    assert relays == ["wss://fiatjaf.com"]


def test_decode_nevent_full_nip21_example():
    event_id, relays, author, kind = nostr_utils.decode_nevent_full(NEVENT)
    assert (
        event_id == "b9f5441e45ca39179320e0031cfb18e34078673dcc3d3e3a3b3a981760aa5696"
    )
    assert relays == ["wss://nostr-relay.untethr.me", "wss://nostr-pub.wellorder.net"]
    # author/kind are optional per NIP-19 — absent here
    assert author == ""
    assert kind is None


def test_decode_rejects_wrong_hrp():
    assert nostr_utils.decode_naddr(NPROFILE) is None
    assert nostr_utils.decode_nevent_full(NPROFILE) is None
    assert nostr_utils.decode_nprofile(NADDR) is None


def test_is_nostr_reference_covers_all_types():
    for uri in (
        "nostr:nevent1...",
        "nostr:nprofile1...",
        "nostr:naddr1...",
        "nostr:nrelay1...",
        "nostr:note1...",
        "nostr:npub1...",
    ):
        assert nostr_utils.is_nostr_reference(uri)
    # nsec is NOT a nostr: URI reference (NIP-21 excludes it)
    assert not nostr_utils.is_nostr_reference("nostr:nsec1...")


# --- database.get_event_by_a (addressable coordinate lookup) ---

from gi.repository import GLib  # noqa: E402 - resolves to the conftest mock


def _patch_glib_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(GLib, "get_user_data_dir", lambda: str(tmp_path / "data"))


def _save_addressable(db, eid, kind, pubkey, d_tag, content, created_at):
    db.save_event(
        {
            "id": eid,
            "pubkey": pubkey,
            "kind": kind,
            "content": content,
            "tags": [["d", d_tag]],
            "created_at": created_at,
            "sig": "s",
        }
    )


def test_get_event_by_a_returns_latest_for_coordinate(tmp_path, monkeypatch):
    _patch_glib_dirs(monkeypatch, tmp_path)
    from gnostr.database import Database

    db = Database()
    _save_addressable(db, "a1", 30023, "pk1", "article-1", "v1", 1)
    _save_addressable(db, "a2", 30023, "pk1", "article-1", "v2", 2)  # newer
    _save_addressable(db, "a3", 30023, "pk1", "article-2", "other", 3)

    ev = db.get_event_by_a(30023, "pk1", "article-1")
    assert ev is not None
    assert ev["id"] == "a2"  # newest for the coordinate
    assert ev["content"] == "v2"

    # different d-tag / kind / author do not match
    assert db.get_event_by_a(30023, "pk1", "article-2")["id"] == "a3"
    assert db.get_event_by_a(30023, "pk1", "missing") is None
    assert db.get_event_by_a(1, "pk1", "article-1") is None
    assert db.get_event_by_a(30023, "pk2", "article-1") is None


def test_get_event_by_a_empty_db_returns_none(tmp_path, monkeypatch):
    _patch_glib_dirs(monkeypatch, tmp_path)
    from gnostr.database import Database

    db = Database()
    assert db.get_event_by_a(30023, "pk1", "article-1") is None
