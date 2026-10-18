"""Row-lock helpers shared by team transactions.

The lock order every team transaction follows is documented in
``brain/team/authorization.py`` (module docstring). This module holds what
keeps a transaction inside it bounded: transient-failure retry for the few
multi-row writers (revocation), per-transaction lock and idle timeouts for
the team routes, and a keyed advisory lock that replaces whole-team row locks
where only one name must be serialized (invitations).
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from brain.config.settings import settings

T = TypeVar("T")

DEADLOCK = "40P01"
SERIALIZATION_FAILURE = "40001"
LOCK_NOT_AVAILABLE = "55P03"
TRANSIENT = frozenset({DEADLOCK, SERIALIZATION_FAILURE})
# What a client may simply retry: a deadlock victim, a serialization failure or a bounded lock wait.
RETRYABLE = TRANSIENT | {LOCK_NOT_AVAILABLE}
RETRY_ATTEMPTS = 5
# Two-key advisory locks live in their own space; the first key namespaces them ("team" in ASCII).
ADVISORY_NAMESPACE = 0x7465616D


def sqlstate(exc: BaseException) -> str | None:
    """The PostgreSQL SQLSTATE behind a SQLAlchemy error (asyncpg or psycopg), or ``None``."""
    orig = getattr(exc, "orig", exc)
    for candidate in (orig, getattr(orig, "__cause__", None)):
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if isinstance(code, str):
            return code
    return None


def retryable(exc: BaseException) -> bool:
    return isinstance(exc, DBAPIError) and sqlstate(exc) in RETRYABLE


def _postgres(session: AsyncSession) -> bool:
    get_bind = getattr(session, "get_bind", None)  # unit-test session doubles have no bind
    return get_bind is not None and get_bind().dialect.name == "postgresql"


async def retry_transient(session: AsyncSession, operation: Callable[[], Awaitable[T]],
                          attempts: int = RETRY_ATTEMPTS) -> T:
    """Run ``operation`` in a savepoint, retrying it when PostgreSQL picks it as a deadlock victim.

    Only the savepoint is rolled back, so locks the caller took before (its
    own authority) stay held and the retry continues the same transaction.
    After ``attempts`` the last error propagates; nothing of the operation is
    committed. Callers capture plain ids before calling: a rolled-back
    savepoint expires the ORM objects it touched."""
    for attempt in range(1, attempts + 1):
        try:
            async with session.begin_nested():
                return await operation()
        except DBAPIError as exc:
            if sqlstate(exc) not in TRANSIENT or attempt == attempts:
                raise
            await asyncio.sleep(random.uniform(0.01, 0.05) * attempt)
    raise AssertionError("unreachable")


async def bound_team_transaction(session: AsyncSession) -> None:
    """Bound lock waits and idle time for the rest of this transaction (``SET LOCAL``; PostgreSQL only).

    A lock wait longer than ``BRAIN_TEAM_LOCK_TIMEOUT_MS`` fails with SQLSTATE
    55P03 (HTTP 503 with Retry-After at the team routes) instead of queueing a
    convoy; a transaction idle longer than ``BRAIN_TEAM_IDLE_TX_TIMEOUT_MS``
    is ended by the server, releasing its locks and its pool connection."""
    if not _postgres(session):
        return
    for name, value in (("lock_timeout", settings.BRAIN_TEAM_LOCK_TIMEOUT_MS),
                        ("idle_in_transaction_session_timeout", settings.BRAIN_TEAM_IDLE_TX_TIMEOUT_MS)):
        # set_config(..., is_local => true) is SET LOCAL with a bound parameter.
        await session.execute(select(func.set_config(name, f"{int(value)}ms", True)))


async def advisory_xact_lock(session: AsyncSession, key: str) -> None:
    """Serialize transactions on ``key`` until commit (PostgreSQL); SQLite already serializes writers."""
    if _postgres(session):
        await session.execute(select(func.pg_advisory_xact_lock(ADVISORY_NAMESPACE, func.hashtext(key))))


__all__ = ["ADVISORY_NAMESPACE", "DEADLOCK", "LOCK_NOT_AVAILABLE", "RETRYABLE", "RETRY_ATTEMPTS",
           "SERIALIZATION_FAILURE", "TRANSIENT", "advisory_xact_lock", "bound_team_transaction", "retry_transient",
           "retryable", "sqlstate"]
