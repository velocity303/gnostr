"""Zap budget ledger + soft cap (spec zaps-payments-v1 §2.5).

Two enforcement layers: the wallet service hard cap (minted with the
connection; QUOTA_EXCEEDED is its signal) and this client soft cap from
GSettings `monthly-zap-budget-sats` (default 100000). The app tracks
Σ spent per calendar month from settled pay_invoice results persisted in
the append-only zap_ledger table and blocks the confirmation dialog when
the next zap would exceed the cap.

bolt11 amount parsing is minimal (bech32 amount prefix only) — enough for
the budget gate; full invoice decoding is the wallet service's job.
"""

import time

DEFAULT_MONTHLY_BUDGET_SATS = 100000

_WALLET_KEYS = {
    "nwc-connection-label": "s",
    "monthly-zap-budget-sats": "i",
}
_WALLET_DEFAULTS = {
    "nwc-connection-label": "",
    "monthly-zap-budget-sats": DEFAULT_MONTHLY_BUDGET_SATS,
}


def _settings():
    """Gio.Settings for the app schema, or None (dev run / missing schema) —
    same pattern as proxy_settings._settings."""
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio

        return Gio.Settings.new("tech.livingonlinux.gnostr")
    except Exception:
        return None


def wallet_get(key, fallback=None):
    if fallback is None:
        fallback = _WALLET_DEFAULTS[key]
    s = _settings()
    if s is None:
        return fallback
    try:
        if _WALLET_KEYS[key] == "i":
            return s.get_int(key)
        return s.get_string(key) or fallback
    except Exception:
        return fallback


def wallet_set(key, value):
    s = _settings()
    if s is None:
        return False
    try:
        if _WALLET_KEYS[key] == "i":
            s.set_int(key, int(value))
        else:
            s.set_string(key, str(value))
        return True
    except Exception:
        return False

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7yl"
_MULTIPLIER = {"m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12}


def bolt11_amount_sats(invoice):
    """sats encoded in the bolt11 amount prefix, or None when absent
    (0 = 'any amount' invoices)."""
    if not isinstance(invoice, str) or not invoice.lower().startswith("lnbc"):
        return None
    body = invoice[4:].lower()
    digits = ""
    for ch in body:
        if ch.isdigit():
            digits += ch
        else:
            break
    if not digits or not body[len(digits) : len(digits) + 1] in _MULTIPLIER:
        return None
    mult = _MULTIPLIER[body[len(digits)]]
    # amount is in BTC: digits * mult
    btc = int(digits) * mult
    sats = btc * 1e8
    # 'p' multiplier must divide evenly (BOLT-11)
    return round(sats)


def month_key(ts=None):
    return time.strftime("%Y-%m", time.localtime(ts if ts is not None else time.time()))


def monthly_budget_sats(settings=None):
    """Soft cap from GSettings with hard fallback (dev-run safe)."""
    if settings is None:
        return int(wallet_get("monthly-zap-budget-sats"))
    return int(settings)


def record_payment(db, bolt11, sats, amount_msat=None):
    """Append-only ledger entry + paid-invoice set (spec §3.3)."""
    db.record_zap_payment(bolt11, month_key(), sats, amount_msat or sats * 1000)


def month_spent_sats(db, month=None):
    return db.zap_month_spent(month or month_key())


def check_budget(db, next_sats, budget_sats, month=None):
    """Raise QuotaExceededError when Σ(month) + next_sats > budget_sats."""
    from gnostr.wallet.backend import QuotaExceededError

    spent = month_spent_sats(db, month)
    if spent + next_sats > budget_sats:
        raise QuotaExceededError(
            f"monthly zap budget reached ({spent} + {next_sats} > "
            f"{budget_sats} sats)",
            code="QUOTA_EXCEEDED",
        )
    return True
