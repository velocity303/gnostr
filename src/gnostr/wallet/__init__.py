"""Wallet backends for gnostr (spec zaps-payments-v1).

v1 ships NWCBackend only; the WalletBackend ABC is the v2 expansion seam
(LndRestBackend / ClnRestBackend drop in behind the same signatures).
"""

from gnostr.wallet.backend import (  # noqa: F401
    InsufficientBalanceError,
    PaymentFailedError,
    QuotaExceededError,
    WalletBackend,
    WalletCapabilityError,
    WalletError,
    WalletTimeoutError,
)
from gnostr.wallet.nwc import NWCBackend  # noqa: F401
from gnostr.wallet.nwc_uri import NWCUriError, parse_nwc_uri  # noqa: F401

__all__ = [
    "WalletBackend",
    "NWCBackend",
    "NWCUriError",
    "parse_nwc_uri",
    "WalletError",
    "WalletTimeoutError",
    "WalletCapabilityError",
    "QuotaExceededError",
    "InsufficientBalanceError",
    "PaymentFailedError",
]
