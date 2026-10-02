"""Attempt-local hooks; never persist callbacks or credentials in job params."""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass
class JobRuntime:
    job_id: str
    report: Callable[[dict], Awaitable[None]]
    check_lease: Callable[[], Awaitable[None]]
    enqueue_repair: Callable[[dict], Awaitable[str]]


current_job: ContextVar[JobRuntime | None] = ContextVar("current_job", default=None)


async def check_job_lease() -> None:
    runtime = current_job.get()
    if runtime is not None:
        await runtime.check_lease()
