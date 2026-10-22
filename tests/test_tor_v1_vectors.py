"""tor-v1-tests (spec gnostr-tor-onion-support-v1 §6 vectors V1/V2/V3-gap).

The sibling tor cards shipped their own unit tests (test_tor_ws_proxy.py,
test_tor_state.py, test_proxy_opener.py, test_proxy_settings.py). This file
closes the remaining §6 vectors that no sibling covered:

- V1 — onion v3 derivation fixture: seed bytes(range(32)) -> ed25519 pubkey
  03a107bf... -> aoqqpp7tzyil4hlq3umoos6atft6jvrqtosq2xy53sdgiesvgg4jncad.onion,
  computed here with a stdlib-only ed25519 (no `cryptography` dep in the
  Flatpak runtime) and used as the fixture for the onion-address validator
  contract: length 56, base32 charset (RFC 4648 lower, no padding), and the
  v3 checksum sha256('.onion checksum' || pubkey || version)[:2].
- V2 — remote-DNS assertion: a real websocket-client socks5h connect through
  a mock SOCKS5 server must put the onion HOSTNAME on the wire (CONNECT
  ATYP=0x03 + full onion bytes + port), and must never resolve the onion
  locally (socket.getaddrinfo patched to raise for the onion host).
- V3 gap — websocket-client raises WebSocketException('Python Socks is
  needed...') when python-socks is absent (regression guard for the manifest
  dep shipped by tor-v1-deps).

V3's happy half (proxy_info -> proxy_protocol == 'socks5h') lives in
test_tor_ws_proxy.py::test_v3_websocket_proxy_info_protocol; V5 (LUD-16
onion -> http://) lives in test_proxy_opener.py.
"""

import base64
import hashlib
import socket
import threading
import types

import pytest

from gnostr import proxy_settings as ps

TEST_ONION = "aoqqpp7tzyil4hlq3umoos6atft6jvrqtosq2xy53sdgiesvgg4jncad.onion"
TEST_ONION_LABEL = TEST_ONION[: -len(".onion")]
TEST_PUBKEY_HEX = "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"

BASE32_CHARSET = "abcdefghijklmnopqrstuvwxyz234567"


# ---------------------------------------------------------------------------
# stdlib ed25519 (RFC 8032 §5.1 / §7): the Flatpak runtime ships no
# `cryptography`, so the derivation fixture must be pure Python.
# ---------------------------------------------------------------------------

_P = 2**255 - 19
_D = (-121665 * pow(121666, -1, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _inv(x):
    return pow(x, _P - 2, _P)


def _recover_x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1) % _P
    if x2 == 0:
        return None
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if x % 2 != sign:
        x = _P - x
    return x


_BY = 4 * _inv(5) % _P
_B = (_recover_x(_BY, 0), _BY)


def _point_add(P, Q):
    x1, y1 = P
    x2, y2 = Q
    den1 = (1 + _D * x1 * x2 * y1 * y2) % _P
    den2 = (1 - _D * x1 * x2 * y1 * y2) % _P
    x3 = (x1 * y2 + x2 * y1) * _inv(den1) % _P
    y3 = (y1 * y2 + x1 * x2) * _inv(den2) % _P
    return (x3, y3)


def _scalar_mult(s, P):
    Q = None
    while s:
        if s & 1:
            Q = P if Q is None else _point_add(Q, P)
        P = _point_add(P, P)
        s >>= 1
    return Q


def ed25519_public_key(seed):
    """RFC 8032 public key (32-byte compressed) for a 32-byte seed."""
    assert len(seed) == 32
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    x, y = _scalar_mult(a, _B)
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


# ---------------------------------------------------------------------------
# onion v3 address derivation + validator (tor onboarding-spec v3, §5)
# ---------------------------------------------------------------------------


def onion_address_from_pubkey(pubkey):
    """base32(pubkey || sha256('.onion checksum'||pubkey||ver)[:2] || ver)."""
    version = b"\x03"
    checksum = hashlib.sha256(b".onion checksum" + pubkey + version).digest()[:2]
    raw = pubkey + checksum + version
    return base64.b32encode(raw).decode().lower().rstrip("=") + ".onion"


def is_valid_onion_address(address):
    """Onion v3 validator: length 56, base32 charset, checksum, version 3."""
    label = address[: -len(".onion")] if address.endswith(".onion") else address
    if len(label) != 56:
        return False
    if any(c not in BASE32_CHARSET for c in label.lower()):
        return False
    try:
        raw = base64.b32decode(label.upper(), casefold=True)
    except Exception:
        return False
    if len(raw) != 35:
        return False
    pubkey, checksum, version = raw[:32], raw[32:34], raw[34:35]
    if version != b"\x03":
        return False
    expect = hashlib.sha256(b".onion checksum" + pubkey + version).digest()[:2]
    return expect == checksum


# ---------------------------------------------------------------------------
# V1 — derivation fixture + validator vectors
# ---------------------------------------------------------------------------


def test_v1_seed_derives_spec_pubkey():
    assert ed25519_public_key(bytes(range(32))).hex() == TEST_PUBKEY_HEX


def test_v1_pubkey_derives_spec_onion():
    pubkey = bytes.fromhex(TEST_PUBKEY_HEX)
    assert onion_address_from_pubkey(pubkey) == TEST_ONION


def test_v1_validator_accepts_spec_address():
    assert is_valid_onion_address(TEST_ONION) is True
    # shape contract from the spec: length 56 + base32 charset
    assert len(TEST_ONION_LABEL) == 56
    assert all(c in BASE32_CHARSET for c in TEST_ONION_LABEL)


@pytest.mark.parametrize(
    "bad,why",
    [
        ("a" * 56 + ".onion", "charset ok, checksum garbage"),
        (TEST_ONION_LABEL[:-1] + "b" + ".onion", "flipped last char breaks checksum"),
        (TEST_ONION_LABEL[:55] + "1" + ".onion", "'1' is not in the base32 charset"),
        (TEST_ONION_LABEL[:-1] + ".onion", "55 chars"),
        (TEST_ONION_LABEL + "aa" + ".onion", "58 chars"),
        ("relay.example", "clearnet host"),
    ],
)
def test_v1_validator_rejects(bad, why):
    assert is_valid_onion_address(bad) is False, why


def test_v1_checksum_covers_pubkey():
    # A different pubkey under the same tail bytes must fail the checksum —
    # the checksum binds the key, it is not a fixed trailer.
    other = ed25519_public_key(bytes(reversed(range(32))))
    tampered = base64.b32encode(
        other + base64.b32decode(TEST_ONION_LABEL.upper())[32:]
    ).decode().lower().rstrip("=") + ".onion"
    assert is_valid_onion_address(tampered) is False


# ---------------------------------------------------------------------------
# V2 — mock SOCKS5 server: CONNECT carries the onion hostname, no local DNS
# ---------------------------------------------------------------------------


class _MockSocksServer:
    """Minimal SOCKS5 server: answers the no-auth greeting, records the full
    CONNECT request, replies with a canned success, then holds the socket
    open (the client only needs the tunnel established)."""

    def __init__(self):
        self.greeting = b""
        self.connect_request = b""
        self.atyp = None
        self.target_host = None
        self.target_port = None
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.sock.listen(1)
        self.sock.settimeout(10)
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(10)
            try:
                head = self._recv_exact(conn, 2)  # VER NMETHODS
                self.greeting = head + self._recv_exact(conn, head[1])  # METHODS
                conn.sendall(b"\x05\x00")  # select no-auth
                head = self._recv_exact(conn, 4)  # VER CMD RSV ATYP
                atyp = head[3]
                self.atyp = atyp
                if atyp == 0x03:  # domain name
                    hlen = self._recv_exact(conn, 1)[0]
                    host_bytes = self._recv_exact(conn, hlen)
                elif atyp == 0x01:  # IPv4
                    host_bytes = self._recv_exact(conn, 4)
                else:
                    return
                port_bytes = self._recv_exact(conn, 2)
                self.connect_request = head + host_bytes + port_bytes
                self.target_host = host_bytes.decode(errors="replace")
                self.target_port = int.from_bytes(port_bytes, "big")
                # success: VER REP RSV ATYP(=IPv4) + BND.ADDR(4) + BND.PORT(2)
                # — python-socks reads exactly 10 bytes here
                conn.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
                # hold the tunnel open until the client is done with it
                conn.settimeout(10)
                try:
                    conn.recv(1)
                except OSError:
                    pass
            except OSError:
                pass

    @staticmethod
    def _recv_exact(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise OSError("closed")
            buf += chunk
        return buf

    def stop(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _ws_options():
    return types.SimpleNamespace(sockopt=[], timeout=5, sslopt=None)


def test_v2_socks5h_connect_sends_onion_hostname_not_local_dns(monkeypatch):
    """Spec V2: with the proxy kwargs from proxy_settings, a real
    websocket-client connect must ship the onion hostname inside the SOCKS5
    CONNECT (ATYP 0x03) and must never hand the onion to the local resolver."""
    pytest.importorskip("python_socks")
    import websocket._http as wshttp

    srv = _MockSocksServer()
    try:
        calls = []

        real_getaddrinfo = socket.getaddrinfo

        def guarded_getaddrinfo(host, *a, **kw):
            calls.append(host)
            if host == TEST_ONION:
                raise AssertionError(f"local DNS leak: getaddrinfo({host!r})")
            return real_getaddrinfo(host, *a, **kw)

        monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)

        kw = ps.socks_proxy_kwargs(
            {"proxy-enabled": True, "proxy-host": "127.0.0.1", "proxy-port": srv.port}
        )
        assert kw["proxy_type"] == "socks5h"
        proxy = wshttp.proxy_info(**kw)
        url = f"ws://{TEST_ONION}:8011"
        sock, (host, port, _resource) = wshttp.connect(url, _ws_options(), proxy, None)
        try:
            sock.close()
        finally:
            srv.thread.join(timeout=5)
            srv.stop()

        # greeting: SOCKS5 no-auth offered first (python-socks offers
        # 00/02/03; the probe-only capture of the exact 3-byte greeting
        # lives in test_tor_state.py)
        assert srv.greeting[0] == 0x05
        assert srv.greeting[2] == 0x00
        # CONNECT: ATYP=0x03 (domain), onion bytes verbatim, port 8011
        assert srv.atyp == 0x03
        assert srv.target_host == TEST_ONION
        assert srv.target_port == 8011
        assert TEST_ONION.encode() in srv.connect_request
        # remote DNS: the onion never reached the local resolver
        assert TEST_ONION not in calls
        assert host == TEST_ONION and port == 8011
    finally:
        srv.stop()


def test_v2_socks5_local_dns_variant_sends_ip_atyp(monkeypatch):
    """Complement: proxy-remote-dns disabled => 'socks5' (ATYP 0x01 path is
    python-socks resolving locally — which for a .onion would fail). Guards
    that the socks5h/socks5 split really switches rdns."""
    pytest.importorskip("python_socks")
    from python_socks._types import ProxyType  # noqa: F401  (import guard)

    kw = ps.socks_proxy_kwargs(
        {"proxy-enabled": True, "proxy-remote-dns": False, "proxy-host": "h", "proxy-port": 1}
    )
    assert kw["proxy_type"] == "socks5"


# ---------------------------------------------------------------------------
# V3 gap — python-socks absence is a loud failure, never a silent direct
# ---------------------------------------------------------------------------


def test_v3_websocket_exception_when_python_socks_absent(monkeypatch):
    """Spec V3 regression guard: if the flatpak manifest ever drops the
    python-socks module, a socks5h connect must raise WebSocketException
    ('Python Socks is needed...') — never fall back to a direct clearnet
    socket."""
    import websocket._http as wshttp
    from websocket._exceptions import WebSocketException

    monkeypatch.setattr(wshttp, "HAVE_PYTHON_SOCKS", False)
    proxy = wshttp.proxy_info(
        http_proxy_host="127.0.0.1", http_proxy_port=9050, proxy_type="socks5h"
    )
    assert proxy.proxy_protocol == "socks5h"
    with pytest.raises(WebSocketException, match="Python Socks is needed"):
        wshttp._start_proxied_socket(f"ws://{TEST_ONION}:8011", _ws_options(), proxy)
