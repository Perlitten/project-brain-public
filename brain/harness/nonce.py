"""Remote Nonce Challenge Store & Protocol."""

import secrets
import time
from typing import Dict, Set


class RemoteNonceManager:
    """Manages challenge nonces for remote execution verification to prevent replay attacks."""

    def __init__(self, ttl_seconds: int = 300):
        self.ttl_seconds = ttl_seconds
        self._issued_nonces: Dict[str, float] = {}
        self._consumed_nonces: Set[str] = set()

    def generate_nonce(self) -> str:
        """Generates a cryptographically secure random nonce."""
        nonce = f"nonce-{secrets.token_hex(16)}"
        self._issued_nonces[nonce] = time.time()
        return nonce

    def verify_and_consume(self, nonce: str) -> bool:
        """Verifies nonce validity and ensures single-use consumption."""
        if not nonce or nonce in self._consumed_nonces:
            return False
        issued_at = self._issued_nonces.get(nonce)
        if not issued_at:
            return False
        if time.time() - issued_at > self.ttl_seconds:
            del self._issued_nonces[nonce]
            return False

        self._consumed_nonces.add(nonce)
        del self._issued_nonces[nonce]
        return True
