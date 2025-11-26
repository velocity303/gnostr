import gi
from gi.repository import Libsecret, GLib

# Schema defines what "kind" of secret this is.
# This helps the Keyring organize it.
SECRET_SCHEMA = Libsecret.Schema.new("me.velocitynet.Gnostr.Schema",
    Libsecret.SchemaFlags.NONE,
    {
        "application": Libsecret.SchemaAttributeType.STRING,
    }
)

class KeyManager:
    @staticmethod
    def save_key(nsec):
        """Saves the private key (nsec) securely to the keyring."""
        attributes = {"application": "gnostr"}

        # This is a synchronous call for simplicity, but in a large app
        # you might want the async version.
        try:
            Libsecret.password_store_sync(
                SECRET_SCHEMA,
                attributes,
                Libsecret.COLLECTION_DEFAULT,
                "Gnostr Private Key", # Label visible in "Passwords and Keys" app
                nsec,
                None
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
            nsec = Libsecret.password_lookup_sync(
                SECRET_SCHEMA,
                attributes,
                None
            )
            return nsec
        except GLib.Error as e:
            print(f"❌ Failed to load key: {e}")
            return None

    @staticmethod
    def delete_key():
        """Removes the key (Log out)."""
        attributes = {"application": "gnostr"}
        try:
            Libsecret.password_clear_sync(
                SECRET_SCHEMA,
                attributes,
                None
            )
            print("✅ Key removed.")
            return True
        except GLib.Error as e:
            print(f"❌ Failed to delete key: {e}")
            return False
