"""Follower sync (DB <-> relays reconciliation).

Pull path: a kind-3 event authored by us -> _handle_event -> save_contacts
(full replace) -> 'contacts-updated'. Push path: sync_followers() re-publishes
the DB following list as a kind-3 contact list tracked for OK acks.

Under the test gi mocks, GObject.Object is a Mock, so `class
NostrClient(GObject.Object)` evaluates to a Mock and its methods are not
introspectable. These tests therefore load the real client.py source and bind
the methods under test to a lightweight fake self — exercising the actual
client.py code without instantiating GObject.
"""

import ast
import inspect
import os
import types
from unittest.mock import MagicMock, patch

import pytest

_CLIENT_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", "gnostr", "client.py"
    )
)

_client_src = open(_CLIENT_PATH, encoding="utf-8").read()


def _unbound(method_name):
    """Extract the real function object for NostrClient.<method_name> from
    client.py source without importing it under the mocked GObject."""
    import json as _json
    import time as _time

    import gnostr.nostr_utils as _nu

    tree = ast.parse(_client_src)
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NostrClient"
    )
    fn = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name
    )
    mod = ast.Module(body=[fn], type_ignores=[])
    ns = {
        "json": _json,
        "time": _time,
        "gnostr": __import__("gnostr"),
        "GLib": types.SimpleNamespace(idle_add=lambda fn, *a: fn(*a)),
    }
    exec(compile(mod, _CLIENT_PATH, "exec"), ns)
    return ns[method_name]


class FakeClient:
    """Minimal self for NostrClient method tests. Any real NostrClient method
    not defined here is auto-bound from client.py source on first access."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        try:
            fn = _unbound(name)
        except StopIteration:
            raise AttributeError(name)
        bound = fn.__get__(self)
        object.__setattr__(self, name, bound)
        return bound

    def __init__(self):
        self.db = MagicMock()
        self.my_pubkey = "a" * 64
        self.my_privkey = "b" * 64
        self.published = []
        self.relay_log = []
        self.pending_publishes = {}
        self.relay_urls = set()
        self.seen_events = set()

    def publish(self, ev):
        self.published.append(ev)
        return True

    def _log_relay(self, line):
        self.relay_log.append(line)

    def emit(self, *a, **k):
        pass

    def _merge_relays(self, urls):
        pass


@pytest.fixture
def client():
    return FakeClient()


def _kind3(pubkey, followed, created_at=100):
    return {
        "id": "e" * 64,
        "pubkey": pubkey,
        "kind": 3,
        "created_at": created_at,
        "tags": [["p", pk] for pk in followed],
        "content": "",
    }


def _run_handle(client, ev):
    # GLib is injected into the extracted function's globals by _unbound
    # (patching "gnostr.client.GLib" is unreliable: the `gnostr.client`
    # attribute is rebound to the NostrClient Mock at package import).
    handle = _unbound("_handle_event")
    handle(client, ev)


def test_pull_reconciles_db_from_relay_kind3(client):
    """Relay kind-3 p-tags are the authority: save_contacts replaces the DB."""
    ev = _kind3(client.my_pubkey, ["c" * 64, "d" * 64])
    _run_handle(client, ev)

    client.db.save_contacts.assert_called_once_with(
        client.my_pubkey, ["c" * 64, "d" * 64]
    )
    assert any("reconciled" in line for line in client.relay_log)


def test_pull_ignores_other_authors_kind3(client):
    ev = _kind3("f" * 64, ["c" * 64])
    _run_handle(client, ev)
    client.db.save_contacts.assert_not_called()


def test_push_sync_republishes_db_list_as_kind3(client):
    """sync_followers() publishes the CURRENT DB following list as a kind-3
    with p-tags and tracks it for OK acks under the sync label."""
    import gnostr.client as client_mod

    client.db.get_following_list.return_value = ["c" * 64, "d" * 64, "e" * 64]

    def sign_stub(event, sk):
        signed = dict(event)
        signed["id"] = "s" * 64
        signed["sig"] = "0" * 64
        return signed

    sync = _unbound("sync_followers")
    # The sync path delegates to _publish_contact_list + _track_publish —
    # bind the real ones from client.py onto the fake self.
    client._publish_contact_list = _unbound("_publish_contact_list").__get__(client)
    client._track_publish = _unbound("_track_publish").__get__(client)
    real_sign = client_mod.gnostr.nostr_utils.sign_event
    client_mod.gnostr.nostr_utils.sign_event = sign_stub

    # _track_publish registers into pending_publishes with label + expiry
    def _track(event, label):
        client.pending_publishes[event["id"]] = {
            "label": label,
            "expires": 1e12,
        }

    client._track_publish = _track
    try:
        assert sync(client) is True
    finally:
        client_mod.gnostr.nostr_utils.sign_event = real_sign

    assert len(client.published) == 1
    ev = client.published[0]
    assert ev["kind"] == 3
    assert [t[1] for t in ev["tags"] if t[0] == "p"] == ["c" * 64, "d" * 64, "e" * 64]
    # tracked for OK ack under the sync label
    assert "s" * 64 in client.pending_publishes
    assert client.pending_publishes["s" * 64]["label"] == "Follow Sync"
    # DB refreshed + activity log line for the Relay Activity pane
    client.db.save_contacts.assert_called_once()
    assert any("follow-sync" in line for line in client.relay_log)


def test_push_sync_requires_keys(client):
    sync = _unbound("sync_followers")
    client.my_privkey = None
    assert sync(client) is False
    assert not client.published


def test_fetch_contacts_for_targets_given_author(client):
    """fetch_contacts_for(pubkey) subscribes for that author's kind-3 list."""
    client.subscribed = []
    client.subscribe = lambda sub_id, filters, snapshot=False: client.subscribed.append(
        (sub_id, filters)
    )
    fetch = _unbound("fetch_contacts_for")
    fetch(client, "c" * 64)
    assert len(client.subscribed) == 1
    assert client.subscribed[0][1] == {"kinds": [3], "authors": ["c" * 64], "limit": 1}


def test_fetch_contacts_delegates_to_own_pubkey(client):
    """fetch_contacts() is the own-pubkey convenience wrapper."""
    client.subscribed = []
    client.subscribe = lambda sub_id, filters, snapshot=False: client.subscribed.append(
        (sub_id, filters)
    )
    fetch = _unbound("fetch_contacts")
    fetch(client)
    assert len(client.subscribed) == 1
    assert client.subscribed[0][1]["authors"] == [client.my_pubkey]


def test_fetch_contacts_skips_without_keys(client):
    client.my_pubkey = None
    client.subscribed = []
    client.subscribe = lambda sub_id, filters, snapshot=False: client.subscribed.append(
        (sub_id, filters)
    )
    fetch = _unbound("fetch_contacts")
    fetch(client)
    assert not client.subscribed
