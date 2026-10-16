"""SignerBackend interface + LocalKeyBackend (spec P2).

`client.py`/`main.py` sign exclusively through a SignerBackend. Callers never
learn whether the key is local or remote — that is the whole point of the
interface: swapping in BunkerBackend must not touch any publish path.

GTK-free and import-safe at package-init time (no gi, no client import).
"""

from gnostr import nostr_utils


class SignerBackend:
    """Signing interface: build an unsigned event dict, hand it over, get a
    signed copy back. `is_local` tells the DM crypto layer whether a raw
    private key is available for NIP-44 ECDH (remote signers keep their key).
    """

    is_local = False

    def sign_event(self, event):
        """Return a signed copy of the unsigned `event` dict, or None on failure."""
        raise NotImplementedError

    def get_public_key(self):
        """Return the hex pubkey this backend signs as."""
        raise NotImplementedError


class LocalKeyBackend(SignerBackend):
    """Wraps today's priv_key signing (keyring-loaded nsec). Behaviour-identical
    to the old direct `nostr_utils.sign_event(event, my_privkey)` calls."""

    is_local = True

    def __init__(self, priv_hex):
        self._priv = priv_hex
        self._pub = nostr_utils.get_public_key(priv_hex)

    def sign_event(self, event):
        return nostr_utils.sign_event(dict(event), self._priv)

    def get_public_key(self):
        return self._pub

    @property
    def local_privkey(self):
        """Raw hex key — exposed only for the at-rest DM crypto path
        (nip44 conversation keys), never for signing outside this backend."""
        return self._priv
