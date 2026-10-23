"""
Gnostr - A native Linux Nostr client.
"""

__version__ = "0.1.0"

# Import profile_nips FIRST — it's stdlib-only (no package deps), so importing
# it before client/dialogs/etc. avoids the partial-initialization window where
# `client.py`'s module-level `import gnostr` would otherwise see a `gnostr`
# package that doesn't yet have `profile_nips` as an attribute.
from . import profile_nips

# relay_entries is stdlib-only (pure data model, spec relays-and-settings-v1 A)
# and must be a package attribute before client.py's methods run under the
# ast-extraction test harness (their namespace carries `gnostr` only).
from . import relay_entries

# Import submodules to make them available as package attributes
# This ensures that `from gnostr.gateway import gateway` works
from . import nostr_utils
from . import client
from . import connection_status
from . import database
from . import dialogs
from . import gateway
from . import key_manager
from . import main
from . import renderer
from . import window

# Import service submodules
from .service import feed_service
from .service import profile_metadata_service
from .service import profile_service

# Import UI submodules
from .ui import feed_view
from .ui import post_widget
from .ui import profile_view
from .ui import sidebar
from .ui import thread_view

__all__ = [
    "__version__",
    "client",
    "connection_status",
    "database",
    "dialogs",
    "gateway",
    "key_manager",
    "main",
    "nostr_utils",
    "relay_entries",
    "renderer",
    "window",
    "feed_service",
    "profile_metadata_service",
    "profile_service",
    "feed_view",
    "post_widget",
    "profile_view",
    "sidebar",
    "thread_view",
]
