"""Remote Nonce Challenge Store & Protocol."""

import secrets
import time
from brain.concurrency import StateLock


def _elapsed_time() -> float:
    """Use suspend-aware monotonic time where available; portable monotonic fallback."""
    clock_id = getattr(time, "CLOCK_BOOTTIME", None)
    if clock_id is not None:
        try:
            return time.clock_gettime(clock_id)
        except OSError:
            # The Python constant may exist on a kernel without this clock.
            pass
    return time.monotonic()


class RemoteNonceManager:
    """Manages challenge nonces for remote execution verification to prevent replay attacks."""

    def __init__(self, ttl_seconds: int = 300):
        self.ttl_seconds = ttl_seconds
        self._issued_nonces: dict[str, float] = {}
        self._consumed_nonces: dict[str, float] = {}
        self._lock = StateLock()

    def _evict_expired(self, now: float) -> None:
        # Replay markers need live only through the original challenge's TTL.
        for records in (self._issued_nonces, self._consumed_nonces):
            expired = [nonce for nonce, issued in records.items() if now - issued > self.ttl_seconds]
            for nonce in expired:
                del records[nonce]

    def generate_nonce(self) -> str:
        """Generates a cryptographically secure random nonce."""
        with self._lock:
            now = _elapsed_time()
            self._evict_expired(now)
            nonce = f"nonce-{secrets.token_hex(16)}"
            self._issued_nonces[nonce] = now
            return nonce

    def verify_and_consume(self, nonce: str) -> bool:
        """Consume once within the inclusive TTL, measured by a monotonic clock."""
        with self._lock:
            if not nonce or nonce in self._consumed_nonces:
                self._issued_nonces.pop(nonce, None)
                return False
            issued_at = self._issued_nonces.get(nonce)
            if issued_at is None:
                return False
            if _elapsed_time() - issued_at > self.ttl_seconds:
                del self._issued_nonces[nonce]
                return False

            self._consumed_nonces[nonce] = issued_at
            del self._issued_nonces[nonce]
            return True
