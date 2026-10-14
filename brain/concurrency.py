"""Per-object synchronization that preserves model and dataclass copy semantics."""
from __future__ import annotations

from threading import RLock
from typing import Any


class StateLock:
    """Reentrant lock; copied/deserialized objects own independent locks."""

    def __init__(self) -> None:
        self._lock = RLock()

    def __enter__(self) -> StateLock:
        self._lock.acquire()
        return self

    def __exit__(self, *args: Any) -> None:
        self._lock.release()

    def __eq__(self, other: object) -> bool:
        # Pydantic includes private attributes in model equality. Synchronization
        # machinery must not change equality of otherwise identical claims.
        return isinstance(other, StateLock)

    def __deepcopy__(self, memo: dict[int, Any]) -> StateLock:
        copied = type(self)()
        memo[id(self)] = copied
        return copied

    def __getstate__(self) -> dict[str, Any]:
        return {}

    def __setstate__(self, state: dict[str, Any]) -> None:
        self._lock = RLock()
