"""One persisted capture preference shared by API, workers and stdio MCP.

Environment configuration seeds a new installation once. A missing/unreadable
policy fails closed; stale process settings never re-enable query recording.
"""
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from brain.database.models import BrainQueryCapturePolicy
from brain.database.session import async_session_factory


async def initialize_query_capture(conn: AsyncConnection, enabled: bool) -> None:
    await conn.execute(text("""
        INSERT INTO brain_query_capture_policy (id, enabled) VALUES (1, :enabled)
        ON CONFLICT (id) DO NOTHING
    """), {"enabled": enabled})


async def query_capture_enabled(session: AsyncSession, *, lock: bool = False) -> bool:
    statement = select(BrainQueryCapturePolicy.enabled).where(BrainQueryCapturePolicy.id == 1)
    if lock:
        # A successful disable waits for earlier recording transactions to finish.
        # New recordings cannot reuse their previous capture decision. SQLite
        # serializes writes with the engine's BEGIN IMMEDIATE transaction policy.
        statement = statement.with_for_update(read=True)
    return (await session.execute(statement)).scalar_one_or_none() is True


async def current_query_capture() -> bool:
    try:
        async with async_session_factory() as session:
            return await query_capture_enabled(session)
    except Exception:
        return False


async def set_query_capture(enabled: bool) -> None:
    async with async_session_factory() as session, session.begin():
        await session.execute(text("""
            INSERT INTO brain_query_capture_policy (id, enabled) VALUES (1, :enabled)
            ON CONFLICT (id) DO UPDATE SET enabled = excluded.enabled
        """), {"enabled": enabled})
