"""NWCBackend — NIP-47 Nostr Wallet Connect client (spec zaps-payments-v1 §2).

Separate websocket connection to the URI's relay (NIP-47 mandates requests/
responses flow there — NOT the social relay pool). Requests are kind 23194
signed by the connection secret (never the main nsec, §3.1), responses are
kind 23195 matched by `e` tag, 30 s timeout -> WALLET_TIMEOUT.

Encryption: NIP-44 v2 preferred; NIP-04 legacy only when the wallet's
kind-13194 info event lacks nip44_v2 (or no info event at all).

The websocket transport is injectable (`transport_factory`) so tests run a
fake wallet service without touching the network; the default transport
reuses the same websocket-client + SOCKS-proxy stack as client.py.

All public methods are blocking and MUST be called off the main thread
(executor pattern from renderer.py VideoPlayer); `call_async` wraps that.
"""

import base64
import concurrent.futures
import json
import threading
import time

from gnostr import nip44, nostr_utils
from gnostr.wallet import nip04
from gnostr.wallet.backend import (WalletBackend,  # noqa: E402
                                   WalletCapabilityError, WalletError,
                                   WalletTimeoutError, error_from_response)
from gnostr.wallet.budget import (bolt11_amount_sats, check_budget,
                                  record_payment)
from gnostr.wallet.nwc_uri import parse_nwc_uri

NWC_INFO_KIND = 13194
NWC_REQUEST_KIND = 23194
NWC_RESPONSE_KIND = 23195
NWC_TIMEOUT_S = 30.0

# v1 method surface (spec §2.2 "v1 required methods"); anything else is
# refused client-side before it can leak onto the wire.
V1_METHODS = frozenset(
    {
        "pay_invoice",
        "make_invoice",
        "get_balance",
        "lookup_invoice",
        "get_info",
        "list_transactions",
    }
)

_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="nwc"
)


class _WsTransport:
    """Default transport: websocket-client WebSocketApp on a daemon thread,
    same shape as client.py RelayConnection (incl. SOCKS proxy kwargs so an
    .onion NWC relay works over the opt-in tor proxy)."""

    def __init__(self, url, on_event, on_connected=None):
        self.url = url
        self.on_event = on_event
        self.on_connected = on_connected
        self._ws = None
        self._thread = None

    def start(self):
        import websocket  # deferred: not needed by fake-transport tests

        def on_msg(ws, m):
            try:
                d = json.loads(m)
            except (ValueError, TypeError):
                return
            if isinstance(d, list) and d and d[0] == "EVENT" and len(d) >= 3:
                self.on_event(d[2])

        def on_open(ws):
            if self.on_connected:
                self.on_connected()

        self._ws = websocket.WebSocketApp(self.url, on_message=on_msg, on_open=on_open)
        from gnostr import proxy_settings

        self._thread = threading.Thread(
            target=self._ws.run_forever,
            kwargs=proxy_settings.socks_proxy_kwargs(),
            daemon=True,
        )
        self._thread.start()

    def send(self, payload):
        self._ws.send(json.dumps(payload))

    def close(self):
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass


class NWCBackend(WalletBackend):
    """One connection = one wallet service. `db` (optional) enables the
    §3.3 double-pay guards + §2.5 budget ledger persistence."""

    def __init__(
        self,
        uri,
        db=None,
        timeout_s=NWC_TIMEOUT_S,
        transport_factory=None,
        budget_sats=None,
    ):
        self.cfg = parse_nwc_uri(uri)
        self.secret = self.cfg["secret"]
        self.client_pubkey = nostr_utils.get_public_key(self.secret)
        self.wallet_pubkey = self.cfg["wallet_pubkey"]
        self.relay_url = self.cfg["relay"]
        self.db = db
        self.timeout_s = timeout_s
        self._budget_sats = budget_sats
        self._transport_factory = transport_factory or _WsTransport
        self._transport = None
        self._connected = threading.Event()
        self._lock = threading.Lock()
        # bolt11 -> threading.Lock (in-flight pay guard, §3.3)
        self._inflight = {}
        self._inflight_guard = threading.Lock()
        # request event id -> threading.Event + result slot
        self._pending = {}
        self._info = None  # cached get_info DTO
        self._capabilities = None  # advertised method list (None = unknown)
        self._encryption = None  # "nip44" | "nip04"
        self._info_waiter = None

    # -- connection lifecycle --------------------------------------------------

    def connect(self, timeout_s=10.0):
        """Open the NWC relay connection and block until it is live."""
        if self._transport is not None and self._connected.is_set():
            return
        self._connected.clear()
        self._transport = self._transport_factory(
            self.relay_url, self._on_event, self._on_connected
        )
        self._transport.start()
        if not self._connected.wait(timeout_s):
            raise WalletError("NWC relay connect timed out", code="WALLET_TIMEOUT")

    def close(self):
        if self._transport:
            self._transport.close()
        self._transport = None
        self._connected.clear()

    def _on_connected(self):
        self._connected.set()

    def _on_event(self, event):
        if not isinstance(event, dict):
            return
        kind = event.get("kind")
        if kind == NWC_RESPONSE_KIND:
            req_id = _tag_value(event, "e")
            with self._lock:
                slot = self._pending.get(req_id)
            if slot is not None:
                slot["event"] = event
                slot["done"].set()
        elif kind == NWC_INFO_KIND and event.get("pubkey") == self.wallet_pubkey:
            self._apply_info_event(event)
            waiter = getattr(self, "_info_waiter", None)
            if waiter:
                waiter(event)

    # -- capability gate ---------------------------------------------------------

    def _apply_info_event(self, event):
        methods = (event.get("content") or "").split()
        enc = _tag_value(event, "encryption") or "nip44_v2"
        self._capabilities = methods
        self._encryption = "nip04" if "nip44_v2" not in enc else "nip44"
        # NIP-47 info content varies by wallet: JSON object (alias/color) or a
        # bare space-separated method list (fixture form). Accept both.
        try:
            content = json.loads(event.get("content") or "{}")
        except (ValueError, TypeError):
            content = {}
        if isinstance(content, dict) and content.get("alias"):
            self._info = {
                "alias": content.get("alias", ""),
                "color": content.get("color", ""),
                "methods": methods,
                "network": content.get("network", "bitcoin"),
            }
        else:
            self._info = {
                "alias": "",
                "color": "",
                "methods": methods,
                "network": "bitcoin",
            }

    def fetch_info_event(self, timeout_s=None):
        """REQ the kind-13194 info event (snapshot). Returns the DTO or None
        when the wallet publishes none (-> legacy nip04 assumption)."""
        self.connect()
        done = threading.Event()
        slot = {"event": None, "done": done}

        def on_info(event):
            slot["event"] = event
            done.set()

        self._info_waiter = on_info
        self._transport.send(
            [
                "REQ",
                "nwc-info",
                {"kinds": [NWC_INFO_KIND], "authors": [self.wallet_pubkey], "limit": 1},
            ]
        )
        got = done.wait(timeout_s or self.timeout_s)
        self._info_waiter = None
        event = slot["event"] if got else None
        if event is None:
            # No info event: legacy wallet — assume nip04, no method list.
            self._encryption = "nip04"
            self._capabilities = None
            return None
        self._apply_info_event(event)
        return self._info

    def _gate(self, method):
        if method not in V1_METHODS:
            raise WalletCapabilityError(
                f"'{method}' is not a v1 method", code="CAPABILITY_MISSING"
            )
        if self._capabilities is not None and method not in self._capabilities:
            raise WalletCapabilityError(
                f"wallet doesn't support {method}", code="CAPABILITY_MISSING"
            )

    # -- request/response core ----------------------------------------------------

    def _encrypt(self, plaintext):
        if self._encryption == "nip04":
            return nip04.nip04_encrypt(plaintext, self.secret, self.wallet_pubkey)
        conv = nip44.get_conversation_key(
            bytes.fromhex(self.secret), bytes.fromhex(self.wallet_pubkey)
        )
        return nip44.encrypt(plaintext, conv)

    def _decrypt(self, content):
        if self._encryption == "nip04":
            return nip04.nip04_decrypt(content, self.secret, self.wallet_pubkey)
        conv = nip44.get_conversation_key(
            bytes.fromhex(self.secret), bytes.fromhex(self.wallet_pubkey)
        )
        return nip44.decrypt(content, conv)

    def call(self, method, params=None):
        """One NIP-47 request/response round trip. Returns the parsed result
        dict; raises the mapped WalletError subclass on a wallet error."""
        self._gate(method)
        self.connect()
        if self._encryption is None:
            # Unknown capabilities: probe the info event once (sets encryption).
            self.fetch_info_event()
        payload = json.dumps(
            {"method": method, "params": params or {}}, separators=(",", ":")
        )
        request = nostr_utils.build_event(
            self.client_pubkey,
            NWC_REQUEST_KIND,
            self._encrypt(payload),
            [["p", self.wallet_pubkey]],
        )
        request = nostr_utils.sign_event(request, self.secret)
        req_id = request["id"]

        done = threading.Event()
        slot = {"event": None, "done": done}
        with self._lock:
            self._pending[req_id] = slot
        try:
            # Open the response sub BEFORE publishing the request so a fast
            # wallet's reply can never race the REQ (lost response = timeout).
            self._transport.send(
                [
                    "REQ",
                    "nwc-res-" + req_id[:8],
                    {
                        "kinds": [NWC_RESPONSE_KIND],
                        "authors": [self.wallet_pubkey],
                        "#e": [req_id],
                    },
                ]
            )
            self._transport.send(["EVENT", request])
            if not done.wait(self.timeout_s):
                raise WalletTimeoutError(
                    f"no response to {method} within {self.timeout_s:.0f}s"
                )
        finally:
            with self._lock:
                self._pending.pop(req_id, None)

        event = slot["event"]
        if event.get("pubkey") != self.wallet_pubkey:
            raise WalletError("response from wrong pubkey", code="PAYMENT_FAILED")
        body = json.loads(self._decrypt(event["content"]))
        if body.get("error"):
            raise error_from_response(body["error"])
        return body.get("result") or {}

    # -- WalletBackend surface ------------------------------------------------------

    def get_info(self):
        if self._info is None:
            self.fetch_info_event()
        if self._info is None:
            # legacy wallet without info event: minimal DTO from the call
            self._info = self.call("get_info")
        return self._info

    def get_balance(self):
        return self.call("get_balance")

    def make_invoice(self, amount_msat, description, expiry_s=None):
        params = {"amount_msat": amount_msat, "description": description}
        if expiry_s is not None:
            params["expiry"] = expiry_s
        return self.call("make_invoice", params)

    def pay_invoice(self, bolt11, amount_msat=None):
        """§3.3 double-pay guards + §2.5 soft budget, then the NIP-47 call."""
        # Guard 1: already paid (persisted set) -> no-op success.
        if self.db is not None and self.db.is_invoice_paid(bolt11):
            return {"preimage": "", "fees_paid_msat": 0, "already_paid": True}
        # Guard 2: in-flight lock per bolt11 — second confirm is a no-op.
        with self._inflight_guard:
            lock = self._inflight.get(bolt11)
            if lock is None:
                lock = threading.Lock()
                self._inflight[bolt11] = lock
            if not lock.acquire(blocking=False):
                return {"preimage": "", "fees_paid_msat": 0, "already_paid": True}
        try:
            # Guard 3: lookup before pay — settled invoice is success, skip.
            try:
                existing = self.call("lookup_invoice", {"bolt11": bolt11})
            except WalletError:
                existing = {}
            if existing.get("settled"):
                self._record_paid(bolt11, amount_msat)
                return {
                    "preimage": existing.get("preimage", ""),
                    "fees_paid_msat": 0,
                    "already_paid": True,
                }
            # Guard 4: client soft budget (spec §2.5 layer 2).
            sats = bolt11_amount_sats(bolt11)
            if sats is None and amount_msat:
                sats = round(amount_msat / 1000)
            if self.db is not None and sats:
                budget = self._budget_sats
                if budget is None:
                    from gnostr.wallet.budget import monthly_budget_sats

                    budget = monthly_budget_sats()
                check_budget(self.db, sats, budget)
            params = {"invoice": bolt11}
            if amount_msat:
                params["amount_msat"] = amount_msat
            result = self.call("pay_invoice", params)
            self._record_paid(bolt11, amount_msat or None, result)
            return result
        finally:
            lock.release()
            with self._inflight_guard:
                self._inflight.pop(bolt11, None)

    def _record_paid(self, bolt11, amount_msat, result=None):
        if self.db is None:
            return
        sats = bolt11_amount_sats(bolt11)
        if sats is None and amount_msat:
            sats = round(amount_msat / 1000)
        record_payment(self.db, bolt11, sats or 0, amount_msat or (sats or 0) * 1000)

    def lookup_invoice(self, payment_hash=None, bolt11=None):
        params = {}
        if payment_hash:
            params["payment_hash"] = payment_hash
        if bolt11:
            params["bolt11"] = bolt11
        return self.call("lookup_invoice", params)

    def list_transactions(self, from_ts, until_ts, limit=50, offset=0, type=None):
        params = {"from": from_ts, "until": until_ts, "limit": limit, "offset": offset}
        if type:
            params["type"] = type
        return self.call("list_transactions", params)

    # -- async wrapper (UI pattern: executor + GLib.idle_add) ----------------------

    def call_async(self, method, callback, *args, **kwargs):
        """Run a backend method off the main thread; callback(result_or_exc)
        is delivered via GLib.idle_add (VideoPlayer._executor pattern)."""

        def work():
            try:
                fn = getattr(self, method)
                result = fn(*args, **kwargs)
            except Exception as e:  # surfaced to the UI callback
                result = e
            try:
                from gi.repository import GLib

                GLib.idle_add(lambda: (callback(result), False)[1])
            except Exception:
                callback(result)
            return result

        return _executor.submit(work)


def _tag_value(event, tag_name):
    for tag in event.get("tags") or []:
        if len(tag) >= 2 and tag[0] == tag_name:
            return tag[1]
    return None
