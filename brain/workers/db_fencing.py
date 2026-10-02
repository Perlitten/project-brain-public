"""Postgres-backed job fencing — the durable authority behind the Redis lease.

A Redis lease check before a database commit leaves a race: the worker can
pass the check and still commit domain writes after losing ownership. The
lease row in ``worker_job_leases`` makes the fence part of the commit itself:

- ``claim_db_lease`` takes a ``FOR UPDATE`` lock on the lease row and only
  takes it when the row is absent, expired, or already ours — so a competing
  claim serializes against an in-flight fenced transaction instead of
  interposing mid-commit.
- ``assert_db_fence`` locks the same row inside the caller's transaction and
  verifies token + expiry. While that transaction is open, no other worker
  can complete a claim — the token check is atomic with the writes.
- ``renew_db_lease`` extends expiry under the same compare-and-set; a refused
  renewal means ownership moved.
- ``release_db_lease`` deletes only the owner's row.

``current_job_fence`` carries the fence for the running job so deep call
sites (e.g. the file indexer's write transactions) can re-assert ownership
without signature changes; ``assert_current_db_fence`` is a no-op when no
fence is installed.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from brain.database.models import WorkerJobLease
from brain.database.session import async_session_factory
from brain.workers.errors import StaleWorkerFencingError


@dataclass(frozen=True)
class DbFence:
    job_id: str
    fencing_token: str


current_job_fence: ContextVar[Optional[DbFence]] = ContextVar(
    "current_job_fence", default=None
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _lease_row(
    session: AsyncSession, job_id: str
) -> Optional[WorkerJobLease]:
    res = await session.execute(
        select(WorkerJobLease)
        .where(WorkerJobLease.job_id == job_id)
        .with_for_update()
    )
    return res.scalar_one_or_none()


async def claim_db_lease(
    session: AsyncSession,
    *,
    job_id: str,
    fencing_token: str,
    worker_id: str,
    ttl_seconds: int,
) -> bool:
    """Take the durable lease for a job. False = held unexpired by another."""
    row = await _lease_row(session, job_id)
    now = _utcnow()
    expires_at = now + timedelta(seconds=max(1, ttl_seconds))
    if row is None:
        session.add(
            WorkerJobLease(
                job_id=job_id,
                fencing_token=fencing_token,
                worker_id=worker_id,
                expires_at=expires_at,
                updated_at=now,
            )
        )
        return True
    if row.fencing_token == fencing_token or row.expires_at <= now:
        row.fencing_token = fencing_token
        row.worker_id = worker_id
        row.expires_at = expires_at
        row.updated_at = now
        return True
    return False


async def assert_db_fence(
    session: AsyncSession,
    *,
    job_id: str,
    fencing_token: str,
) -> None:
    """Fail the caller's transaction unless we still own the job lease.

    The FOR UPDATE lock serializes this check against concurrent claims for
    the remainder of the transaction, so a verified fence cannot be stolen
    between this check and commit.
    """
    row = await _lease_row(session, job_id)
    if (
        row is None
        or row.fencing_token != fencing_token
        or row.expires_at <= _utcnow()
    ):
        raise StaleWorkerFencingError(
            f"Job {job_id} durable lease is not owned by this worker; "
            "aborting transaction"
        )


async def assert_current_db_fence(session: AsyncSession) -> None:
    """Assert the fence installed via ``current_job_fence``, if any."""
    fence = current_job_fence.get()
    if fence is None:
        return
    await assert_db_fence(
        session, job_id=fence.job_id, fencing_token=fence.fencing_token
    )


async def renew_db_lease(
    session: AsyncSession,
    *,
    job_id: str,
    fencing_token: str,
    ttl_seconds: int,
) -> bool:
    """Extend the lease expiry under compare-and-set. False = ownership lost."""
    row = await _lease_row(session, job_id)
    if row is None or row.fencing_token != fencing_token:
        return False
    now = _utcnow()
    row.expires_at = now + timedelta(seconds=max(1, ttl_seconds))
    row.updated_at = now
    return True


async def release_db_lease(
    session: AsyncSession,
    *,
    job_id: str,
    fencing_token: str,
) -> bool:
    """Delete the lease row only when we own it (never evict a new owner)."""
    res = cast(
        CursorResult,
        await session.execute(
            delete(WorkerJobLease).where(
                WorkerJobLease.job_id == job_id,
                WorkerJobLease.fencing_token == fencing_token,
            )
        ),
    )
    return bool(res.rowcount)


async def try_claim_db_lease(
    *,
    job_id: str,
    fencing_token: str,
    ttl_seconds: int,
    worker_id: Optional[str] = None,
) -> bool:
    async with async_session_factory() as session:
        async with session.begin():
            return await claim_db_lease(
                session,
                job_id=job_id,
                fencing_token=fencing_token,
                worker_id=worker_id or f"worker:{fencing_token[:8]}",
                ttl_seconds=ttl_seconds,
            )


async def renew_db_lease_open(
    *, job_id: str, fencing_token: str, ttl_seconds: int
) -> bool:
    async with async_session_factory() as session:
        async with session.begin():
            return await renew_db_lease(
                session,
                job_id=job_id,
                fencing_token=fencing_token,
                ttl_seconds=ttl_seconds,
            )


async def release_db_lease_open(*, job_id: str, fencing_token: str) -> bool:
    async with async_session_factory() as session:
        async with session.begin():
            return await release_db_lease(
                session, job_id=job_id, fencing_token=fencing_token
            )


async def check_db_fence_open(*, job_id: str, fencing_token: str) -> None:
    """Standalone fence verification for result-commit boundaries."""
    async with async_session_factory() as session:
        async with session.begin():
            await assert_db_fence(
                session, job_id=job_id, fencing_token=fencing_token
            )
