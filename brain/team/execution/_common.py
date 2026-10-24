"""Shared exceptions, event helpers and row lookup for team execution."""
from __future__ import annotations

import uuid
from typing import Any, Callable
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from brain.database.harness_models import AgentTaskEvent
from brain.team.authorization import TeamDenied, fresh
from brain.team.models import AgentRun, AgentRunExecution


EVENT_PREFIX = "run.execution."


TERMINAL = ("completed", "failed", "revoked", "cancelled", "fenced", "budget_exhausted")


SessionFactory = Callable[[], Any]


class ExecutionNotFound(LookupError):
    pass


class ExecutionDenied(TeamDenied):
    """An execution gate refused; the message names the gate (shown to callers already holding task access)."""


def _event(session: AsyncSession, run: AgentRun, kind: str, key: str, actor: str, payload: dict) -> None:
    session.add(AgentTaskEvent(task_id=run.task_id, event_type=f"{EVENT_PREFIX}{kind}", actor=actor[:100],
                               idempotency_key=f"exec:{run.id}:{key}"[:255],
                               payload_json={"run_id": str(run.id), **payload}))


async def _events(session: AsyncSession, run: AgentRun) -> list[AgentTaskEvent]:
    return list((await session.execute(select(AgentTaskEvent).where(
        AgentTaskEvent.task_id == run.task_id, AgentTaskEvent.idempotency_key.like(f"exec:{run.id}:%"))
        .order_by(AgentTaskEvent.id))).scalars().all())


async def _run_and_row(session: AsyncSession, task_id: uuid.UUID,
                       run_id: uuid.UUID) -> tuple[AgentRun, AgentRunExecution]:
    run = await fresh(session, AgentRun, AgentRun.id == run_id, AgentRun.task_id == task_id)
    row = await fresh(session, AgentRunExecution, AgentRunExecution.run_id == run_id) if run else None
    if run is None or row is None:
        raise ExecutionNotFound("Execution not found")
    return run, row
