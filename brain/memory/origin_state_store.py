"""Read and write :class:`RepositoryOriginState` rows. Never raises: freshness must not fail on it."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from brain.database.models import RepositoryOriginState
from brain.database.session import async_session_factory
from brain.indexers.source_mirror import redact_url

_FIELDS = (
    "remote_url",
    "tracked_branch",
    "source_mode",
    "remote_head",
    "remote_head_seen_at",
    "polled_at",
    "poll_status",
    "poll_error",
    "mirror_head",
    "commits_behind",
    "commits_behind_base",
    "commits_behind_head",
    "last_reindex_job_id",
    "last_reindex_head",
    "last_reindex_requested_at",
    "updated_at",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_dict(row: RepositoryOriginState) -> dict[str, Any]:
    return {"repository_id": row.repository_id, **{name: getattr(row, name) for name in _FIELDS}}


async def get_origin_state(repository_id: int) -> dict[str, Any] | None:
    try:
        async with async_session_factory() as session:
            row = await session.get(RepositoryOriginState, repository_id)
            return _as_dict(row) if row is not None else None
    except Exception as exc:  # a missing table or a DB outage is not a freshness verdict
        logger.debug("origin state for repository {} unavailable: {}", repository_id, exc)
        return None


async def list_origin_states() -> dict[int, dict[str, Any]]:
    try:
        async with async_session_factory() as session:
            rows = (await session.execute(select(RepositoryOriginState))).scalars().all()
            return {row.repository_id: _as_dict(row) for row in rows}
    except Exception as exc:
        logger.debug("origin states unavailable: {}", exc)
        return {}


async def _write(repository_id: int, values: dict[str, Any]) -> None:
    async with async_session_factory() as session:
        async with session.begin():
            row = await session.get(RepositoryOriginState, repository_id)
            if row is None:
                row = RepositoryOriginState(repository_id=repository_id)
                session.add(row)
            for name, value in values.items():
                setattr(row, name, value)
            row.updated_at = utc_now()


async def upsert_origin_state(repository_id: int, **values: Any) -> bool:
    """Merge ``values`` into the repository's row, creating it when missing. False on failure."""
    unknown = set(values) - set(_FIELDS)
    if unknown:
        raise ValueError(f"Unknown origin state field(s): {', '.join(sorted(unknown))}")
    # Both are shown in the UI: whatever the caller passed, no credential is stored.
    for name in ("remote_url", "poll_error"):
        if values.get(name):
            values[name] = redact_url(values[name])
    for attempt in range(2):
        try:
            await _write(repository_id, values)
            return True
        except IntegrityError:
            # A concurrent writer created the row first; the second pass updates it.
            if attempt:
                break
        except Exception as exc:
            logger.warning("Could not record origin state for repository {}: {}", repository_id, exc)
            return False
    logger.warning("Could not record origin state for repository {}: concurrent insert", repository_id)
    return False
