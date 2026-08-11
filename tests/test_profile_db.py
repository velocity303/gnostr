"""Tests for the profiles table — full NIP-01/NIP-24 metadata persistence.

Follows test_nostr_types.py / test_following.py pattern: import the mocked
GLib and patch get_user_data_dir to a temp dir so the real Database class
runs against an isolated SQLite file.
"""

from gi.repository import GLib  # noqa: E402 - resolves to the conftest mock


def _patch_glib_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(GLib, "get_user_data_dir", lambda: str(tmp_path / "data"))


def _make_db(tmp_path, monkeypatch):
    _patch_glib_dirs(monkeypatch, tmp_path)
    from gnostr.database import Database

    return Database()


def test_save_and_get_profile_round_trips_all_fields(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    content = (
        '{"name": "bob", "display_name": "Bob Builder", "about": "bio here", '
        '"picture": "https://x/pic.png", "website": "https://bob.com", '
        '"banner": "https://x/banner.png", "nip05": "bob@example.com", '
        '"lud16": "bob@example.com", "bot": true, '
        '"birthday": {"year": 1990, "month": 5, "day": 1}}'
    )
    db.save_profile("pk1", content, 100)

    prof = db.get_profile("pk1")
    assert prof is not None
    assert prof["name"] == "bob"
    assert prof["display_name"] == "Bob Builder"
    assert prof["about"] == "bio here"
    assert prof["picture"] == "https://x/pic.png"
    assert prof["website"] == "https://bob.com"
    assert prof["banner"] == "https://x/banner.png"
    assert prof["nip05"] == "bob@example.com"
    assert prof["lud16"] == "bob@example.com"
    assert prof["bot"] == 1
    assert prof["birthday"] == '{"year": 1990, "month": 5, "day": 1}'
    assert prof["raw_json"] == content


def test_save_profile_missing_optional_fields_defaults(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    db.save_profile("pk2", '{"name": "alice"}', 200)

    prof = db.get_profile("pk2")
    assert prof is not None
    assert prof["name"] == "alice"
    assert prof["website"] == ""
    assert prof["banner"] == ""
    assert prof["nip05"] == ""
    assert prof["lud16"] == ""
    assert prof["bot"] == 0
    assert prof["birthday"] == ""


def test_save_profile_ignores_deprecated_fields(tmp_path, monkeypatch):
    """NIP-24: displayName/username are deprecated — prefer display_name/name."""
    db = _make_db(tmp_path, monkeypatch)
    db.save_profile(
        "pk3",
        '{"displayName": "Old", "username": "olduser", "name": "newuser"}',
        300,
    )

    prof = db.get_profile("pk3")
    assert prof["name"] == "newuser"  # username ignored
    assert prof["display_name"] == ""  # displayName ignored


def test_get_profile_missing_returns_none(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    assert db.get_profile("nobody") is None


def test_migration_adds_columns_to_existing_table(tmp_path, monkeypatch):
    """A pre-existing profiles table (old schema) gets the new columns added."""
    _patch_glib_dirs(monkeypatch, tmp_path)
    import sqlite3

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = sqlite3.connect(data_dir / "gnostr.db")
    conn.execute(
        "CREATE TABLE profiles (pubkey TEXT PRIMARY KEY, name TEXT, "
        "display_name TEXT, about TEXT, picture TEXT, updated_at INTEGER)"
    )
    conn.commit()
    conn.close()

    from gnostr.database import Database

    db = Database()
    db.save_profile("pk9", '{"name": "migrated", "banner": "https://b/b.png"}', 1)
    prof = db.get_profile("pk9")
    assert prof is not None
    assert prof["name"] == "migrated"
    assert prof["banner"] == "https://b/b.png"
    assert prof["website"] == ""
