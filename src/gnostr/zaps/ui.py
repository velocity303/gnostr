"""GTK surfaces for zap send (spec zaps-payments-v1 §3.4, §2.1 UI wiring).

ZapSheet: amount + comment entry (bounded by the LNURL min/max sendable).
ZapConfirmDialog: the §3.4 contract — sats (exact msat in tooltip),
recipient display name, LNURL domain, fee-free note. Emits one pay or
nothing; no auto-pay, no remembered amount.
start_zap(): the off-main-thread orchestration used by the post-card and
profile-header zap buttons (threading + GLib.idle_add, dialogs.py
on_test_nwc pattern).
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from gnostr.zaps.lnurl import (
    ZapError,
    fetch_metadata,  # noqa: E402
    resolve_target,
    zapable_profile,
)


class ZapReceiptsDialog(Adw.Dialog):
    """Tap-through list for a post's zap chip: who zapped, how much
    (spec zaps-payments-v1 §1.4 deliverable 3, v1 rendering). Rows come
    from the verified-only zaps table — nothing here can be unverified."""

    def __init__(self, parent, rows):
        super().__init__()
        self.set_title("Zaps")
        self.set_content_width(360)
        self.set_content_height(min(480, 60 + 56 * max(len(rows), 1)))

        clamp = Adw.Clamp(maximum_size=360)
        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        clamp.set_child(outer)
        self.set_child(clamp)

        if not rows:
            outer.append(
                Gtk.Label(label="No zap receipts", css_classes=["dim-label"])
            )
            return

        total = sum(r["msat"] for r in rows) // 1000
        head = Gtk.Label(label=f"{total:,} sats from {len(rows)} zaps")
        head.add_css_class("heading")
        head.set_xalign(0)
        outer.append(head)

        for r in rows:
            row = Adw.ActionRow()
            row.set_title(recipient_display_name(parent.db, r["sender"]))
            sats = max(r["msat"] // 1000, 1)
            row.set_subtitle(r["comment"] or "")
            amt = Gtk.Label(label=f"⚡ {sats:,}")
            amt.add_css_class("caption")
            amt.add_css_class("dim-label")
            row.add_suffix(amt)
            outer.append(row)


def _spent(db):
    from gnostr.wallet.budget import month_spent_sats

    try:
        return month_spent_sats(db)
    except Exception:
        return 0


def wallet_backend_for(main_window):
    """Lazily build + cache the NWCBackend from the libsecret URI (spec §3.2).
    Returns None when no URI is configured."""
    cached = getattr(main_window, "_zap_backend", None)
    if cached is not None:
        return cached
    try:
        from gnostr.key_manager import NWCUriManager

        uri = NWCUriManager.load_uri()
    except Exception:
        uri = None
    if not uri:
        return None
    from gnostr.wallet import NWCBackend

    backend = NWCBackend(uri, db=main_window.db)
    main_window._zap_backend = backend
    return backend


def recipient_display_name(db, pubkey):
    prof = db.get_profile(pubkey) if db else None
    if prof:
        return prof.get("display_name") or prof.get("name") or pubkey[:8]
    return pubkey[:8]


def _quota_toast(mw):
    """§2.5: QUOTA_EXCEEDED / soft-cap block is terminal UX and points at
    the budget page (Settings → Wallet)."""
    from gnostr.dialogs import RelayPreferencesWindow

    toast = Adw.Toast(title="monthly zap budget reached")
    toast.set_button_label("Budget settings")
    toast.connect(
        "button-clicked",
        lambda t: _open_wallet_page(mw, RelayPreferencesWindow),
    )
    mw.add_toast(toast)


def _open_wallet_page(mw, win_cls):
    try:
        win = win_cls(mw.client, mw)
        win.set_visible_page(win.wallet_page)
        win.present()
    except Exception:
        pass


def _error_copy(e):
    """User-facing failure copy (spec §2.2: error codes are UX, not retry
    fuel; QUOTA_EXCEEDED is terminal)."""
    from gnostr.wallet.backend import (
        InsufficientBalanceError,
        PaymentFailedError,
        QuotaExceededError,
        WalletError,
    )

    if isinstance(e, QuotaExceededError):
        return "monthly zap budget reached"
    if isinstance(e, InsufficientBalanceError):
        return "wallet balance too low"
    if isinstance(e, PaymentFailedError):
        return f"Payment failed: {e}"
    if isinstance(e, WalletError):
        return f"Wallet error: {e}"
    if isinstance(e, ZapError):
        return str(e)
    return f"Zap failed: {e}"


class ZapSheet(Adw.Dialog):
    """Amount + comment entry, bounded by the LNURL payRequest window."""

    def __init__(self, parent, metadata, on_result):
        super().__init__()
        self.set_title("Send a zap")
        self._on_result = on_result
        self.min_sats = -(-metadata["minSendable"] // 1000)  # ceil
        self.max_sats = max(metadata["maxSendable"] // 1000, 1)
        self.comment_allowed = metadata["commentAllowed"]

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=18,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )
        self.set_child(box)

        self.amount_row = Adw.EntryRow(title="Amount (sats)")
        self.amount_row.set_text(str(self.min_sats))
        box.append(self.amount_row)

        self.comment_row = Adw.EntryRow(title="Comment")
        if self.comment_allowed <= 0:
            self.comment_row.set_sensitive(False)
            self.comment_row.set_title("Comment (recipient doesn't accept comments)")
        box.append(self.comment_row)

        self.hint = Gtk.Label(
            label=f"Between {self.min_sats:,} and {self.max_sats:,} sats",
            css_classes=["caption", "dim-label"],
        )
        box.append(self.hint)

        btn = Gtk.Button(label="Continue", css_classes=["pill", "suggested-action"])
        btn.connect("clicked", self._on_continue)
        box.append(btn)

    def _on_continue(self, _btn):
        try:
            sats = int(self.amount_row.get_text().strip())
        except ValueError:
            self.hint.set_text("Enter a whole number of sats")
            return
        if not (self.min_sats <= sats <= self.max_sats):
            self.hint.set_text(f"Between {self.min_sats:,} and {self.max_sats:,} sats")
            return
        comment = self.comment_row.get_text().strip()
        if self.comment_allowed > 0:
            comment = comment[: self.comment_allowed]
        self._on_result(sats * 1000, comment)
        self.close()


class ZapConfirmDialog(Adw.Dialog):
    """§3.4 confirmation: shows sats (exact msat tooltip), recipient name,
    LNURL domain, fee note; emits exactly one answer."""

    def __init__(self, parent, summary, on_answer):
        super().__init__()
        self.set_title("Confirm zap")
        self._on_answer = on_answer

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            margin_top=18,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )
        self.set_child(box)

        sats_lbl = Gtk.Label()
        sats_lbl.set_markup(f"<b>{summary['sats']:,} sats</b>")
        # §3.4: sats shown rounded, EXACT msat in the tooltip
        sats_lbl.set_tooltip_text(f"exact: {summary['amount_msat']} msat")
        box.append(sats_lbl)

        box.append(
            Gtk.Label(label=f"to {summary['recipient_name']}", css_classes=["heading"])
        )
        box.append(
            Gtk.Label(
                label=f"via {summary['lnurl_domain']}",
                css_classes=["caption", "dim-label"],
            )
        )
        if summary.get("comment"):
            box.append(
                Gtk.Label(label=f"“{summary['comment']}”", css_classes=["caption"])
            )
        box.append(
            Gtk.Label(
                label="No fees beyond the amount sent.",
                css_classes=["caption", "dim-label"],
            )
        )
        box.append(
            Gtk.Label(
                label=(
                    f"Budget: {summary['budget_spent_sats']:,} / "
                    f"{summary['budget_total_sats']:,} sats this month"
                ),
                css_classes=["caption", "dim-label"],
            )
        )

        btn_box = Gtk.Box(spacing=8, homogeneous=True, margin_top=8)
        box.append(btn_box)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda b: (self._answer(False), self.close()))
        send = Gtk.Button(label="Send zap")
        send.add_css_class("suggested-action")
        send.add_css_class("pill")
        send.connect("clicked", lambda b: (self._answer(True), self.close()))
        btn_box.append(cancel)
        btn_box.append(send)

    def _answer(self, ok):
        cb, self._on_answer = self._on_answer, None
        if cb:
            cb(ok)


def start_zap(main_window, pubkey, event=None):
    """Entry point from the post-card / profile-header zap button.

    Worker threads do resolve -> metadata gate -> invoice -> pay; dialogs
    and toasts happen on the main thread via GLib.idle_add. The confirmation
    dialog is the payment boundary: confirming runs one pay_invoice,
    cancelling runs nothing (§3.4).
    """
    from gnostr.zaps.send import ZapFlow

    mw = main_window
    if not getattr(mw, "pub_key", None):
        mw.add_toast(Adw.Toast(title="Log in to send zaps"))
        return
    backend = wallet_backend_for(mw)
    if backend is None:
        mw.add_toast(
            Adw.Toast(title="No wallet connected — add an NWC URI in Settings → Wallet")
        )
        return

    prof = mw.db.get_profile(pubkey) or {}
    lnurl = resolve_target(prof)
    if not lnurl:
        mw.add_toast(Adw.Toast(title="recipient doesn't support zaps"))
        return

    name = recipient_display_name(mw.db, pubkey)
    client = mw.client
    flow = ZapFlow(
        backend,
        mw.db,
        write_relays=lambda: list((client._write_relays() or {}).keys()),
        signer=client._sign,
        sender_pubkey=mw.pub_key,
        recipient_pubkey=pubkey,
        event=event,
    )

    def idle(fn):
        GLib.idle_add(lambda: (fn(), False)[1])

    def toast(title):
        idle(lambda: mw.add_toast(Adw.Toast(title=title)))

    def spawn(fn):
        import threading

        threading.Thread(target=fn, daemon=True).start()

    def do_pay(prepared, meta):
        """Post-confirmation: one pay_invoice via the flow (budget re-check +
        double-pay guards inside NWCBackend, §3.3). QUOTA_EXCEEDED ->
        terminal budget copy."""

        def worker():
            from gnostr.wallet.backend import QuotaExceededError

            try:
                flow.confirm_and_pay(prepared, meta, name, confirm=lambda s: True)
            except QuotaExceededError:
                idle(lambda: _quota_toast(mw))
                return
            except Exception as e:
                toast(_error_copy(e))
                return
            toast(f"Zapped {prepared['sats']:,} sats")

        spawn(worker)

    def on_amount(amount_msat, comment):
        """Sheet done: fetch invoice off-thread, then the §3.4 dialog."""

        def worker():
            from gnostr.wallet.budget import monthly_budget_sats

            try:
                meta = fetch_metadata(lnurl)
                prepared = flow.prepare(lnurl, meta, amount_msat, comment)
                # Soft budget BEFORE the dialog (§2.5: blocks confirmation)
                from gnostr.wallet.backend import QuotaExceededError
                from gnostr.wallet.budget import check_budget

                try:
                    check_budget(mw.db, prepared["sats"], monthly_budget_sats())
                except QuotaExceededError:
                    idle(lambda: _quota_toast(mw))
                    return
            except ZapError as e:
                toast(str(e))
                return
            summary = {
                **prepared,
                "recipient_name": name,
                "lnurl_domain": meta["domain"],
                "budget_spent_sats": _spent(mw.db),
                "budget_total_sats": monthly_budget_sats(),
            }
            idle(
                lambda: ZapConfirmDialog(
                    mw, summary, lambda ok: do_pay(prepared, meta) if ok else None
                ).present()
            )

        spawn(worker)

    # Gate first: metadata fetch off-thread, sheet opens only if zapable.
    def gate_worker():
        try:
            meta = fetch_metadata(lnurl)
        except ZapError as e:
            toast(str(e))
            return
        idle(lambda: ZapSheet(mw, meta, on_amount).present())

    spawn(gate_worker)
