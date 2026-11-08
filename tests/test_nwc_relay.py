"""Fake NWC wallet over a REAL local websocket relay (spec zaps-payments-v1).

Exercises NWCBackend's default transport (websocket-client, no injection):
a minimal stdlib RFC-6455 relay on loopback plays the wallet service against
the v4_* fixtures. Asserts the 23194 that hits the wire decrypts to
v4_nwc_request_plaintext, the 23195 reply carries the matching `e` tag, and
the kind-13194 info event gates encryption to nip44.

Loopback ws:// is allowed by the URI parser for exactly this test path; the
spending credential never leaves the machine (James' rule: no node identity).
"""

import base64
import hashlib
import json
import os
import socket
import struct
import threading

import pytest

from gnostr import nip44
from gnostr.wallet import nip04
from gnostr.wallet.nwc import NWCBackend

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
WS_MAGIC = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


@pytest.fixture
def vectors():
    with open(os.path.join(FIXTURES, "zap_test_vectors.json")) as f:
        return json.load(f)


def _write_frame(sock, data):
    head = bytearray([0x81])  # FIN + text
    if len(data) < 126:
        head.append(len(data))
    elif len(data) < 65536:
        head.append(126)
        head += struct.pack(">H", len(data))
    else:
        head.append(127)
        head += struct.pack(">Q", len(data))
    sock.sendall(bytes(head) + data)


class LocalWalletRelay:
    """Minimal websocket relay that answers NIP-47 like the fixture wallet."""

    def __init__(self, vectors, encryption="nip44"):
        self.vectors = vectors
        self.encryption = encryption
        self.wallet_sec = vectors["keys"]["nwc_wallet_sec"]
        self.wallet_pub = vectors["keys"]["nwc_wallet_pub"]
        self.decrypted_requests = []
        self.response_etags = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._accept_loop, daemon=True).start()

    # -- socket plumbing ----------------------------------------------------

    def _accept_loop(self):
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()

    def _serve_conn(self, conn):
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            key = None
            for line in buf.split(b"\r\n"):
                if line.lower().startswith(b"sec-websocket-key:"):
                    key = line.split(b":", 1)[1].strip()
            if key is None:
                return
            accept = base64.b64encode(hashlib.sha1(key + WS_MAGIC).digest())
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )
            # bytes already read past the handshake belong to the first frame
            pending = [buf.split(b"\r\n\r\n", 1)[1]]

            def read(n):
                while sum(len(p) for p in pending) < n:
                    chunk = conn.recv(65536)
                    if not chunk:
                        raise ConnectionError("relay socket closed")
                    pending.append(chunk)
                out = b"".join(pending)
                rest = out[n:]
                pending.clear()
                if rest:
                    pending.append(rest)
                return out[:n]

            while not self._stop.is_set():
                try:
                    b0, b1 = read(2)
                    ln = b1 & 0x7F
                    if ln == 126:
                        ln = struct.unpack(">H", read(2))[0]
                    elif ln == 127:
                        ln = struct.unpack(">Q", read(8))[0]
                    mask = read(4) if (b1 & 0x80) else None
                    data = read(ln) if ln else b""
                    if mask:
                        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                    opcode = b0 & 0x0F
                except (ConnectionError, OSError):
                    return
                if opcode == 0x8:
                    return
                if opcode != 0x1:
                    continue
                try:
                    msg = json.loads(data)
                except ValueError:
                    continue
                self._handle(conn, msg)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    # -- NIP-47 wallet behaviour ---------------------------------------------

    def _conv(self, client_pub):
        return nip44.get_conversation_key(
            bytes.fromhex(self.wallet_sec), bytes.fromhex(client_pub)
        )

    def _handle(self, conn, msg):
        if not (isinstance(msg, list) and msg):
            return
        if msg[0] == "REQ" and len(msg) >= 3:
            sub_id, filt = msg[1], msg[2]
            if filt.get("kinds") == [13194]:
                self._send(conn, ["EVENT", sub_id, self.vectors["v4_nwc_info_event"]])
        elif msg[0] == "EVENT" and len(msg) >= 2:
            event = msg[1]
            if event.get("kind") == 23194:
                self._respond(conn, event)

    def _respond(self, conn, request):
        client_pub = request["pubkey"]
        if self.encryption == "nip04":
            plain = nip04.nip04_decrypt(request["content"], self.wallet_sec, client_pub)
        else:
            plain = nip44.decrypt(request["content"], self._conv(client_pub))
        self.decrypted_requests.append(json.loads(plain))
        req = json.loads(plain)
        method = req["method"]
        if method == "pay_invoice":
            result = json.loads(self.vectors["v4_nwc_response_plaintext"])["result"]
        elif method == "get_balance":
            result = {"balance_msat": 21_000_000_000}
        else:
            result = {}
        body = json.dumps(
            {"result_type": method, "error": None, "result": result},
            separators=(",", ":"),
        )
        if self.encryption == "nip04":
            content = nip04.nip04_encrypt(body, self.wallet_sec, client_pub)
        else:
            content = nip44.encrypt(body, self._conv(client_pub))
        response = {
            "kind": 23195,
            "pubkey": self.wallet_pub,
            "content": content,
            "created_at": 1763136000,
            "tags": [["p", client_pub], ["e", request["id"]]],
        }
        self.response_etags.append(request["id"])
        # NIP-01 wire form: ["EVENT", <subscription_id>, <event>]
        self._send(conn, ["EVENT", "nwc-res-" + request["id"][:8], response])

    def _send(self, conn, msg):
        try:
            _write_frame(conn, json.dumps(msg).encode())
        except OSError:
            pass


@pytest.fixture(autouse=True)
def _no_gsettings(monkeypatch):
    """conftest mocks gi globally; force the dev-run path so the default
    transport's socks_proxy_kwargs() returns {} instead of Mock ints."""
    from gnostr import proxy_settings

    monkeypatch.setattr(proxy_settings, "_settings", lambda: None)


def _uri(vectors, port):
    return (
        f"nostr+walletconnect://{vectors['keys']['nwc_wallet_pub']}"
        f"?relay=ws://127.0.0.1:{port}"
        f"&secret={vectors['keys']['nwc_client_sec']}"
    )


def test_wire_pay_invoice_matches_fixture(vectors):
    """Default transport: 23194 on the wire decrypts byte-for-byte to the
    fixture plaintext; the 23195 e tag matches the request id."""
    relay = LocalWalletRelay(vectors)
    try:
        be = NWCBackend(_uri(vectors, relay.port), timeout_s=10.0)
        invoice = json.loads(vectors["v4_nwc_request_plaintext"])["params"]["invoice"]
        result = be.call("pay_invoice", {"invoice": invoice})
        be.close()
        assert (
            result["preimage"]
            == json.loads(vectors["v4_nwc_response_plaintext"])["result"]["preimage"]
        )
        # the request that hit the wire decrypts to the fixture plaintext
        assert relay.decrypted_requests == [
            json.loads(vectors["v4_nwc_request_plaintext"])
        ]
        # e tag on the reply matched the request event id (client matched it
        # or the call would have timed out)
        assert len(relay.response_etags) == 1
    finally:
        relay.stop()


def test_wire_info_event_sets_nip44(vectors):
    relay = LocalWalletRelay(vectors)
    try:
        be = NWCBackend(_uri(vectors, relay.port), timeout_s=10.0)
        info = be.fetch_info_event()
        assert info is not None
        assert be._encryption == "nip44"
        assert "pay_invoice" in be._capabilities
        be.close()
    finally:
        relay.stop()


def test_wire_nip04_legacy_roundtrip(vectors):
    relay = LocalWalletRelay(vectors, encryption="nip04")
    try:
        be = NWCBackend(_uri(vectors, relay.port), timeout_s=10.0)
        be._encryption = "nip04"
        be._capabilities = ["get_balance"]
        assert be.get_balance() == {"balance_msat": 21_000_000_000}
        be.close()
    finally:
        relay.stop()
