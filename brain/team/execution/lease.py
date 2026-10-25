"""The run execution lease: fencing-token compare-and-swap and orphan recovery."""
from __future__ import annotations

import asyncio
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable
from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from brain.execution.container import ContainerBackend
from brain.team._util import _now
from brain.team.authorization import fresh
from brain.team.models import AgentRun, AgentRunExecution
from brain.team.execution._common import SessionFactory, _event
from brain.team.execution.policy import operator_policy


def worker_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"[:128]


@dataclass(frozen=True)
class Lease:
    run_id: uuid.UUID
    owner: str
    token: int


async def acquire_run_lease(factory: SessionFactory, run_id: uuid.UUID, owner: str, *, lease_seconds: int) -> Lease | None:
    """Start the run's single execution. ``None`` when it already started, finished or was cancelled."""
    async with factory() as session, session.begin():
        now = _now()
        token = (await session.execute(update(AgentRunExecution).where(
            AgentRunExecution.run_id == run_id, AgentRunExecution.started_at.is_(None),
            AgentRunExecution.released_at.is_(None))
            .values(lease_owner=owner[:128], fencing_token=AgentRunExecution.fencing_token + 1,
                    lease_expires_at=now + timedelta(seconds=lease_seconds), heartbeat_at=now, started_at=now)
            .returning(AgentRunExecution.fencing_token))).scalar_one_or_none()
    return Lease(run_id, owner[:128], int(token)) if token is not None else None


async def _fence(session: AsyncSession, lease: Lease, lease_seconds: int) -> AgentRunExecution | None:
    """Compare-and-renew: the row only if this owner still holds this token on an unexpired, unreleased lease."""
    now = _now()
    held = (await session.execute(update(AgentRunExecution).where(
        AgentRunExecution.run_id == lease.run_id, AgentRunExecution.lease_owner == lease.owner,
        AgentRunExecution.fencing_token == lease.token, AgentRunExecution.released_at.is_(None),
        AgentRunExecution.lease_expires_at > now)
        .values(heartbeat_at=now, lease_expires_at=now + timedelta(seconds=lease_seconds))
        .returning(AgentRunExecution.run_id))).scalar_one_or_none()
    if held is None:
        return None
    return await fresh(session, AgentRunExecution, AgentRunExecution.run_id == lease.run_id)


async def recover_orphaned_runs(factory: SessionFactory, *,
                                reaper: Callable[[str], None] | None = None) -> list[str]:
    """Fence every started execution whose lease expired unreleased (owner crashed, hung or lost the database).

    The token is bumped and the lease released, so the old owner can commit
    nothing more; the run is not resumed (request a new run instead). Containers
    and networks of fenced container-backend runs are removed (``reaper``,
    default: the configured container backend's ``reap``)."""
    fenced: list[str] = []
    containers: list[str] = []
    async with factory() as session, session.begin():
        now = _now()
        rows = (await session.execute(select(AgentRunExecution).where(
            AgentRunExecution.released_at.is_(None), AgentRunExecution.started_at.is_not(None),
            AgentRunExecution.lease_expires_at <= now))).scalars().all()
        for row in rows:
            # Capture before the UPDATE: the ORM synchronises the loaded row with the new values.
            previous_owner, steps_done, steps = row.lease_owner, row.steps_done, list(row.steps)
            changed = (await session.execute(update(AgentRunExecution).where(
                AgentRunExecution.run_id == row.run_id, AgentRunExecution.fencing_token == row.fencing_token,
                AgentRunExecution.released_at.is_(None))
                .values(fencing_token=row.fencing_token + 1, released_at=now, lease_owner=None)
                .returning(AgentRunExecution.fencing_token))).scalar_one_or_none()
            if changed is None:
                continue
            run = await fresh(session, AgentRun, AgentRun.id == row.run_id)
            if run is None:
                continue
            in_flight = None
            if steps_done < len(steps):
                in_flight = {"step": steps_done, "tool": steps[steps_done]["tool"], "state": "unknown",
                             "note": "The owner stopped renewing its lease; this step may have run partially. Its "
                                     "effects in the working copy are not recorded and network effects cannot be "
                                     "recalled."}
            _event(session, run, "fenced", f"{changed}:fenced", "system:execution-recovery",
                   {"reason": "The lease expired without renewal", "previous_owner": previous_owner,
                    "fencing_token": changed, "steps_recorded": steps_done, "in_flight": in_flight})
            fenced.append(str(row.run_id))
            if row.backend == ContainerBackend.backend_name:
                containers.append(str(row.run_id))
    if fenced:
        logger.warning("Fenced {} orphaned team execution(s): {}", len(fenced), ", ".join(fenced))
    if containers:
        if reaper is None:
            policy = operator_policy()
            reaper = ContainerBackend(policy.container).reap if not policy.error else None
        for run_id in containers:
            if reaper is not None:
                await asyncio.to_thread(reaper, run_id)
    return fenced


# Compat alias: historical name; new code should use acquire_run_lease.
acquire_lease = acquire_run_lease
