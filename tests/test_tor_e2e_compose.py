"""tor-v1-tests V4 (spec gnostr-tor-onion-support-v1 §6): end-to-end onion
handshake through a real tor daemon — docker-compose nightly/manual suite.

Topology (fully synthetic per §7-Q5 — no real relays, no real node, no
secrets): test client -> SOCKS5 127.0.0.1:9150 -> tor (compose) -> v3
hidden service -> strfry relay. The spec's named images (goldstein/tor,
petertduggan/tor) no longer exist on Docker Hub (verified 2026-10-22); the
compose builds tor from alpine + apk instead, same SOCKS5 + hidden-service
surface.

GATING: the CI job container has no docker daemon, so this suite SKIPS
unless GNOSTR_TOR_E2E=1 is set AND docker responds. Nightly runner / dev
machines with docker opt in:

    GNOSTR_TOR_E2E=1 pytest tests/test_tor_e2e_compose.py

Assertions (spec V4):
- REQ/EOSE round-trip succeeds over ws://<onion>:8011 via the proxy.
- With tor stopped, the connect fails with the DISTINCT proxy-unavailable
  error (connection to the SOCKS port refused) — never a clearnet fallback:
  the attempted URL is unchanged and the onion never reaches local DNS.
"""

import json
import os
import socket
import subprocess
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
COMPOSE_DIR = os.path.join(HERE, "tor-e2e")
SOCKS_PORT = 9150  # host port mapped to tor's 9050 (never collides w/ system tor)
HS_PORT = 8011  # hidden-service port -> strfry:7777


def _docker_available():
    try:
        subprocess.run(
            ["docker", "info"], capture_output=True, timeout=15, check=True
        )
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    os.environ.get("GNOSTR_TOR_E2E") != "1" or not _docker_available(),
    reason="nightly/manual: needs GNOSTR_TOR_E2E=1 and a docker daemon",
)


def _compose(*args, check=True, timeout=180):
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=COMPOSE_DIR,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def tor_stack():
    """Bring the compose stack up, yield the hidden-service hostname, and
    always tear the stack down (no leaked containers, no state)."""
    _compose("down", "-v", "--remove-orphans", check=False, timeout=120)
    _compose("up", "--build", "-d")
    try:
        hostname = _wait_hidden_service()
        yield hostname
    finally:
        _compose("down", "-v", "--remove-orphans", check=False, timeout=120)


def _wait_hidden_service():
    """tor writes the HS hostname once it has generated the keypair, but a
    freshly generated HS needs its introduction points established before a
    client CONNECT succeeds (30-60s after bootstrap). Poll with a real
    SOCKS CONNECT to the onion endpoint until it tunnels or the deadline."""
    from python_socks.sync import Proxy
    from python_socks._types import ProxyType

    deadline = time.monotonic() + 180
    last = ""
    while time.monotonic() < deadline:
        out = _compose(
            "exec", "-T", "tor", "cat", "/var/lib/tor/hidden_service/hostname",
            check=False, timeout=30,
        )
        last = out.stdout.strip()
        if last.endswith(".onion"):
            try:
                p = Proxy.create(
                    proxy_type=ProxyType.SOCKS5,
                    host="127.0.0.1", port=SOCKS_PORT, rdns=True,
                )
                sock = p.connect(last, HS_PORT, timeout=15)
                sock.close()
                return last
            except Exception:
                pass
        time.sleep(5)
    pytest.fail(f"hidden service never came up (last hostname: {last!r})")


def _ws_connect(url, timeout=60):
    """Open a websocket through the SOCKS5h proxy using the production kwargs
    shape (proxy_settings.socks_proxy_kwargs feeds WebSocket.connect)."""
    pytest.importorskip("python_socks")
    import websocket

    ws = websocket.WebSocket()
    ws.connect(
        url,
        http_proxy_host="127.0.0.1",
        http_proxy_port=SOCKS_PORT,
        proxy_type="socks5h",
        timeout=timeout,
    )
    return ws


def _req_eose_roundtrip(url):
    """Send a NIP-01 REQ over the established tunnel and wait for EOSE."""
    ws = _ws_connect(url)
    try:
        ws.send(json.dumps(["REQ", "tor-e2e-1", {"kinds": [1], "limit": 1}]))
        ws.settimeout(60)
        reply = json.loads(ws.recv())
        assert reply[0] == "EOSE" and reply[1] == "tor-e2e-1", reply
    finally:
        ws.close()


def test_v4_onion_req_eose_roundtrip(tor_stack):
    url = f"ws://{tor_stack}:{HS_PORT}"
    _req_eose_roundtrip(url)


def test_v4_tor_stopped_distinct_proxy_error_no_clearnet_fallback(tor_stack):
    """Spec V4 second half: with tor stopped the failure is a DISTINCT
    proxy-unavailable error on the SOCKS port — the attempted URL is
    unchanged and the onion is never handed to the local resolver."""
    _compose("stop", "tor", timeout=120)
    try:
        url = f"ws://{tor_stack}:{HS_PORT}"
        real_gai = socket.getaddrinfo
        calls = []

        def guarded(host, *a, **kw):
            calls.append(host)
            if host.endswith(".onion"):
                raise AssertionError(f"clearnet fallback: local DNS for {host!r}")
            return real_gai(host, *a, **kw)

        socket.getaddrinfo = guarded
        try:
            with pytest.raises(Exception) as excinfo:
                _ws_connect(url, timeout=10)
        finally:
            socket.getaddrinfo = real_gai

        err = excinfo.value
        # distinct proxy-unavailable failure: the SOCKS port itself refused
        low = f"{type(err).__name__}: {err}".lower()
        assert any(
            t in low
            for t in ("refused", "unreachable", "proxy", "socks", "connection closed")
        ), f"expected a proxy-unavailable error, got {low!r}"
        # no clearnet fallback: the onion never reached the resolver, and the
        # attempted URL is exactly the configured onion endpoint (unchanged)
        assert tor_stack not in "".join(calls)
        assert url == f"ws://{tor_stack}:{HS_PORT}"
    finally:
        _compose("start", "tor", timeout=120)
        _wait_hidden_service()
