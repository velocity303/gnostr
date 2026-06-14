# gateway.py
import abc
from typing import Optional, List, Dict, Any

class AbstractRepository(abc.ABC):
    """
    Abstract Base Class for all data repositories in gnostr.
    Defines the required contract (interface) that concrete implementations 
    (e.g., DatabaseGateway, MockDatabaseGateway) must adhere to.
    """

    @abc.abstractmethod
    def close(self):
        """Cleanly closes any underlying connection resources."""
        raise NotImplementedError("Subclasses must implement the close method.")

# --- Data Gateway Interfaces (The Contract) ---

class IKeyValueStore(AbstractRepository):
    @abc.abstractmethod
    def get_key(self, key: str) -> Optional[str]:
        """Retrieve a single stored value by key."""
        pass

    @abc.abstractmethod
    def set_key(self, key: str, value: Any) -> None:
        """Store or replace a value associated with a key."""
        pass

class IPostgreSQLRepository(AbstractRepository):
    # Represents connection/session management for complex relational data
    @abc.abstractmethod
    def execute_query(self, query: str, params: Optional[Dict] = None) -> List[Dict]:
        """Execute a parameterized SQL query and return results as list of dicts."""
        pass

class IEventRepository(AbstractRepository):
    # Deals specifically with Nostr Event and Group data
    @abc.abstractmethod
    def save_event(self, event: Dict) -> str:
        """Saves a complete event structure (id, kind, pubkey, content). Returns event ID/row key."""
        pass

    @abc.abstractmethod
    def find_events(self, criteria: Dict, limit: Optional[int] = None) -> List[Dict]:
        """Retrieves events based on robust filtering criteria (e.g., kind=1 AND authors=[pubkey])."""
        pass


# --- Concrete Implementation Placeholder ---

class DatabaseGateway(IKeyValueStore, IEventRepository):
    """
    The concrete implementation that interfaces with the actual database 
    (e.g., a SQLAlchemy wrapper or specialized Redis connection).
    This class will be initialized/subclassed to connect to SQLite, Postgres, etc.
    """
    def __init__(self, config: Dict):
        # In a real app, this would initialize connection pooling (DB connection setup)
        self._config = config
        print(f"INFO: Initializing DatabaseGateway using configuration for {config.get('type', 'unknown')}.")

    def close(self):
        """Placeholder to ensure resources are cleaned up."""
        # Example: self.connection_pool.close()
        pass

    # --- IKeyValueStore Methods ---
    def get_key(self, key: str) -> Optional[str]:
        print(f"DEBUG: Retrieving simple key '{key}' from DB.")
        # Replace with actual DB query accessing a single key-value pair
        return None 

    def set_key(self, key: str, value: Any) -> None:
        print(f"DEBUG: Setting complex key '{key}' in DB.")
        # Replace with actual DB write operation (e.g., JSON blob storage)
        pass

    # --- IEventRepository Methods ---
    def save_event(self, event: Dict) -> str:
        """Implements saving of a structured Nostr event."""
        print(f"DEBUG: Persisting new event for {event.get('pubkey')}.")
        # Logic to map Python dict to SQL/NoSQL schema & execute INSERT.
        return "mock_event_id_xyz123"

    def find_events(self, criteria: Dict, limit: Optional[int] = None) -> List[Dict]:
        """Performs structured retrieval of events (e.g., feed queries)."""
        print(f"DEBUG: Searching for events with criteria: {criteria}, limited to {limit}.")
        # Mock response simulating a list of dictionaries returned from the DB
        return [
            {"id": "mock_event1", "pubkey": "fakepub1", "content": "Mock post 1.", "created_at": time.time(), "tags": []},
            {"id": "mock_event2", "pubkey": "fakepub2", "content": "Mock post 2.", "created_at": 0, "tags": []}
        ]

# Example Usage Test (Run this file test to verify interfaces)
if __name__ == "__main__":
    print("--- Testing Gateway Contracts ---")
    db = DatabaseGateway({"type": "SQLite"})
    try:
        db.set_key("user_pref", {"theme": "dark"})
        db.get_key("user_pref")
        results = db.find_events({"kind": 1, "authors": ["fakepub"]})
        print(f"Successfully called find_events: {len(results)} results.")
    finally:
        db.close()