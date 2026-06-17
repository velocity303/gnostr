from enum import Enum
import typing

class ConnectionStatus(Enum):
    """Type-safe representation of connection states."""
    CONNECTED = "Connected" 
    DISCONNECTED = "Disconnected"
    CONNECTING = "Connecting" # Transient state placeholder, e.g., during handshake

def status_from_string(raw_state: str) -> ConnectionStatus:
    """
    Maps a raw string (e.g., from an API response or internal flag) 
    to a structured ConnectionStatus Enum member.
    Raises ValueError if the state is unrecognized.
    """
    # Standardized spelling check for common pitfalls
    if isinstance(raw_state, str):
        normalized_state = raw_state.strip()
        if "Connected" in normalized_state:
            return ConnectionStatus.CONNECTED
        elif "Disconnected" in normalized_state:
            return ConnectionStatus.DISCONNECTED
        elif "Connecting" in normalized_state:
            return ConnectionStatus.CONNECTING

    # Fallback for other unexpected inputs (like None, or empty string)
    raise TypeError(f"Invalid raw state input of type {type(raw_state).__name__} and value '{raw_state}'")