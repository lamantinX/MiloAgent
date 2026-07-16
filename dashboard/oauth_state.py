import secrets
import threading
import time
from typing import Optional, Dict, Any

class OAuthStateStore:
    def __init__(self, max_capacity: int = 1000):
        self._store: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._max_capacity = max_capacity

    def generate_state(self, purpose: str, business_id: str, account_id: str, redirect_to: str, ttl_seconds: int = 600) -> str:
        with self._lock:
            self._purge_expired()
            if len(self._store) >= self._max_capacity:
                raise RuntimeError("OAuthStateStore is at maximum capacity")

            state_token = secrets.token_urlsafe(32)
            while state_token in self._store:
                state_token = secrets.token_urlsafe(32)

            now = time.time()
            self._store[state_token] = {
                "purpose": purpose,
                "business_id": business_id,
                "account_id": account_id,
                "redirect_to": redirect_to,
                "created_at": now,
                "expires_at": now + ttl_seconds
            }
            return state_token

    def consume(self, state_token: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._purge_expired()
            if state_token in self._store:
                return self._store.pop(state_token)
            return None

    def _purge_expired(self, current_time: Optional[float] = None):
        now = current_time or time.time()
        expired = [token for token, data in self._store.items() if data["expires_at"] <= now]
        for token in expired:
            del self._store[token]

# Global store for the dashboard
oauth_store = OAuthStateStore()
