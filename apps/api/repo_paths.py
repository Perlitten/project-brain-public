"""Repository-path sandbox refusals reported as HTTP 400 instead of escaping as 500."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from fastapi import HTTPException


@contextmanager
def repo_path_errors_as_400() -> Iterator[None]:
    """Wrap only the ``resolve_repo_path`` / ``validate_secure_repo_path`` call.

    The validator raises ``ValueError`` for a path outside the allowed roots, a
    missing path and a non-directory. Unhandled, that surfaced as HTTP 500,
    which reads as a server fault rather than a bad request. 400 with the
    validator's message matches the drift and workspace routers.
    """
    try:
        yield
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
