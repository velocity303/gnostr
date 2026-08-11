"""
Gnostr - A native Linux Nostr client.
"""

__version__ = "0.1.0"

# Import submodules to make them available as package attributes
# This ensures that `from gnostr.gateway import gateway` works
from . import client
from . import connection_status
from . import database
from . import dialogs
from . import gateway
from . import key_manager
from . import main
from . import nostr_utils
from . import profile_nips
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
