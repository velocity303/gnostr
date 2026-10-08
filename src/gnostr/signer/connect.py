"""bunker:// connect session logic (spec P2 connect UX).

Kept GTK-free on purpose: URI validation, connect handshake, session blob
shape, and reconnection live here so the whole connect flow is unit-testable
headless. The GTK dialog (dialogs.ConnectSignerWindow) and the startup
restore path in main.py are thin shells over these functions.

Session persistence rides the existing libsecret layer (KeyManager) — the
bunker session is NOT a private key (it holds only the bunker:// URI, the
remote-signer pubkey/relays/secret, and the user pubkey), but it is a
credential: anyone holding it can ask the signer to request signatures.
"""

import json

from .bunker import BunkerBackend, BunkerError


def session_blob(backend):
    """Serializable session record for a connected backend (libsecret value)."""
    return json.dumps(
        {
            "uri": backend._uri,
            "signer_pubkey": backend.signer_pubkey,
            "relays": backend.relays,
            "secret": backend.secret,
            "user_pubkey": backend.get_public_key(),
        }
    )


def new_bunker_backend(client, bunker_uri, on_sign_request=None, timeout=30.0):
    """Parse the URI, run the NIP-46 connect handshake, return a connected
    BunkerBackend. Raises ValueError (bad URI) or BunkerError (handshake)."""
    backend = BunkerBackend(
        client, bunker_uri, on_sign_request=on_sign_request, timeout=timeout
    )
    backend.connect()
    return backend


def reconnect_from_session(client, session, on_sign_request=None, timeout=30.0):
    """Re-establish a saved session (startup restore).

    Re-runs the full connect handshake and verifies the signer still answers
    with the SAME user pubkey that was saved — a signer that now reports a
    different identity is a spoof/misconfiguration and must not silently log
    the user into the wrong account.
    """
    uri = (session or {}).get("uri")
    if not uri:
        raise BunkerError("saved bunker session has no uri")
    backend = new_bunker_backend(
        client, uri, on_sign_request=on_sign_request, timeout=timeout
    )
    saved_pubkey = session.get("user_pubkey")
    if saved_pubkey and backend.get_public_key() != saved_pubkey:
        pubkey = backend.get_public_key()
        backend.disconnect()
        raise BunkerError(
            f"signer now reports {pubkey[:8]}…, session was saved for "
            f"{saved_pubkey[:8]}… — refusing to restore"
        )
    return backend
