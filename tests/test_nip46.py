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
    kind-24133 request the client publishes, in order."""

    def __init__(self, backend, user_priv, signer_priv=None, deny=False):
        self.signer_priv, self.signer_pub = (
            nostr_utils.generate_keypair()
            if signer_priv is None
            else (signer_priv, nostr_utils.get_public_key(signer_priv))
        )
        self.user_priv = user_priv
        self.user_pub = nostr_utils.get_public_key(user_priv)
        self.backend = backend
        self.deny = deny
        self.methods = []
        self._seen = 0
        self._stop = threading.Event()
        self._thread = None

    def serve(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self):
        while not self._stop.is_set():
            events = self.backend._client.published
            while self._seen < len(events):
                self._answer(events[self._seen])
                self._seen += 1
            time.sleep(0.01)

    def _answer(self, req_ev):
        if req_ev.get("kind") != 24133:
            return
        if req_ev.get("pubkey") != self.backend.client_pub:
            return
        conv = nip44.get_conversation_key(
            bytes.fromhex(self.signer_priv), bytes.fromhex(self.backend.client_pub)
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
            [["p", self.backend.client_pub]],
        )
        self.backend.handle_event(nostr_utils.sign_event(resp, self.signer_priv))


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
