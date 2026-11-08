import gi

import json
import threading

# FIX: Namespace is 'Secret', not 'Libsecret'
gi.require_version("Secret", "1")
from gi.repository import Secret, GLib

# Schema defines what "kind" of secret this is.
SECRET_SCHEMA = Secret.Schema.new(
    "tech.livingonlinux.gnostr.Schema",
    Secret.SchemaFlags.NONE,
    {
        "application": Secret.SchemaAttributeType.STRING,
    },
)


class KeyManager:
    @staticmethod
    def save_key(nsec):
        """Saves the private key (nsec) securely to the keyring."""
        attributes = {"application": "gnostr"}

        try:
            Secret.password_store_sync(
                SECRET_SCHEMA,
                attributes,
                Secret.COLLECTION_DEFAULT,
                "Gnostr Private Key",
                nsec,
                None,
            )
            print("✅ Key saved securely.")
            return True
        except GLib.Error as e:
            print(f"❌ Failed to save key: {e}")
            return False

    @staticmethod
    def load_key():
        """Loads the private key from the keyring."""
        attributes = {"application": "gnostr"}

        try:
            nsec = Secret.password_lookup_sync(SECRET_SCHEMA, attributes, None)
            return nsec
        except GLib.Error as e:
            print(f"❌ Failed to load key: {e}")
            return None

    @staticmethod
    def delete_key():
        """Removes the private key (Log out)."""
        attributes = {"application": "gnostr"}
        try:
            Secret.password_clear_sync(SECRET_SCHEMA, attributes, None)
            print("✅ Key removed.")
            return True
        except GLib.Error as e:
            print(f"❌ Failed to delete key: {e}")
            return False


def run_off_main(fn, callback):
    """Run fn() on a daemon worker thread and dispatch its result to
    callback(value) on the GTK main thread via GLib.idle_add.

    The house pattern for anything that may block (libsecret D-Bus lookups,
    scrypt, network) from a UI callback. Exceptions collapse to None so a
    dead keyring never takes the loop with it. Returns the Thread so tests
    can join it."""

    def _worker():
        try:
            val = fn()
        except Exception:
            val = None
        GLib.idle_add(callback, val)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t


def load_key_async(callback):
    """KeyManager.load_key() off the GTK main thread (startup-hang fix).

    Secret.password_lookup_sync blocks the caller until the D-Bus secret
    service answers: multi-second on cold boot, INDEFINITELY on a locked
    keyring (the unlock prompt waits on the user), worse through the Flatpak
    portal. The lookup runs on a daemon worker thread and callback(nsec_or_None)
    is dispatched on the main thread via GLib.idle_add — the house pattern
    (client.py connect_all / probe_tor). Returns the Thread so callers and
    tests can join it."""

    def _worker():
        nsec = KeyManager.load_key()
        GLib.idle_add(callback, nsec)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t


# NIP-46 bunker session (P2 #15): a separate libsecret item — the session is a
# credential (URI + signer identity + secret token) but NOT a private key.
SESSION_SCHEMA = Secret.Schema.new(
    "tech.livingonlinux.gnostr.BunkerSession",
    Secret.SchemaFlags.NONE,
    {
        "application": Secret.SchemaAttributeType.STRING,
    },
)


class BunkerSessionManager:
    @staticmethod
    def save_session(blob):
        attributes = {"application": "gnostr-bunker"}
        try:
            Secret.password_store_sync(
                SESSION_SCHEMA,
                attributes,
                Secret.COLLECTION_DEFAULT,
                "Gnostr Bunker Session",
                blob,
                None,
            )
            return True
        except GLib.Error as e:
            print(f"❌ Failed to save bunker session: {e}")
            return False

    @staticmethod
    def load_session():
        attributes = {"application": "gnostr-bunker"}
        try:
            blob = Secret.password_lookup_sync(SESSION_SCHEMA, attributes, None)
            return json.loads(blob) if blob else None
        except GLib.Error as e:
            print(f"❌ Failed to load bunker session: {e}")
            return None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def delete_session():
        attributes = {"application": "gnostr-bunker"}
        try:
            Secret.password_clear_sync(SESSION_SCHEMA, attributes, None)
            return True
        except GLib.Error as e:
            print(f"❌ Failed to delete bunker session: {e}")
            return False


# NWC connection URI (spec zaps-payments-v1 §3.2): a SPENDING credential,
# stored with the same treatment as the NIP-44 DM key — libsecret only,
# never GSettings/config JSON.
NWC_SCHEMA = Secret.Schema.new(
    "tech.livingonlinux.gnostr.NwcUri",
    Secret.SchemaFlags.NONE,
    {
        "application": Secret.SchemaAttributeType.STRING,
    },
)


class NWCUriManager:
    @staticmethod
    def save_uri(uri):
        attributes = {"application": "gnostr-nwc"}
        try:
            Secret.password_store_sync(
                NWC_SCHEMA,
                attributes,
                Secret.COLLECTION_DEFAULT,
                "Gnostr NWC Wallet Connection",
                uri,
                None,
            )
            return True
        except GLib.Error as e:
            print(f"❌ Failed to save NWC URI: {e}")
            return False

    @staticmethod
    def load_uri():
        attributes = {"application": "gnostr-nwc"}
        try:
            return Secret.password_lookup_sync(NWC_SCHEMA, attributes, None)
        except GLib.Error as e:
            print(f"❌ Failed to load NWC URI: {e}")
            return None

    @staticmethod
    def delete_uri():
        attributes = {"application": "gnostr-nwc"}
        try:
            Secret.password_clear_sync(NWC_SCHEMA, attributes, None)
            return True
        except GLib.Error as e:
            print(f"❌ Failed to delete NWC URI: {e}")
            return False
