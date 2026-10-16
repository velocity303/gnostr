"""NIP-46 remote signing + SignerBackend layer (spec: docs/onboarding-key-management.md P2).

This package is the SOLE home of NIP-46 in gnostr. Nothing else may speak the
remote-signing protocol. The layer it exposes:

- `SignerBackend` — the signing interface every publish path goes through
  (`sign_event`, `get_public_key`). UI code never touches a raw priv_key.
- `LocalKeyBackend` — wraps today's local priv_key signing (keyring key).
- `BunkerBackend` — NIP-46 client: `bunker://` connect, `sign_event` RPC over
  NIP-44 v2 boxes, piggybacking on NostrClient's relay ownership (it never
  opens WebSockets itself).
"""

from .backend import LocalKeyBackend, SignerBackend
from .bunker import KIND_NIP46, BunkerBackend, BunkerError, parse_bunker_uri
from .connect import new_bunker_backend, reconnect_from_session, session_blob

__all__ = [
    "SignerBackend",
    "LocalKeyBackend",
    "BunkerBackend",
    "BunkerError",
    "KIND_NIP46",
    "parse_bunker_uri",
    "new_bunker_backend",
    "reconnect_from_session",
    "session_blob",
]
