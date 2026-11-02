"""Startup ownership against real PostgreSQL in a disposable database."""
import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from brain.database import session as session_module
from brain.database import migrations
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
    async def initialize(connection):
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
@pytest.mark.parametrize("terminate_owner", [False, True])
async def test_full_schema_owner_loss_rolls_back_before_next_startup(
    startup_database, monkeypatch, terminate_owner,
):
    entered = asyncio.Event()
    release = asyncio.Event()
    schema_pids = []

    async def migrate(connection, *, acquire_lock):
        assert acquire_lock is False
        schema_pids.append(await connection.scalar(text("SELECT pg_backend_pid()")))
        number = len(schema_pids)
        await connection.execute(text(f"CREATE TABLE startup_owner_{number} (value INTEGER)"))
        if number == 1:
            entered.set()
            await release.wait()
        await connection.execute(text(f"INSERT INTO startup_owner_{number} VALUES (7)"))

    monkeypatch.setattr(migrations, "apply_migrations", migrate)
    first = asyncio.create_task(session_module.init_db())
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with startup_database.connect() as observer:
            owner_pid = await observer.scalar(text(
                "SELECT pid FROM pg_locks WHERE locktype='advisory' AND granted AND objid=:key "
                "AND database=(SELECT oid FROM pg_database WHERE datname=current_database())"
            ), {"key": SCHEMA_MIGRATION_LOCK_ID})
            assert owner_pid is not None
            if terminate_owner:
                assert await observer.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": owner_pid})
            second = asyncio.create_task(session_module._init_db_with_advisory_lock())
            if not terminate_owner:
                async def wait_for_blocked_lock():
                    while not second.done():
                        if await observer.scalar(text(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                            "AND objid=:key AND NOT granted "
                            "AND database=(SELECT oid FROM pg_database WHERE datname=current_database()))"
                        ), {"key": SCHEMA_MIGRATION_LOCK_ID}):
                            return
                        await asyncio.sleep(.01)
                    second.result()
                    pytest.fail("Concurrent startup did not wait for the healthy owner")

                await asyncio.wait_for(wait_for_blocked_lock(), 5)
                assert len(schema_pids) == 1
                release.set()
            await asyncio.wait_for(second, 5)
        release.set()
        if terminate_owner:
            with pytest.raises(DBAPIError):
                await asyncio.wait_for(first, 5)
            assert session_module._db_initialized is False
        else:
            await asyncio.wait_for(first, 5)
            assert session_module._db_initialized is True
        async with startup_database.connect() as observer:
            assert (await observer.scalar(text("SELECT to_regclass('startup_owner_1')")) is None) == terminate_owner
            assert await observer.scalar(text("SELECT value FROM startup_owner_2")) == 7
        assert schema_pids[0] == owner_pid
        if terminate_owner:
            await session_module.init_db()
            assert session_module._db_initialized is True
    finally:
        release.set()
        for task in (first, second):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_task", [False, True])
async def test_full_schema_failure_rolls_back_all_startup_stages(startup_database, monkeypatch, cancel_task):
    from brain.telemetry import query_capture

    entered = asyncio.Event()
    release = asyncio.Event()

    async def migrate(connection, *, acquire_lock):
        assert acquire_lock is False
        await connection.execute(text("CREATE TABLE startup_migration_probe (value INTEGER)"))

    capture = query_capture.initialize_query_capture

    async def fail_after_capture(connection, enabled):
        await capture(connection, enabled)
        entered.set()
        if cancel_task:
            await release.wait()
        raise RuntimeError("capture setup failed")

    monkeypatch.setattr(migrations, "apply_migrations", migrate)
    monkeypatch.setattr(query_capture, "initialize_query_capture", fail_after_capture)
    startup = asyncio.create_task(session_module.init_db())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if cancel_task:
            startup.cancel()
        with pytest.raises(asyncio.CancelledError if cancel_task else RuntimeError):
            await asyncio.wait_for(startup, 5)
        assert session_module._db_initialized is False
        async with startup_database.begin() as observer:
            assert await observer.scalar(text(f"SELECT pg_try_advisory_xact_lock({SCHEMA_MIGRATION_LOCK_ID})"))
            assert await observer.scalar(text("SELECT to_regclass('startup_migration_probe')")) is None
            assert await observer.scalar(text("SELECT to_regclass('brain_query_capture_policy')")) is None
            assert await observer.scalar(text("SELECT to_regnamespace('harness')")) is None
            assert not await observer.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector')"))
        monkeypatch.setattr(query_capture, "initialize_query_capture", capture)
        await session_module.init_db()
        assert session_module._db_initialized is True
        async with startup_database.connect() as observer:
            assert await observer.scalar(text("SELECT count(*) FROM brain_query_capture_policy")) == 1
    finally:
        startup.cancel()
        await asyncio.gather(startup, return_exceptions=True)


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
