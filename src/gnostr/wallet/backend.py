"""WalletBackend ABC (spec zaps-payments-v1 §2.4) + error taxonomy (§2.2).

DTOs mirror NIP-47 JSON exactly so v2 direct-LND/CLN adapters are drop-in:
call sites (zap flow, wallet panel) never branch on backend type. v1 ships
NWCBackend only.

Error codes are UX, not retry fuel (spec §2.2): each wire error code maps to
a distinct exception class so the UI can show the right copy and never
auto-retry a spend.
"""

import abc


class WalletError(Exception):
    """Base for every wallet-backend failure. Carries the NIP-47 error code
    (when the wallet service sent one) and the human-readable message."""

    code = "WALLET_ERROR"

    def __init__(self, message="", code=None):
        super().__init__(message or self.code)
        if code:
            self.code = code


class WalletTimeoutError(WalletError):
    """No kind-23195 response within 30 s (spec §2.2 WALLET_TIMEOUT)."""

    code = "WALLET_TIMEOUT"


class WalletCapabilityError(WalletError):
    """The wallet's kind-13194 info event does not advertise this method.
    UI copy: 'wallet doesn't support X'."""

    code = "CAPABILITY_MISSING"


class QuotaExceededError(WalletError):
    """Wallet-service hard cap hit (or the app soft cap blocked the zap).
    UI copy: 'monthly zap budget reached' — terminal, never a retry."""

    code = "QUOTA_EXCEEDED"


class InsufficientBalanceError(WalletError):
    """UI copy: show the current balance (spec §2.2)."""

    code = "INSUFFICIENT_BALANCE"


class PaymentFailedError(WalletError):
    """UI copy: show the message; never auto-retry a spend (spec §2.2)."""

    code = "PAYMENT_FAILED"


# NIP-47 wire error code -> exception class (spec §2.2 UX notes)
ERROR_CODE_MAP = {
    "QUOTA_EXCEEDED": QuotaExceededError,
    "INSUFFICIENT_BALANCE": InsufficientBalanceError,
    "PAYMENT_FAILED": PaymentFailedError,
    "PAYMENT_EXPIRED": PaymentFailedError,
    "NOT_AUTHORIZED": WalletCapabilityError,
    "TIMEOUT": WalletTimeoutError,
}


def error_from_response(error_obj):
    """Build the right exception from a NIP-47 response error object
    ({"code": ..., "message": ...}) or a bare code string."""
    if isinstance(error_obj, dict):
        code = (error_obj.get("code") or "").upper()
        message = error_obj.get("message") or code
    else:
        code = (error_obj or "").upper()
        message = code
    cls = ERROR_CODE_MAP.get(code, WalletError)
    return cls(message, code=code or None)


class WalletBackend(abc.ABC):
    """Abstract wallet interface per spec §2.4. All amounts are msat ints;
    return values are the NIP-47 result dicts verbatim so a v2 backend
    (LndRestBackend / ClnRestBackend) drops in with zero call-site rework."""

    @abc.abstractmethod
    def get_info(self):
        """-> {"alias": str, "color": str, "methods": [str], "network": str}"""
        raise NotImplementedError

    @abc.abstractmethod
    def get_balance(self):
        """-> {"balance_msat": int}"""
        raise NotImplementedError

    @abc.abstractmethod
    def make_invoice(self, amount_msat, description, expiry_s=None):
        """-> {"invoice": str, "payment_hash": str, "created_at": int}"""
        raise NotImplementedError

    @abc.abstractmethod
    def pay_invoice(self, bolt11, amount_msat=None):
        """-> {"preimage": str, "fees_paid_msat": int}"""
        raise NotImplementedError

    @abc.abstractmethod
    def lookup_invoice(self, payment_hash=None, bolt11=None):
        """-> {"settled": bool, ...}"""
        raise NotImplementedError

    @abc.abstractmethod
    def list_transactions(self, from_ts, until_ts, limit=50, offset=0, type=None):
        """-> {"transactions": [...]}"""
        raise NotImplementedError
