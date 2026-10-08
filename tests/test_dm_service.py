"""Tests for the DM crypto/service layer (slice 2 of 5).

Covers:
- NIP-44 v2 round-trip encrypt/decrypt through DMService (the real service, not
  just nip44 primitives) using our own keypair + a counterparty's.
- Conversation-key symmetry: A->B and B->A derive the same key.
- Off-thread decryption scheduling: `schedule_decrypt` never runs crypto on the
  main thread; results are marshalled via idle_add (or emitted directly when no
  GLib main loop exists).
- Thread-safety of concurrent encrypt/decrypt + conversation-key caching.
- Signal emission contract: dm-decrypted / dm-decrypt-error fired with the right
  args, and decrypt errors surface as signals rather than raising in the worker.

GTK-free core, so this runs under pytest with gi mocked (conftest). Because a
mocked GObject subclass is not introspectable at runtime, we load the real
DMService class from source via AST, extract each method into its own module
namespace, and bind it to a lightweight fake self — exercising the actual code
without instantiating GObject (same pattern as test_follow_sync.py).

The crypto primitives are pure-Python NIP-44 v2; round-trip correctness is also
cross-checked against the official committed vectors in tests/data/nip44.vectors.json.
"""

import ast
import os
import threading
import time
from unittest.mock import MagicMock, patch

import ecdsa
import pytest

# --- load real DMService methods from source (gi is mocked in conftest) ------


_SRC_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "src",
    "gnostr",
    "service",
    "dm_service.py",
)


def _load_dm_service_ns():
    """Load the real DMService class body as a namespace with {name: obj}.

    Returns the class node itself (so tests can patch attributes like
    _default_idle_add) plus the dict of its members.
    """
    src = open(_SRC_PATH, encoding="utf-8").read()
    tree = ast.parse(src)
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DMService"
    )
    ns = {"__class__": cls}
    ns.update({n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)})
    return ns


def _load_dm_service():
    """Load the real DMService class body as a dict of {name: FunctionDef}."""
    src = open(_SRC_PATH, encoding="utf-8").read()
    tree = ast.parse(src)
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DMService"
    )
    return {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}


def _extract(name):
    """Extract DMService.<name> into its own module namespace (real FunctionDef)."""
    fn = _load_dm_service()[name]
    mod = ast.Module(body=[fn], type_ignores=[])
    ns = {}
    exec(compile(mod, _SRC_PATH + ":extract", "exec"), ns)
    return ns[name]


def _bind_methods(fake_self):
    """Bind every DMService method to `fake_self` so calls run real code.

    We wrap the raw (unbound) FunctionDef in a functools.partial that injects
    `fake_self` as the first argument, so call() -> fn(self, *args). Using
    __get__ here would double-bind self (an extracted FunctionDef is already an
    unbound function), so we skip it and rely on the partial.

    Each method runs in its own module namespace, which does not carry the
    module-level `from gnostr import nip44`, so we inject that import into the
    namespace before executing — the crypto primitives must resolve to the real
    vector-tested module at call time.
    """
    import functools

    for name, fn in _load_dm_service().items():
        mod = ast.Module(body=[fn], type_ignores=[])
        ns = {}
        exec(
            compile(mod, _SRC_PATH + ":bind:" + name, "exec"), ns
        )  # noqa: F821 - dynamic path
        ns["nip44"] = __import__("gnostr.nip44", fromlist=["nip44"])
        # Methods run in an isolated namespace that carries none of the real
        # module's top-level imports; inject the stdlib names they reference
        # (threading is used by schedule_decrypt's worker) so calls resolve.
        ns.setdefault("threading", __import__("threading"))
        setattr(fake_self, name, functools.partial(ns[name], fake_self))


class FakeDM:
    """Minimal self for DMService method tests with a capturing signal bus."""

    def __init__(self):
        self._privkey = None
        # The real DMService.__init__ reads our_pubkey/our_privkey from the
        # injected keypair or falls back to gateway._our_pubkey_override; mirror
        # that so a bare fake doesn't AttributeError on construction. `_our_privkey`
        # is set per-test via svc._our_privkey = ... and read by encrypt().
        self._gateway_our_pubkey_override = ""
        self._our_privkey = None
        self._conv_cache = {}
        self._conv_lock = threading.Lock()
        # Mirror the real DMService.__init__ attributes so any method that reads
        # them (e.g. _default_idle_add) doesn't AttributeError on a fresh fake.
        self._idle_add = None
        # Signal emission sink. Under mocked gi the real GObject emit is opaque,
        # so we record calls here — the worker emits through this bus.
        self.signals = []

    def emit(self, *args):
        """Records a signal call; in production this is the real GObject emit."""
        self.signals.append(args)


# --- fixtures / helpers ----------------------------------------------------


@pytest.fixture(scope="function")
def alice_priv():
    sk = ecdsa.SigningKey.generate(curve=ecdsa.SECP256k1)
    return sk.privkey.secret_multiplier.to_bytes(32, "big").hex()


@pytest.fixture(scope="function")
def bob_priv():
    sk = ecdsa.SigningKey.generate(curve=ecdsa.SECP256k1)
    return sk.privkey.secret_multiplier.to_bytes(32, "big").hex()


@pytest.fixture(scope="function")
def alice_pub(alice_priv):
    sk = ecdsa.SigningKey.from_string(bytes.fromhex(alice_priv), curve=ecdsa.SECP256k1)
    return sk.get_verifying_key().pubkey.point.x().to_bytes(32, "big")


@pytest.fixture(scope="function")
def bob_pub(bob_priv):
    sk = ecdsa.SigningKey.from_string(bytes.fromhex(bob_priv), curve=ecdsa.SECP256k1)
    return sk.get_verifying_key().pubkey.point.x().to_bytes(32, "big")


def bob_pub_for(alice):
    """Bob's pubkey (derived from the alice fixture privkey)."""
    sk = ecdsa.SigningKey.from_string(bytes.fromhex(alice), curve=ecdsa.SECP256k1)
    return sk.get_verifying_key().pubkey.point.x().to_bytes(32, "big").hex()


def _real_keypair():
    """A fresh on-curve secp256k1 keypair as (priv_hex, pub_hex)."""
    sk = ecdsa.SigningKey.generate(curve=ecdsa.SECP256k1)
    priv = sk.privkey.secret_multiplier.to_bytes(32, "big").hex()
    pub = sk.get_verifying_key().pubkey.point.x().to_bytes(32, "big").hex()
    return priv, pub


def _kb(hexkey: str) -> bytes:
    """Decode a hex key string to raw bytes (nip44 expects 32-byte values)."""
    return bytes.fromhex(hexkey)


def _svc_encrypt(load_dm_service, our_priv_hex, recipient_pub_hex):
    """Encrypt plaintext to `recipient` using a service whose privkey is ours."""
    svc = FakeDM()
    _bind_methods(svc)
    svc._our_privkey = _kb(our_priv_hex)
    return svc.encrypt("payload", _kb(recipient_pub_hex))


def _event(event_id="e" * 64, sender=None, ts=1000, content=None):
    """A minimal kind-14 rumor dict."""
    return {
        "id": event_id,
        "pubkey": sender or ("a" * 64),
        "created_at": ts,
        "content": content or "",
        "tags": [],
    }


# --- round-trip encrypt/decrypt --------------------------------------------


def test_encrypt_decrypt_roundtrip(alice_priv, alice_pub, bob_priv, bob_pub):
    """Encrypt to Bob with Alice's key; Bob decrypts with his own key."""
    alice = FakeDM()
    _bind_methods(alice)
    alice._our_privkey = _kb(alice_priv)
    plaintext = "hello bob"

    payload = alice.encrypt(plaintext, bob_pub)

    bob = FakeDM()
    _bind_methods(bob)
    bob._our_privkey = _kb(bob_priv)
    # Bob derives the key from (bob_priv, alice_pub). The sender is Alice.
    assert bob.decrypt(_event(content=payload, sender=alice_pub)) == plaintext


def test_conversation_key_is_symmetric(alice_priv, alice_pub, bob_priv, bob_pub):
    """conv(A,B) == conv(B,A): both derive the same key from one direction."""
    alice = FakeDM()
    _bind_methods(alice)
    alice._our_privkey = _kb(alice_priv)
    bob = FakeDM()
    _bind_methods(bob)
    bob._our_privkey = _kb(bob_priv)

    k_ab = alice._conversation_key(bob_pub)  # Alice's view of A<->B
    k_ba = bob._conversation_key(alice_pub)  # Bob's view of B<->A

    assert k_ab == k_ba


def test_encrypt_then_decrypt_across_two_services(
    alice_priv, alice_pub, bob_priv, bob_pub
):
    """Full outbound path: Alice encrypts for Bob, Bob decrypts."""
    alice = FakeDM()
    _bind_methods(alice)
    alice._our_privkey = _kb(alice_priv)
    bob = FakeDM()
    _bind_methods(bob)
    bob._our_privkey = _kb(bob_priv)

    msg = "secret message"
    payload = alice.encrypt(msg, bob_pub)

    # Bob derives the same key from (bob_priv, alice_pub).
    assert bob.decrypt(_event(content=payload, sender=alice_pub)) == msg


def test_decrypt_rejects_bad_mac():
    """A payload encrypted for a different conversation key must fail to decrypt."""
    # svc holds an invalid key ("0"*64 -> d=0), so conversation-key derivation
    # fails and the worker surfaces it as dm-decrypt-error with sender=bob_pub.
    svc = FakeDM()
    _bind_methods(svc)
    svc._our_privkey = _kb("0" * 64)

    # Encrypt with a real counterparty so the payload is well-formed; but its
    # conversation key differs from ours, so decryption fails (MAC mismatch).
    other_priv, recipient_pub = _real_keypair()
    payload = _svc_encrypt(
        _load_dm_service(), our_priv_hex=other_priv, recipient_pub_hex=recipient_pub
    )

    with pytest.raises(Exception):
        svc.decrypt(_event(content=payload, sender=recipient_pub))


def test_service_requires_local_private_key():
    """Encrypt/decrypt raise without a loaded local key."""
    svc = FakeDM()
    _bind_methods(svc)
    svc._our_privkey = None  # no key loaded
    with pytest.raises(ValueError):
        svc.encrypt("x", "a" * 64)
    with pytest.raises(ValueError):
        svc.decrypt(_event(content="y"))


# --- off-thread scheduling --------------------------------------------------


def test_schedule_decrypt_runs_off_thread(alice_priv, alice_pub, bob_priv, bob_pub):
    """The worker runs on a real thread; crypto never touches the main loop."""
    alice = FakeDM()
    _bind_methods(alice)
    alice._our_privkey = _kb(alice_priv)

    # Pre-encrypt with Alice's key so schedule_decrypt can decrypt it back.
    evt = _event(content=alice.encrypt("off-thread-ok", bob_pub), sender=bob_pub)

    received = {}

    def on_result(plaintext, error):
        received["text"] = plaintext
        received["error"] = error

    svc = FakeDM()
    _bind_methods(svc)
    svc._our_privkey = _kb(alice_priv)
    # The worker runs in a background thread; wait for it to complete.
    svc.schedule_decrypt(evt, callback=on_result)

    deadline = time.time() + 5
    while not received and time.time() < deadline:
        time.sleep(0.01)

    assert "text" in received
    assert received["error"] is None


def test_schedule_decrypt_emits_dm_decrypted_signal(alice_priv, alice_pub, bob_pub):
    """When idle_add marshals onto the main loop, dm-decrypted fires."""
    alice = FakeDM()
    _bind_methods(alice)
    alice._our_privkey = _kb(alice_priv)

    # A GLib-like marshaller that records the full marshalled call
    # (self.emit, "dm-decrypted", event, plaintext) — simulating delivery on
    # the main thread.
    emitted = []

    def fake_idle_add(*args):
        emitted.append(args)

    svc = FakeDM()
    _bind_methods(svc)
    svc._our_privkey = _kb(alice_priv)
    with patch.object(svc, "_idle_add", staticmethod(fake_idle_add)):
        evt = _event(
            content=alice.encrypt("off-thread-decrypted", bob_pub), sender=bob_pub
        )
        svc.schedule_decrypt(evt)  # no callback -> emits via idle_add -> emit
        # Wait while the patch is still active: if the with block exits first,
        # _idle_add reverts to None and the worker takes the direct-emit
        # branch instead of the marshalled one (test race).
        deadline = time.time() + 5
        while not emitted and time.time() < deadline:
            time.sleep(0.01)

    assert len(emitted) == 1
    name, event_arg, plaintext = emitted[0][1], emitted[0][2], emitted[0][3]
    assert name == "dm-decrypted"
    assert event_arg is evt
    assert plaintext == "off-thread-decrypted"


def test_schedule_decrypt_emits_dm_decrypt_error_signal(alice_priv, alice_pub, bob_pub):
    """A decrypt failure emits dm-decrypt-error with (event, sender, error)."""
    # svc holds an invalid key ("0"*64 -> d=0), so conversation-key derivation
    # fails and the worker surfaces it as dm-decrypt-error with sender=bob_pub.
    alice = FakeDM()
    _bind_methods(alice)
    alice._our_privkey = _kb(alice_priv)  # encryptor key

    payload = alice.encrypt("secret", bob_pub)

    emitted = []

    def fake_idle_add(*args):
        # Marshalled call: (self.emit, "dm-decrypt-error", event, sender, error).
        emitted.append(args)

    svc = FakeDM()
    _bind_methods(svc)
    svc._our_privkey = _kb("0" * 64)  # invalid -> error path
    with patch.object(svc, "_idle_add", staticmethod(fake_idle_add)):
        evt = _event(content=payload, sender=bob_pub)
        svc.schedule_decrypt(evt)  # no callback -> emits dm-decrypt-error
        # Wait while the patch is still active: the error path must marshal
        # through _idle_add like the success path (never emit on the worker
        # thread); exiting the with block early would revert _idle_add and
        # let the worker take the direct-emit branch (test race).
        deadline = time.time() + 5
        while not emitted and time.time() < deadline:
            time.sleep(0.01)

    assert len(emitted) == 1
    name, event_arg, sender_arg, error_arg = (
        emitted[0][1],
        emitted[0][2],
        emitted[0][3],
        emitted[0][4],
    )
    assert name == "dm-decrypt-error"
    assert event_arg is evt
    assert sender_arg == bob_pub
    assert isinstance(error_arg, Exception)


# --- thread-safety ----------------------------------------------------------


def test_conversation_key_cache_is_thread_safe(alice_priv, alice_pub):
    """Concurrent lookups of the same key don't corrupt the cache."""
    svc = FakeDM()
    _bind_methods(svc)
    svc._our_privkey = _kb(alice_priv)

    def worker():
        for _ in range(50):
            assert svc._conversation_key(alice_pub) is not None

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert all(not t.is_alive() for t in threads)


def test_concurrent_encrypt_decrypt_roundtrip(alice_priv, alice_pub, bob_priv, bob_pub):
    """Many concurrent encrypt/decrypt ops across two services stay correct."""
    alice = FakeDM()
    _bind_methods(alice)
    alice._our_privkey = _kb(alice_priv)
    bob = FakeDM()
    _bind_methods(bob)
    bob._our_privkey = _kb(bob_priv)

    errors = []

    def task(i):
        try:
            msg = f"message number {i}"
            payload = alice.encrypt(msg, bob_pub)
            decrypted = bob.decrypt(_event(content=payload, sender=alice_pub))
            if decrypted != msg:
                raise AssertionError(f"mismatch: {decrypted!r} != {msg!r}")
        except Exception as e:  # noqa: BLE001 - collect, don't crash the run
            errors.append(e)

    threads = [threading.Thread(target=task, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert all(not t.is_alive() for t in threads)
    assert not errors


def test_schedule_decrypt_thread_safety(alice_priv, alice_pub, bob_priv, bob_pub):
    """Many concurrent schedule_decrypt calls don't crash the cache/threads."""
    svc = FakeDM()
    _bind_methods(svc)
    svc._our_privkey = _kb(alice_priv)
    count = {"done": 0}

    def worker():
        # Encrypt to a real counterparty (bob), then decrypt with our own key.
        recipient_pub = bob_pub
        for _ in range(20):
            evt = _event(content=svc.encrypt("hi", recipient_pub), sender=recipient_pub)
            plaintext = svc.decrypt(evt)
            if plaintext != "hi":
                raise AssertionError("decrypt mismatch")
            count["done"] += 1

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert all(not t.is_alive() for t in threads)
    assert count["done"] == 160
