from __future__ import annotations

from typing import Any


class PermanentJobError(RuntimeError):
    """A completed or invalid operation that must not be retried unchanged."""

    def __init__(self, message: str, *, result: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.result = result


class StaleWorkerFencingError(PermanentJobError):
    """Raised when a worker's fencing token no longer owns the job lease."""
