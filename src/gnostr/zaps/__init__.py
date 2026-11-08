"""Zap send protocol layer (spec zaps-payments-v1 §1.1-1.3, §3.4).

Pure-protocol modules (no GTK): target resolution + LNURL-pay gate (lnurl),
kind-9734 request builder + callback client (request), and the ZapFlow
orchestration (send). The GTK surfaces live in gnostr.zaps.ui.
"""

from gnostr.zaps.lnurl import NotZapableError  # noqa: F401
from gnostr.zaps.lnurl import (
    ProfileSharingRequired,
    ZapError,
    ZapHTTPError,
    clear_metadata_cache,
    fetch_metadata,
    resolve_target,
)
from gnostr.zaps.request import (
    build_9734,
    build_callback_url,  # noqa: F401
    canonical_event_json,
    fetch_invoice,
    verify_event_sig,
)
from gnostr.zaps.send import ZapFlow  # noqa: F401

__all__ = [
    "ZapError",
    "NotZapableError",
    "ProfileSharingRequired",
    "ZapHTTPError",
    "resolve_target",
    "fetch_metadata",
    "clear_metadata_cache",
    "build_9734",
    "canonical_event_json",
    "build_callback_url",
    "fetch_invoice",
    "verify_event_sig",
    "ZapFlow",
]
