"""Relay entry data model (spec relays-and-settings-v1 §A, card RELSET-2).

Tier-1 pure tests: relay_entries.py is stdlib-only (no GObject/GTK), so the
real module is imported directly — same contract as test_proxy_settings.py
for the pure half of that module. Covers entry shapes, role normalisation,
legacy migration, role routing order, NIP-65 10002 markers and the
import-from-profile merge.
"""

from gnostr import relay_entries as re_


# -- roles -------------------------------------------------------------------


def test_normalize_roles_canonical_order_and_dedupe():
    assert re_.normalize_roles(["dm", "read", "read", "write"]) == [
        "read",
        "write",
        "dm",
    ]


def test_normalize_roles_drops_unknown_and_empty():
    assert re_.normalize_roles(["read", "admin", "bogus"]) == ["read"]
    assert re_.normalize_roles(None) == []
    assert re_.normalize_roles([]) == []


def test_make_entry_defaults_and_url_cleanup():
    e = re_.make_entry("  wss://relay.example/  ")
    assert e == {
        "url": "wss://relay.example",
        "roles": ["read", "write"],
        "enabled": True,
    }
    # explicit empty roles falls back to the default pair (spec: new relays)
    assert re_.make_entry("wss://r.example", roles=[])["roles"] == ["read", "write"]
    assert re_.make_entry("wss://r.example", roles=["dm"])["roles"] == ["dm"]
    assert re_.make_entry("wss://r.example", enabled=False)["enabled"] is False


def test_normalize_entries_coerces_and_dedupes_by_url():
    raws = [
        {"url": "wss://a.example", "roles": ["write"], "enabled": False},
        {"url": "wss://a.example", "roles": ["read"]},  # dup: first wins
        {"url": "  "},  # unusable: dropped
        "not-a-dict",  # dropped
        {"url": "wss://b.example", "roles": ["bogus"]},  # roles -> default
    ]
    out = re_.normalize_entries(raws)
    assert out == [
        {"url": "wss://a.example", "roles": ["write"], "enabled": False},
        {"url": "wss://b.example", "roles": ["read", "write"], "enabled": True},
    ]


# -- legacy migration ----------------------------------------------------------


def test_entries_from_legacy_sorted_deduped_default_roles():
    entries = re_.entries_from_legacy(
        ["wss://z.example", "wss://a.example", "wss://z.example", "  "]
    )
    assert [e["url"] for e in entries] == ["wss://a.example", "wss://z.example"]
    assert all(e["roles"] == ["read", "write"] and e["enabled"] for e in entries)


# -- accessors / routing order --------------------------------------------------


def _entries():
    return re_.normalize_entries(
        [
            {"url": "wss://read-only.example", "roles": ["read"]},
            {"url": "wss://write-only.example", "roles": ["write"]},
            {"url": "wss://dm.example", "roles": ["dm"]},
            {"url": "wss://both.example", "roles": ["read", "write"]},
            {"url": "wss://parked.example", "roles": ["read", "write"], "enabled": False},
        ]
    )


def test_enabled_urls_keeps_preference_order_and_skips_parked():
    assert re_.enabled_urls(_entries()) == [
        "wss://read-only.example",
        "wss://write-only.example",
        "wss://dm.example",
        "wss://both.example",
    ]


def test_urls_for_role_order_is_preference_order():
    e = _entries()
    assert re_.urls_for_role(e, "read") == [
        "wss://read-only.example",
        "wss://both.example",
    ]
    assert re_.urls_for_role(e, "write") == [
        "wss://write-only.example",
        "wss://both.example",
    ]
    assert re_.urls_for_role(e, "dm") == ["wss://dm.example"]
    # parked entry carries read+write but must never route
    assert "wss://parked.example" not in re_.urls_for_role(e, "read")


def test_entry_for_trailing_slash_tolerant():
    e = _entries()
    assert re_.entry_for(e, "wss://both.example/") is e[3]
    assert re_.entry_for(e, "wss://missing.example") is None


# -- NIP-65 kind-10002 markers ---------------------------------------------------


def test_relay10002_tags_markers_per_roles():
    tags = re_.relay10002_tags(_entries())
    assert tags == [
        ["r", "wss://read-only.example", "read"],
        ["r", "wss://write-only.example", "write"],
        # dm counts as write (DMs are published there)
        ["r", "wss://dm.example", "write"],
        # both roles -> plain r tag
        ["r", "wss://both.example"],
    ]
    # parked entry is not advertised
    assert all("parked" not in t[1] for t in tags)


def test_relay10002_tags_empty_entries():
    assert re_.relay10002_tags([]) == []
    assert re_.relay10002_tags(None) == []


# -- import-from-profile merge ----------------------------------------------------


def test_merge_imported_appends_new_keeps_existing_roles_and_order():
    entries = _entries()
    before = [e["url"] for e in entries]
    added = re_.merge_imported(
        entries,
        ["wss://new.example", "wss://read-only.example", "http://not-ws.example", ""],
    )
    assert added == ["wss://new.example"]
    assert [e["url"] for e in entries] == before + ["wss://new.example"]
    # existing entry kept its (read-only) roles
    assert re_.entry_for(entries, "wss://read-only.example")["roles"] == ["read"]
    # new entry got default roles
    assert re_.entry_for(entries, "wss://new.example")["roles"] == ["read", "write"]
