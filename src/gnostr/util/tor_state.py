# src/gnostr/util/tor_state.py
"""Tor/.onion reachability state machine (spec gnostr-tor-onion-support-v1 §A.2.1).

Data layer only — no widgets, no GTK imports: testable headless. The probe is
greeting-only per RFC 1928 §3: send ``b"\\x05\\x01\\x00"`` (VER=5, NMETHODS=1,
METHOD=no-auth), expect ``b"\\x05\\x00"``. A bare TCP connect cannot distinguish
tor from any listener; a full CONNECT consumes circuit time and mutates tor
state. v1 supports no proxy auth, so ``b"\\x05\\x02"`` (auth-required) is
correctly DOWN with a distinguishing reason for the tooltip.

Honest caveat (spec A.2.1): tor opens its SOCKS port before bootstrap
completes, so REACHABLE can briefly precede working circuits; the per-relay
exponential backoff in client.py absorbs the bootstrap window.
"""

import socket
from enum import Enum
from urllib.parse import urlsplit

# SOCKS5 greeting: VER=5, NMETHODS=1, METHOD=no-auth (RFC 1928 §3)
SOCKS5_GREETING = b"\x05\x01\x00"
# Reply selecting no-auth
SOCKS5_OK = b"\x05\x00"
# Reply demanding authentication (unsupported in v1)
SOCKS5_AUTH_REQUIRED = b"\x05\x02"


class TorState(Enum):
    DISABLED = "disabled"  # proxy-enabled == false
    REACHABLE = "reachable"  # proxy enabled, SOCKS5 greeting OK
    DOWN = "down"  # proxy enabled, probe failed


def host_is_onion(url):
    """True iff the URL's HOSTNAME ends with '.onion' (case-insensitive).

    hostname-based, NOT string-suffix: 'ws://x.onion.evil.example' -> False.
    """
    try:
        hostname = urlsplit(url).hostname or ""
    except ValueError:
        return False
    return hostname.lower().endswith(".onion")


def probe_socks5_detail(host, port, timeout=2.0):
    """SOCKS5 greeting probe; returns (reachable, reason).

    reasons: 'ok' | 'auth' (proxy wants auth — unsupported) | 'closed'
    (connection dropped without reply) | 'refused' | 'timeout' | 'error'.
    Never sends CONNECT: zero side effects, zero circuit cost.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(SOCKS5_GREETING)
            reply = sock.recv(2)
        if reply == SOCKS5_OK:
            return True, "ok"
        if reply == SOCKS5_AUTH_REQUIRED:
            return False, "auth"
        return False, "closed" if not reply else "error"
    except socket.timeout:
        return False, "timeout"
    except ConnectionRefusedError:
        return False, "refused"
    except OSError:
        return False, "error"


def probe_socks5(host, port, timeout=2.0):
    """True iff a no-auth SOCKS5 proxy answers the greeting.

    Anything else (refused, timeout, short read, b'\\x05\\x02' auth-required,
    garbage) -> False.
    """
    ok, _reason = probe_socks5_detail(host, port, timeout)
    return ok


def onion_inert(url, proxy_enabled, tor_state):
    """Skip gate (spec A.2.2): an onion relay is inert iff it is an onion
    host and the proxy is not (enabled AND REACHABLE). Inert relays are never
    instantiated as NostrRelay — no connection attempts, no retry storms, no
    error spam, and they never touch the aggregate status-changed."""
    return host_is_onion(url) and not (
        proxy_enabled and tor_state == TorState.REACHABLE
    )
