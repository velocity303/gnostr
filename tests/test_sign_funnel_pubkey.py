"""Signing funnel contract: NostrClient._sign must fill a missing pubkey.

compute_event_id() indexes event["pubkey"] directly, so any builder that
forgets it (blossom BUD-01 auth events arrive without one) makes signing
raise KeyError -> sign_event returns None -> uploads die with "Could not
sign upload request". The funnel is the single choke point, so it patches
the pubkey from my_pubkey before delegating. Same FakeClient/ast pattern as
test_relay_retry_publish.py (GObject is a Mock under the test gi mocks).
"""

import ast
import os
import types
from unittest.mock import MagicMock

import pytest

_CLIENT_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", "gnostr", "client.py"
    )
)

_client_src = open(_CLIENT_PATH, encoding="utf-8").read()


def _unbound(method_name):
    tree = ast.parse(_client_src)
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NostrClient"
    )
    fn = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name
    )
    mod = ast.Module(body=[fn], type_ignores=[])
    ns = {
        "json": __import__("json"),
        "time": __import__("time"),
        "gnostr": __import__("gnostr"),
        "GLib": types.SimpleNamespace(idle_add=lambda fn, *a: fn(*a)),
    }
    exec(compile(mod, _CLIENT_PATH, "exec"), ns)
    return ns[method_name]


_sign = _unbound("_sign")


class FakeClient:
    def __init__(self, priv, pub):
        self.my_privkey = priv
        self.my_pubkey = pub


@pytest.fixture()
def keys():
    import gnostr.nostr_utils as nu

    priv = "a" * 64
    return priv, nu.get_public_key(priv)


def test_sign_fills_missing_pubkey(keys):
    priv, pub = keys
    c = FakeClient(priv, pub)
    ev = {"kind": 24242, "created_at": 1, "tags": [], "content": ""}
    signed = _sign(c, ev)
    assert signed is not None, "signing a pubkey-less event must not fail"
    assert signed["pubkey"] == pub
    assert signed["sig"]


def test_sign_keeps_existing_pubkey(keys):
    priv, pub = keys
    c = FakeClient(priv, pub)
    ev = {"pubkey": pub, "kind": 1, "created_at": 1, "tags": [], "content": "hi"}
    signed = _sign(c, ev)
    assert signed is not None and signed["pubkey"] == pub


def test_sign_does_not_mutate_caller_dict(keys):
    priv, pub = keys
    c = FakeClient(priv, pub)
    ev = {"kind": 24242, "created_at": 1, "tags": [], "content": ""}
    _sign(c, ev)
    assert "pubkey" not in ev, "funnel must copy, not mutate the caller's event"


def test_sign_returns_none_without_key(keys):
    _, pub = keys
    c = FakeClient(None, pub)
    c.signer = MagicMock()
    c.signer.sign_event.return_value = {"pubkey": pub}
    ev = {"kind": 1, "created_at": 1, "tags": [], "content": ""}
    assert _sign(c, ev) is not None  # backend path unaffected
