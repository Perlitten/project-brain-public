"""Startup ownership against real PostgreSQL in a disposable database."""
import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from brain.database import session as session_module
from brain.database.migrations import SCHEMA_MIGRATION_LOCK_ID


@pytest_asyncio.fixture
async def startup_database(monkeypatch):
    url = session_module.async_engine.url
    if url.get_backend_name() != "postgresql":
        pytest.skip("requires PostgreSQL")
    admin = create_async_engine(url.set(database="postgres"), poolclass=NullPool,
                                isolation_level="AUTOCOMMIT")
    name = "brain_startup_" + uuid.uuid4().hex
    engine = create_async_engine(url.set(database=name))
    created = False
    try:
        try:
            async with admin.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except (OSError, ConnectionError):
            pytest.skip("PostgreSQL unavailable")
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
            created = True
        monkeypatch.setattr(session_module, "async_engine", engine)
        monkeypatch.setattr(session_module, "_lite", False)
        monkeypatch.setattr(session_module, "_db_initialized", False)
        monkeypatch.setattr(session_module, "_graph_schema_ready", False)
        monkeypatch.setattr(session_module, "_init_db_lock", asyncio.Lock())
        monkeypatch.setattr(session_module, "_initialize_graph_schema", AsyncMock(return_value=True))
        yield engine
    finally:
        await engine.dispose()
        if created:
            async with admin.connect() as connection:
                await connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await admin.dispose()


@pytest.mark.asyncio
async def test_startup_waits_for_concurrent_migration(startup_database, monkeypatch):
    initializer = AsyncMock()
    monkeypatch.setattr(session_module, "_init_db_unlocked", initializer)
    startup = None
    try:
        async with startup_database.begin() as owner:
            await owner.execute(text(f"SELECT pg_advisory_xact_lock({SCHEMA_MIGRATION_LOCK_ID})"))
            startup = asyncio.create_task(session_module.init_db())

            async def wait_for_blocked_lock():
                async with startup_database.connect() as observer:
                    while True:
                        if startup.done():
                            startup.result()
                            pytest.fail("Startup returned before the concurrent migration released ownership")
                        blocked = await observer.scalar(text(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                            "AND objid=:key AND NOT granted)"), {"key": SCHEMA_MIGRATION_LOCK_ID})
                        if blocked:
                            return
                        await asyncio.sleep(.01)

            await asyncio.wait_for(wait_for_blocked_lock(), timeout=5)
            assert session_module._db_initialized is False
            initializer.assert_not_awaited()
        await asyncio.wait_for(startup, timeout=5)
        initializer.assert_awaited_once()
        assert session_module._db_initialized is True
    finally:
        if startup:
            startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("migration failed"), asyncio.CancelledError()])
async def test_failed_or_cancelled_startup_releases_lock(startup_database, monkeypatch, failure):
    async def initialize():
        async with startup_database.begin() as connection:
            await connection.execute(text("CREATE TABLE startup_probe (value INTEGER)"))
            await connection.execute(text("INSERT INTO startup_probe VALUES (7)"))
            if initializer.await_count == 1:
                raise failure

    initializer = AsyncMock(side_effect=initialize)
    monkeypatch.setattr(session_module, "_init_db_unlocked", initializer)
    # A different connection must be able to own the lock, including after
    # cancellation (session-level locks survive a pooled connection rollback).
    async with startup_database.begin() as connection:
        with pytest.raises(type(failure)):
            await session_module.init_db()
        assert session_module._db_initialized is False
        initializer.assert_awaited_once()
        assert await connection.scalar(text(f"SELECT pg_try_advisory_xact_lock({SCHEMA_MIGRATION_LOCK_ID})"))
        assert await connection.scalar(text("SELECT to_regclass('startup_probe')")) is None
    await session_module.init_db()
    assert initializer.await_count == 2
    assert session_module._db_initialized is True
    async with startup_database.connect() as connection:
        assert await connection.scalar(text("SELECT value FROM startup_probe")) == 7


@pytest.mark.asyncio
async def test_full_schema_startup_is_restartable(startup_database, monkeypatch):
    await session_module.init_db()
    async with startup_database.begin() as connection:
        applied = await connection.scalar(text("SELECT count(*) FROM brain_schema_migrations"))
        assert applied > 0
        await connection.execute(text("CREATE TABLE startup_probe (value INTEGER)"))
        await connection.execute(text("INSERT INTO startup_probe VALUES (7)"))
    monkeypatch.setattr(session_module, "_db_initialized", False)
    await session_module.init_db()
    async with startup_database.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM brain_schema_migrations")) == applied
        assert await connection.scalar(text("SELECT value FROM startup_probe")) == 7
