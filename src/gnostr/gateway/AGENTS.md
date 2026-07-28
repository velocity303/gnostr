# DOX — src/gnostr/gateway/

## Purpose
Nostr relay connection gateway — manages WebSocket connections to relays, handles reconnection, and provides a clean interface for the client layer.

## Ownership
- `gateway.py` — Gateway class: relay WebSocket connection, message parsing, reconnection logic

## Local Contracts
- Gateway is instantiated by NostrClient, not by UI code
- All relay communication flows through Gateway — no direct WebSocket access from other modules
- Reconnection is automatic on disconnect with timeout backoff
- Gateway parses incoming Nostr events and dispatches to registered callbacks

## Work Guidance
- Gateway uses Python's `websockets` library for relay connections
- Event parsing follows NIP-01 protocol: JSON array with [type, ...] structure
- On connection loss, Gateway waits 3 seconds before reconnecting
- Gateway tracks connection state and exposes it via status callbacks
- Do not add UI-relevant logic here — this is a transport layer only

## Verification
- `pytest tests/test_gateway.py` — gateway connection and event parsing tests pass
- Mock WebSocket connections in tests — never connect to real relays
