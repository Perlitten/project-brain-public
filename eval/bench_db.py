"""Shared isolated-database harness for eval benchmarks.

Benchmarks that plant learnings/episodes must not write into the
production ``memory_*`` tables that ``/ask`` reads. This module creates a
scratch Postgres database, applies the real migrations, rebinds
``brain.database.session`` to it for the duration of the context, then
restores the bindings and drops the database — so a crashed run cannot
leave benchmark rows behind either.

Usage::

    async with isolated_bench_db() as bench:
        ...  # LearningStore, consolidation, etc. all hit the scratch DB
"""

from __future__ import annotations

import uuid
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def _swap_db(url: str, db_name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{db_name}", parts.query, parts.fragment))


class isolated_bench_db:
    """Async context manager: a migrated scratch DB bound for the block."""

    def __init__(self) -> None:
        self.db_name: str | None = None
        self._admin_engine: AsyncEngine | None = None
        self._bench_engine: AsyncEngine | None = None
        self._orig_engine: AsyncEngine | None = None

    async def __aenter__(self) -> "isolated_bench_db":
        import brain.database.session as session_mod
        from brain.config.settings import settings
        from brain.database.migrations import apply_migrations

        base_url = settings.DATABASE_URL
        if not base_url:
            raise RuntimeError("isolated_bench_db: settings.DATABASE_URL is not configured")
        self.db_name = f"{settings.POSTGRES_DB}_bench_{uuid.uuid4().hex[:8]}"

        # Admin connection to the configured database itself (any database can
        # host CREATE/DROP DATABASE outside a transaction).
        self._admin_engine = create_async_engine(base_url, isolation_level="AUTOCOMMIT")
        try:
            async with self._admin_engine.connect() as conn:
                await conn.execute(text(f'CREATE DATABASE "{self.db_name}"'))

            # Mirror the production startup schema path (_init_db_unlocked):
            # pgvector extension -> harness schema -> create_all -> migrations.
            from brain.database import Base
            from brain.database import harness_models  # noqa: F401 - registers models on Base

            self._bench_engine = create_async_engine(_swap_db(base_url, self.db_name))
            async with self._bench_engine.begin() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            async with self._bench_engine.begin() as conn:
                await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {harness_models.HARNESS_SCHEMA}"))
            async with self._bench_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with self._bench_engine.begin() as conn:
                await apply_migrations(conn, acquire_lock=False)
        except BaseException:
            # A failed setup must not orphan the half-migrated scratch DB.
            if self._bench_engine is not None:
                await self._bench_engine.dispose()
            try:
                async with self._admin_engine.connect() as conn:
                    await conn.execute(
                        text(f'DROP DATABASE IF EXISTS "{self.db_name}" WITH (FORCE)')
                    )
            finally:
                await self._admin_engine.dispose()
            raise

        # Rebind everything that resolves the engine lazily. LearningStore and
        # friends call async_session_factory() at use time; run_consolidation
        # reads session_mod.async_engine inside the function, so patching the
        # module attribute is picked up.
        self._orig_engine = session_mod.async_engine
        session_mod.async_session_factory.configure(bind=self._bench_engine)
        session_mod.async_engine = self._bench_engine
        return self

    async def __aexit__(self, *exc_info) -> None:
        import brain.database.session as session_mod

        session_mod.async_session_factory.configure(bind=self._orig_engine)
        session_mod.async_engine = self._orig_engine
        if self._bench_engine is not None:
            await self._bench_engine.dispose()
        if self._admin_engine is not None:
            try:
                async with self._admin_engine.connect() as conn:
                    await conn.execute(
                        text(f'DROP DATABASE IF EXISTS "{self.db_name}" WITH (FORCE)')
                    )
            finally:
                await self._admin_engine.dispose()
