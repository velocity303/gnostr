import gi

import json

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
