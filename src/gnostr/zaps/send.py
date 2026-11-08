"""ZapFlow orchestration (spec zaps-payments-v1 §3.3/§3.4).

resolve -> metadata gate -> invoice (callback) -> confirmation -> pay.
The dialog is the payment boundary: it emits exactly one pay_invoice call
or nothing. No auto-pay, no remembered-amount shortcut (§3.4). Budget
enforcement reuses the ZAPS-C layers: the client soft cap check raises
QuotaExceededError before the wallet call; the wallet's hard cap surfaces
as QUOTA_EXCEEDED from pay_invoice — terminal UX, never a retry (§2.2).

The flow is backend-agnostic (WalletBackend ABC) and UI-agnostic: `confirm`
is an injected callable (the GTK dialog in ui.py; a fake in tests), so the
whole path is testable headless.
"""

from gnostr.wallet.backend import QuotaExceededError, WalletError
from gnostr.wallet.budget import bolt11_amount_sats, check_budget
from gnostr.zaps.lnurl import ZapError
from gnostr.zaps.request import fetch_invoice


class ZapCancelled(Exception):
    """User dismissed the confirmation dialog — no payment was attempted."""


class ZapFlow:
    """One zap, start to finish. Construct per zap; call run() off the UI
    thread (GTK path wraps it in a worker thread + GLib.idle_add)."""

    def __init__(
        self,
        backend,
        db,
        write_relays,
        signer,
        sender_pubkey,
        recipient_pubkey,
        event=None,
        settings=None,
        budget_sats=None,
    ):
        """backend: WalletBackend (ZAPS-C). db: Database (budget ledger).
        write_relays: callable -> list[str] (same set DMs use, §1.3).
        signer: callable(event) -> signed event (main key; client._sign).
        event: (event_id, kind) for a note-zap, None for a profile zap.
        budget_sats: soft-cap override (tests); None reads GSettings."""
        self.backend = backend
        self.db = db
        self._write_relays = write_relays
        self.sign = signer
        self.sender_pubkey = sender_pubkey
        self.recipient_pubkey = recipient_pubkey
        self.event = event
        self.settings = settings
        self.budget_sats = budget_sats

    def prepare(self, lnurl_bech32, metadata, amount_msat, comment=None):
        """Build + sign the 9734, fetch the invoice from the callback.

        Returns {bolt11, sats, amount_msat, success_action, comment}.
        Raises ZapError on any LNURL failure (before any payment state).
        """
        from gnostr.zaps.request import build_9734

        relays = list(self._write_relays() or [])
        unsigned = build_9734(
            self._sender_pubkey(),
            self.recipient_pubkey,
            amount_msat,
            lnurl_bech32,
            relays,
            event=self.event,
            comment=comment if comment else "Zap!",
        )
        signed = self.sign(unsigned)
        if not signed or not signed.get("sig"):
            raise ZapError("Could not sign the zap request")
        resp = fetch_invoice(
            metadata["callback"],
            signed,
            lnurl_bech32,
            comment=comment or None,
            settings=self.settings,
        )
        bolt11 = resp["pr"]
        sats = bolt11_amount_sats(bolt11)
        if sats is None:
            # amount-less invoice (rare): the requested amount is the truth
            sats = round(amount_msat / 1000)
        return {
            "bolt11": bolt11,
            "sats": sats,
            "amount_msat": amount_msat,
            "success_action": resp.get("successAction"),
            "comment": comment or "",
        }

    def _sender_pubkey(self):
        return self.sender_pubkey or ""

    def confirm_and_pay(self, prepared, metadata, recipient_name, confirm):
        """§3.4 dialog boundary: budget check -> confirm(...) -> one pay or
        nothing. `confirm(summary) -> bool`. Returns the pay result dict;
        raises ZapCancelled / QuotaExceededError / WalletError."""
        bolt11 = prepared["bolt11"]
        sats = prepared["sats"]

        # Soft budget BEFORE the dialog (spec §2.5: blocks the confirmation
        # dialog when the next zap would exceed the cap).
        from gnostr.wallet.budget import month_spent_sats, monthly_budget_sats

        budget = self.budget_sats or monthly_budget_sats()
        spent = month_spent_sats(self.db) if self.db is not None else 0
        if self.db is not None:
            check_budget(self.db, sats, budget)

        summary = {
            "sats": sats,
            "amount_msat": prepared["amount_msat"],
            "recipient_name": recipient_name,
            "recipient_pubkey": self.recipient_pubkey,
            "lnurl_domain": metadata.get("domain", ""),
            "comment": prepared["comment"],
            "budget_spent_sats": spent,
            "budget_total_sats": budget,
        }
        if not confirm(summary):
            raise ZapCancelled("zap cancelled")

        # Double-pay guards (in-flight lock, lookup-before-pay, persisted
        # paid set) live inside NWCBackend.pay_invoice (ZAPS-C §3.3).
        return self.backend.pay_invoice(bolt11)

    def run(
        self,
        lnurl_bech32,
        metadata,
        amount_msat,
        recipient_name,
        comment=None,
        confirm=lambda s: True,
    ):
        """Full flow: prepare -> confirm -> pay. Raises on any failure."""
        prepared = self.prepare(lnurl_bech32, metadata, amount_msat, comment)
        return self.confirm_and_pay(prepared, metadata, recipient_name, confirm)
