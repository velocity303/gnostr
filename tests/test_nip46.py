"""NIP-46 client round-trip against a local mock signer (spec P2 acceptance).

No network, no GTK. A FakeClient captures the kind-24133 request events the
BunkerBackend publishes through it; MockSigner holds the remote signer's
keypair, decrypts each NIP-44 v2 box, performs the RPC, and feeds the
encrypted response back through BunkerBackend.handle_event exactly the way
NostrClient._handle_event routes kind 24133 in production.
"""

import json
import threading
import time

import pytest

from gnostr import nip44, nostr_utils
from gnostr.signer import BunkerBackend, BunkerError, parse_bunker_uri

RELAY = "wss://relay.example"


class FakeClient:
    """Transport double: captures publishes, records relay/REQ calls."""

    def __init__(self):
        self.published = []
        self.relays = []
        self.requests = []
        self._lock = threading.Lock()

    def publish(self, event):
        with self._lock:
            self.published.append(event)

    def add_relay(self, url):
        self.relays.append(url)

    def request_once(self, sub_id, filters):
        self.requests.append((sub_id, filters))


class MockSigner:
    """The remote signer. `serve()` runs a daemon loop that answers every new
    kind-24133 request the client publishes, in order.

    It can serve several backends at once (connect helpers build their own
    backend instances): `follow(backend)` registers another one, and each
    request is routed by its `p` tag — the client pubkey the request was
    addressed to — so the ECDH conversation always matches the requester.
    """

    def __init__(self, backend, user_priv, signer_priv=None, deny=False):
        self.signer_priv, self.signer_pub = (
            nostr_utils.generate_keypair()
            if signer_priv is None
            else (signer_priv, nostr_utils.get_public_key(signer_priv))
        )
        self.user_priv = user_priv
        self.user_pub = nostr_utils.get_public_key(user_priv)
        self.backends = {}
        self.follow(backend)
        self.deny = deny
        self.methods = []
        self._seen = {}
        self._handled = set()
        self._stop = threading.Event()
        self._thread = None

    def follow(self, backend):
        """Also answer requests published by this backend."""
        self.backends[backend.client_pub] = backend

    def serve(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self):
        while not self._stop.is_set():
            for backend in list(self.backends.values()):
                events = backend._client.published
                seen = self._seen.get(id(backend._client), 0)
                while seen < len(events):
                    self._answer(events[seen])
                    seen += 1
                self._seen[id(backend._client)] = seen
            time.sleep(0.01)

    def _answer(self, req_ev):
        if req_ev.get("kind") != 24133:
            return
        # A kind-24133 REQUEST is p-tagged to the signer; the requester is the
        # event's pubkey (the client keypair). Several backends can share one
        # FakeClient in tests, so dedupe on the request id.
        if req_ev.get("id") in self._handled:
            return
        client_pub = req_ev.get("pubkey")
        backend = self.backends.get(client_pub)
        if backend is None:
            return
        self._handled.add(req_ev.get("id"))
        conv = nip44.get_conversation_key(
            bytes.fromhex(self.signer_priv), bytes.fromhex(client_pub)
        )
        try:
            req = json.loads(nip44.decrypt(req_ev["content"], conv))
        except Exception:
            return
        method = req.get("method")
        self.methods.append(method)
        result, error = None, None
        if method == "connect":
            result = req["params"][1]  # echo the secret per NIP-46
        elif method == "get_public_key":
            result = self.user_pub
        elif method == "sign_event":
            if self.deny:
                error = "user rejected"
            else:
                unsigned = json.loads(req["params"][0])
                unsigned["pubkey"] = self.user_pub
                unsigned["id"] = nostr_utils.compute_event_id(unsigned)
                result = json.dumps(
                    nostr_utils.sign_event(unsigned, self.user_priv)
                )
        elif method == "logout":
            result = "ok"
        resp = nostr_utils.build_event(
            self.signer_pub,
            24133,
            nip44.encrypt(
                json.dumps({"id": req["id"], "result": result, "error": error}), conv
            ),
            [["p", client_pub]],
        )
        backend.handle_event(nostr_utils.sign_event(resp, self.signer_priv))


def _pair(secret="s3cr3t", deny=False, **backend_kw):
    """Backend + signer sharing one keypair so the ECDH conversation matches."""
    signer_priv, signer_pub = nostr_utils.generate_keypair()
    client = FakeClient()
    backend = BunkerBackend(
        client,
        f"bunker://{signer_pub}?relay={RELAY}&secret={secret}",
        timeout=3.0,
        **backend_kw,
    )
    signer = MockSigner(
        backend, nostr_utils.generate_keypair()[0], signer_priv, deny=deny
    )
    return client, backend, signer


# --- bunker:// URI parsing ----------------------------------------------------


def test_parse_bunker_uri():
    signer = "a" * 64
    pk, relays, secret = parse_bunker_uri(
        f"bunker://{signer}?relay=wss://r1.example&relay=wss://r2.example&secret=tok"
    )
    assert pk == signer
    assert relays == ["wss://r1.example", "wss://r2.example"]
    assert secret == "tok"


@pytest.mark.parametrize(
    "bad",
    ["", "nostr://" + "a" * 64, "bunker://nothex?relay=wss://r", "bunker://" + "a" * 64],
)
def test_parse_bunker_uri_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_bunker_uri(bad)


# --- connect handshake --------------------------------------------------------


def test_connect_handshake_sets_user_pubkey():
    client, backend, signer = _pair()
    signer.serve()
    pk = backend.connect()
    signer.stop()
    assert pk == signer.user_pub
    assert backend.get_public_key() == signer.user_pub
    assert backend.connected
    # connect then get_public_key (NIP-46: re-call after connect)
    assert signer.methods[:2] == ["connect", "get_public_key"]
    # signer relay joined through the client's relay ownership, never a raw WS
    assert client.relays == [RELAY]
    # request_once (not subscribe) so the feed's live subscription is untouched
    assert client.requests[0][1]["kinds"] == [24133]
    assert client.requests[0][1]["authors"] == [backend.signer_pubkey]
    assert client.requests[0][1]["#p"] == [backend.client_pub]


def test_connect_secret_mismatch_is_rejected():
    client, backend, signer = _pair(secret="right")
    signer.serve()

    # Force mismatch: the signer answers every request with the wrong secret.
    def answer_with_wrong_secret(req_ev):
        if req_ev.get("kind") != 24133:
            return
        conv = nip44.get_conversation_key(
            bytes.fromhex(signer.signer_priv), bytes.fromhex(backend.client_pub)
        )
        req = json.loads(nip44.decrypt(req_ev["content"], conv))
        resp = nostr_utils.build_event(
            signer.signer_pub,
            24133,
            nip44.encrypt(
                json.dumps({"id": req["id"], "result": "WRONG", "error": None}),
                conv,
            ),
            [["p", backend.client_pub]],
        )
        backend.handle_event(nostr_utils.sign_event(resp, signer.signer_priv))

    signer._answer = answer_with_wrong_secret
    with pytest.raises(BunkerError):
        backend.connect()
    assert not backend.connected
    signer.stop()


# --- sign_event round-trip ----------------------------------------------------


def test_sign_event_round_trip():
    client, backend, signer = _pair()
    signer.serve()
    backend.connect()
    event = {
        "kind": 1,
        "content": "hello from the bunker",
        "tags": [["e", "c" * 64]],
        "created_at": int(time.time()),
    }
    signed = backend.sign_event(event)
    signer.stop()
    assert signed is not None
    assert signed["pubkey"] == signer.user_pub
    assert signed["kind"] == 1
    assert signed["content"] == "hello from the bunker"
    assert signed["tags"] == [["e", "c" * 64]]
    # id recomputed over the signer's fields, sig is a 64-byte BIP-340 pair
    assert signed["id"] == nostr_utils.compute_event_id(signed)
    assert len(signed["sig"]) == 128
    # the request itself was published through the client transport
    kinds = [e["kind"] for e in client.published]
    assert kinds.count(24133) >= 3  # connect + get_public_key + sign_event


def test_sign_event_survives_signer_error():
    client, backend, signer = _pair(deny=True)
    signer.serve()
    backend.connect()
    signed = backend.sign_event({"kind": 1, "content": "x", "tags": []})
    signer.stop()
    assert signed is None


def test_sign_event_rejects_wrong_pubkey_answer():
    """A signer that answers with someone else's identity must be discarded."""
    client, backend, signer = _pair()
    signer.serve()
    backend.connect()
    # Point the backend at a different user pubkey than the signer answers as.
    backend._user_pubkey = "d" * 64
    signed = backend.sign_event({"kind": 1, "content": "x", "tags": []})
    signer.stop()
    assert signed is None


# --- local guards -------------------------------------------------------------


def test_local_approval_callback_denies_without_rpc():
    client, backend, signer = _pair()
    backend.on_sign_request = lambda kind, preview: False
    backend.connected = True
    backend._user_pubkey = "b" * 64
    assert backend.sign_event({"kind": 1, "content": "x", "tags": []}) is None
    # No NIP-46 request ever left the client.
    assert client.published == []


def test_rate_guard_blocks_burst():
    _, backend, _ = _pair()
    backend.connected = True
    backend._user_pubkey = "b" * 64
    backend.on_sign_request = None
    results = [backend._rate_ok() for _ in range(10)]
    limit = backend._rate_limit
    assert results[:limit] == [True] * limit
    assert all(r is False for r in results[limit:])


def test_rate_guard_window_recovers():
    _, backend, _ = _pair(rate_limit=2, rate_window=0.2)
    assert backend._rate_ok() and backend._rate_ok()
    assert not backend._rate_ok()
    time.sleep(0.25)
    assert backend._rate_ok()


def test_disconnected_backend_refuses_to_sign():
    _, backend, _ = _pair()
    backend.on_sign_request = None
    assert backend.sign_event({"kind": 1, "content": "x", "tags": []}) is None


def test_disconnect_destroys_client_keypair():
    client, backend, signer = _pair()
    signer.serve()
    backend.connect()
    backend.disconnect()
    signer.stop()
    assert "logout" in signer.methods
    assert backend.client_priv is None
    assert backend._conv is None
    assert not backend.connected


def test_handle_event_ignores_foreign_authors_and_garbage():
    _, backend, _ = _pair()
    stranger_priv, stranger_pub = nostr_utils.generate_keypair()
    ev = nostr_utils.build_event(
        stranger_pub, 24133, "garbage-not-a-box", [["p", backend.client_pub]]
    )
    backend.handle_event(nostr_utils.sign_event(ev, stranger_priv))  # no raise

    # Correct author but p-tagged to a different client → ignored.
    other = nostr_utils.generate_keypair()[1]
    conv = nip44.get_conversation_key(
        bytes.fromhex(backend.signer_pubkey), bytes.fromhex(other)
    )
    ev2 = nostr_utils.build_event(
        backend.signer_pubkey,
        24133,
        nip44.encrypt(json.dumps({"id": "x", "result": "y"}), conv),
        [["p", other]],
    )
    backend.handle_event(nostr_utils.sign_event(ev2, backend.signer_pubkey))


# --- connect session logic (P2 connect UX, issue #15) -------------------------


def _capture_backends(monkeypatch, signer):
    """connect.py builds its own BunkerBackend instances; capture each one and
    register it with the mock signer so it answers that client's requests."""
    from gnostr.signer import connect as connect_mod

    built = []
    real = connect_mod.BunkerBackend

    class Capturing(real):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            built.append(self)
            signer.follow(self)

    monkeypatch.setattr(connect_mod, "BunkerBackend", Capturing)
    return built


def test_new_bunker_backend_connects_and_blobs(monkeypatch):
    from gnostr.signer import new_bunker_backend, session_blob

    client, proto, signer = _pair()
    signer.serve()
    built = _capture_backends(monkeypatch, signer)
    uri = f"bunker://{proto.signer_pubkey}?relay={RELAY}&secret=s3cr3t"
    backend = new_bunker_backend(client, uri)
    signer.stop()
    assert backend.connected
    assert backend.get_public_key() == signer.user_pub
    blob = json.loads(session_blob(backend))
    assert blob["uri"] == uri
    assert blob["signer_pubkey"] == proto.signer_pubkey
    assert blob["relays"] == [RELAY]
    assert blob["secret"] == "s3cr3t"
    assert blob["user_pubkey"] == signer.user_pub


def test_reconnect_from_session_restores_same_identity(monkeypatch):
    from gnostr.signer import new_bunker_backend, reconnect_from_session, session_blob

    client, proto, signer = _pair()
    signer.serve()
    built = _capture_backends(monkeypatch, signer)
    backend = new_bunker_backend(
        client, f"bunker://{proto.signer_pubkey}?relay={RELAY}&secret=s3cr3t"
    )
    session = json.loads(session_blob(backend))
    backend.disconnect()

    # A fresh client + backend re-runs the handshake from the saved session.
    client2 = FakeClient()
    restored = reconnect_from_session(client2, session)
    signer.stop()
    assert restored.get_public_key() == signer.user_pub
    assert restored._client is client2


def test_reconnect_refuses_changed_signer_identity(monkeypatch):
    from gnostr.signer import reconnect_from_session

    client, backend, signer = _pair()
    signer.serve()
    backend.connect()
    _capture_backends(monkeypatch, signer)  # the reconnect builds a fresh backend
    session = {
        "uri": backend._uri,
        "signer_pubkey": backend.signer_pubkey,
        "relays": backend.relays,
        "secret": backend.secret,
        "user_pubkey": "b" * 64,  # session saved for a DIFFERENT identity
    }
    signer.stop()
    with pytest.raises(BunkerError):
        reconnect_from_session(client, session)


@pytest.mark.parametrize("session", [None, {}, {"nope": 1}])
def test_reconnect_requires_uri(session):
    from gnostr.signer import reconnect_from_session

    with pytest.raises(BunkerError):
        reconnect_from_session(FakeClient(), session)


def test_new_bunker_backend_rejects_garbage_uri():
    from gnostr.signer import new_bunker_backend

    with pytest.raises(ValueError):
        new_bunker_backend(FakeClient(), "not-a-bunker-uri")
