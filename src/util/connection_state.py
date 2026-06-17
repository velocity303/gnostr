from dataclasses import dataclass, field
import time
from typing import Literal

# Defines an enumerative, structured state for connections, 
# providing a stable data payload regardless of minor UI changes.
ConnectionStatusLiteral = Literal['CONNECTED', 'DISCONNECTED', 'ERROR', 'WARNING']

@dataclass(frozen=True)
class ConnectionState:
    """
    Represents the canonical connection status of a single endpoint (e.g., one relay).
    This structured object replaces simple string status reports and is designed 
    to be consumed directly by UI/View services for rendering colored buttons.
    """
    status: ConnectionStatusLiteral
    details: str  # Detailed message (e.g., "Ping acknowledged", "Lost connection via TLS handshake")
    timestamp: float = field(default_factory=time.time)

    CONNECTED: "ConnectionState" = None
    WARNING: "ConnectionState" = None
    DISCONNECTED: "ConnectionState" = None

ConnectionState.CONNECTED = ConnectionState(status='CONNECTED', details='🟢 Connected')
ConnectionState.WARNING = ConnectionState(status='WARNING', details='🟡 Warning/Error')
ConnectionState.DISCONNECTED = ConnectionState(status='DISCONNECTED', details='🔴 Disconnected')

# Simple placeholder for completeness, assuming the main app logic will use this structure.
def get_current_state():
    """Mock function to retrieve a default state."""
    return ConnectionState(status='DISCONNECTED', details='Awaiting connection initialization.')