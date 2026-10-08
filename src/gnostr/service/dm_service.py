"""DM crypto + service layer (slice 2 of 5).

Sits on top of the data-layer `AbstractDMGateway` (see gateway/dm_gateway.py)
and adds:
- NIP-44 v2 conversation-key encryption/decryption, reusing the pure-Python,
  vector-tested primitives in nip44.py. We intentionally do NOT use legacy
  per-message AES-CBC (deprecated, metadata-leaking): that is dead per spec §4.
- Off-thread decryption so crypto never blocks the UI/main thread (Librem 5).
- GObject signals emitted on the main context for decrypted messages/errors so
  the UI can react without touching the decrypting worker thread.

GTK-free core: this module imports only `GObject` (for the signal base) and no
UI widgets, relay I/O, or clipboard. The heavy crypto runs in a daemon worker;
the result is marshalled back onto the GLib main loop via idle_add before any
signal fires — so signals are always emitted on the main thread even though the
decryption itself happens off-thread.

Unit-testable headless: no GTK imports at module scope, so it runs under pytest
with gi mocked (see tests/test_dm_service.py). The crypto primitives themselves
live in `gnostr.nip44` and are covered by the official NIP-44 vector suite there;
this module is exercised through real round-trips on top of them.
"""

import threading

from gi.repository import GObject


class DMService(GObject.Object):
    """Encrypt/decrypt DM content and surface decrypted messages to the UI.

    Coordinates with `AbstractDMGateway` for persistence (slice 3 wires inbound
    rumors through here) and with `NostrClient.publish` for outbound wraps
    (slice 5). This class owns only the crypto + off-thread scheduling + signals.

    The local user's keypair is injected (never read from disk here): the UI
    passes `client.my_privkey`. Conversation keys are symmetric ECDH, so a
    single `_conversation_key(peer_pubkey)` serves both inbound decrypt and
    outbound encrypt for that peer.
    """

    __gsignals__ = {
        # (event: dict, plaintext: str) — fired on the GLib main context after
        # an off-thread decrypt completes; `event` is the raw kind-14 rumor/wrap.
        "dm-decrypted": (GObject.SignalFlags.RUN_FIRST, None, (dict, str)),
        # (event: dict, sender_pubkey: str, error: Exception) — a local decryption
        # failure (bad MAC/version/size). The UI toasts this instead of crashing.
        "dm-decrypt-error": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (dict, str, Exception),
        ),
    }

    def __init__(self, gateway, our_pubkey=None, our_privkey=None, idle_add=None):
        """gateway: AbstractDMGateway. our_pubkey/our_privkey: local keypair hex.

        idle_add: optional callable(callback) that marshals a result back onto
        the GLib main loop; defaults to `GLib.idle_add` when gi is importable,
        and None in a headless/test context (where emission is then direct).
        """
        super().__init__()
        self._gateway = gateway
        self._our_pubkey = our_pubkey or getattr(gateway, "_our_pubkey_override", "")
        # `nip44` takes raw 32-byte keys; the rest of the app (client.set_keys,
        # nostr_utils.sign_event) carries keys as hex strings. Normalize here so
        # the production path (client._ensure_dm_service) does not hit
        # nip44's "bad key length" guard on every inbound DM.
        self._our_privkey = (
            self._as_key_bytes(our_privkey) if our_privkey is not None else None
        )
        # Conversation-key cache, guarded by a lock — mirrors the renderer's
        # ImageLoader `_cache`/`_cache_lock` pattern (thread-safe, bounded work).
        self._conv_cache = {}
        self._conv_lock = threading.Lock()
        self._idle_add = idle_add if idle_add is not None else self._default_idle_add()

    def _as_key_bytes(self, key):
        """Accept a 64-char hex string or raw 32 bytes; return raw 32 bytes."""
        if key is None:
            raise ValueError("key must be 32 raw bytes or 64-char hex")
        if isinstance(key, bytes):
            return key
        try:
            return bytes.fromhex(key)
        except (ValueError, TypeError):
            raise ValueError("key must be 32 raw bytes or 64-char hex") from None

    def _default_idle_add(self):
        """Return GLib.idle_add in the real app; None when gi isn't importable."""
        try:
            from gi.repository import GLib  # noqa: WPS433 (local import on purpose)

            return GLib.idle_add
        except Exception:
            return None

    def _conversation_key(self, peer_pubkey):
        """Symmetric NIP-44 conversation key for this user <-> `peer_pubkey`.

        Delegates to the vector-tested `nip44.get_conversation_key`, which does
        secp256k1 ECDH over the unhashed 32-byte x-coordinate and HKDF-extracts
        with salt 'nip44-v2'. Symmetric: conv(A,B) == conv(B,A).
        """
        privkey = self._our_privkey
        if not privkey:
            raise ValueError("no local private key loaded")
        with self._conv_lock:
            cached = self._conv_cache.get(peer_pubkey)
            if cached is not None:
                return cached
            conv_key = nip44.get_conversation_key(
                privkey, self._as_key_bytes(peer_pubkey)
            )
            self._conv_cache[peer_pubkey] = conv_key
            return conv_key

    def decrypt(self, event):
        """Decrypt an inbound DM rumor's content to plaintext.

        Derives the conversation key from (our privkey, sender pubkey) and runs
        NIP-44 v2 decryption via `nip44.decrypt`. Raises `nip44.NIP44Error` on
        any protocol violation (unknown version byte, MAC mismatch, bad
        padding/size).
        """
        sender = event.get("pubkey") or ""
        if not sender:
            raise ValueError("event has no sender pubkey")
        payload = event.get("content", "")
        conv_key = self._conversation_key(sender)
        return nip44.decrypt(payload, conv_key)

    def encrypt(self, plaintext, peer_pubkey):
        """Encrypt `plaintext` for a peer (outbound). Same conversation key the
        recipient derives from our pubkey — symmetric NIP-44 v2.
        """
        if not self._our_privkey:
            raise ValueError("no local private key loaded")
        conv_key = self._conversation_key(peer_pubkey)
        return nip44.encrypt(plaintext, conv_key)

    def schedule_decrypt(self, event, callback=None):
        """Decrypt off the UI/main thread; never run crypto on the main loop.

        Runs `decrypt` in a daemon worker so it can never block the UI (Librem 5
        perf requirement). The result is marshalled back onto the GLib main
        context via idle_add before any signal fires, so signals are always
        emitted on the main thread even though decryption happens off-thread.

        `callback(result_or_error, error)` may be supplied for tests / callers
        that don't want GObject signals; otherwise dm-decrypted/dm-decrypt-error
        are emitted.
        """

        # Runs on a worker thread, never the main loop.
        def _worker():
            try:
                plaintext = self.decrypt(event)
                if callback is not None:
                    callback(plaintext, None)
                elif self._idle_add is not None:
                    self._idle_add(self.emit, "dm-decrypted", event, plaintext)
                else:
                    # No idle marshaller available (headless): emit directly.
                    # Safe here — this branch only runs when there is no GLib
                    # main loop to marshal onto, so a direct emit can't race it.
                    self.emit("dm-decrypted", event, plaintext)
            except Exception as e:  # noqa: BLE001 - surface every failure via signal
                sender = event.get("pubkey") or ""
                if callback is not None:
                    callback(None, e)
                elif self._idle_add is not None:
                    # Marshal through idle_add exactly like the success path:
                    # dm-decrypt-error must never fire on the worker thread —
                    # the documented contract is that signals are always
                    # emitted on the GLib main context.
                    self._idle_add(self.emit, "dm-decrypt-error", event, sender, e)
                else:
                    # No idle marshaller available (headless): emit directly.
                    # Safe here — this branch only runs when there is no GLib
                    # main loop to marshal onto, so a direct emit can't race it.
                    self.emit("dm-decrypt-error", event, sender, e)

        threading.Thread(target=_worker, daemon=True).start()


# --- re-export the vector-tested NIP-44 v2 primitives ----------------------
# The crypto lives in gnostr.nip44 (official test vectors committed at
# tests/data/nip44.vectors.json); DMService is a thin orchestration layer on
# top. Importing here keeps a single source of truth and avoids duplicating —
# and getting wrong — the ChaCha20 / HKDF / ECDH math in this module.

from gnostr import nip44  # noqa: E402  (module-level, after class defs)
