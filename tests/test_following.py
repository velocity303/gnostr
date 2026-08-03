"""Tests for the follow/unfollow toggle (Task 9) — the DB persistence layer.

Covers database.set_following / get_following_list round-trip: adding a follow,
toggling it off, and confirming a no-op doesn't error.

Note: the real `gnostr.client` is intentionally NOT imported here. Under conftest's
`gi.repository` mocking, classes inheriting the mocked `GObject.Object` (NostrClient)
resolve to Mocks, so importing the real client is unsupported — which is why
tests/AGENTS.md mandates "Service tests mock both Database and Client." The kind-3
publishing path (`client.publish_contacts`) is verified by code review and the
on-device Flatpak check described in the UX-overhaul plan.
"""

# The test env mocks `gi.repository.GLib` (see conftest.py); import it here so we
# patch the exact mock object that database.py binds at import time.
from gi.repository import GLib  # noqa: E402 - resolves to the mock in conftest


def _patch_glib_dirs(monkeypatch, tmp_path):
    # Database resolves GLib.get_user_data_dir.
    monkeypatch.setattr(GLib, "get_user_data_dir", lambda: str(tmp_path / "data"))


def test_set_following_and_get_following_list_roundtrip(tmp_path, monkeypatch):
    _patch_glib_dirs(monkeypatch, tmp_path)
    from gnostr.database import Database

    db = Database()
    db.set_following("owner1", "pk_a", True)
    db.set_following("owner1", "pk_b", True)
    db.set_following("owner2", "pk_c", True)

    assert set(db.get_following_list("owner1")) == {"pk_a", "pk_b"}
    assert db.get_following_list("owner2") == ["pk_c"]
    # other owners are isolated
    assert db.get_following_list("nobody") == []

    # toggle off removes the row; toggling off an absent key is a harmless no-op
    db.set_following("owner1", "pk_a", False)
    assert db.get_following_list("owner1") == ["pk_b"]
    db.set_following("owner1", "pk_missing", False)
    assert db.get_following_list("owner1") == ["pk_b"]
