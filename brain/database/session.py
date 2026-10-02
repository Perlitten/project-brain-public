import asyncio
import time
from typing import AsyncGenerator, Dict, Any
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import QueuePool
from redis.asyncio import Redis, from_url as redis_from_url
from neo4j import AsyncGraphDatabase, AsyncDriver
from loguru import logger

from brain.config.settings import settings

# --- SQLAlchemy / PostgreSQL Setup ---
assert settings.DATABASE_URL is not None, "DATABASE_URL is not configured"
async_engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    future=True,
    pool_pre_ping=True,
    pool_size=settings.POSTGRES_POOL_SIZE,
    max_overflow=settings.POSTGRES_MAX_OVERFLOW,
    pool_timeout=settings.POSTGRES_POOL_TIMEOUT_SECONDS,
)

async_session_factory = async_sessionmaker(
    bind=async_engine, class_=AsyncSession, expire_on_commit=False, autocommit=False, autoflush=False
)

_init_db_lock = asyncio.Lock()
_db_initialized = False
_graph_schema_ready = False


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency helper to get an async SQLAlchemy session."""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# --- Redis Setup ---
assert settings.REDIS_URL is not None, "REDIS_URL is not configured"
redis_client: Redis = redis_from_url(
    settings.REDIS_URL,
    encoding="utf-8",
    decode_responses=True,
    # BRPOP in the worker blocks; default socket_timeout kills idle connections.
    socket_timeout=None,
    socket_connect_timeout=5,
    retry_on_timeout=True,
    health_check_interval=30,
)

# --- Neo4j Setup ---
neo4j_driver: AsyncDriver = AsyncGraphDatabase.driver(
    settings.NEO4J_URI, auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD)
)


# --- Health Check and Connection Cleanup ---
async def check_health() -> Dict[str, Dict[str, Any]]:
    """Checks the health of PostgreSQL, Redis, and Neo4j databases.

    Returns:
        A dictionary containing the status ("healthy" or "unhealthy") and details
        or error messages for each service.
    """
    health_status: Dict[str, Dict[str, Any]] = {}

    # 1. Test PostgreSQL connection
    try:
        async with async_engine.connect() as conn:
            from sqlalchemy import text

            await conn.execute(text("SELECT 1"))
        pool = async_engine.sync_engine.pool
        pool_info: Dict[str, Any]
        if isinstance(pool, QueuePool):
            pool_info = {
                "size": pool.size(),
                "checked_out": pool.checkedout(),
                "overflow": pool.overflow(),
                "max_overflow": settings.POSTGRES_MAX_OVERFLOW,
            }
        else:
            pool_info = {"type": type(pool).__name__}
        health_status["postgres"] = {
            "status": "healthy",
            "message": "SQLAlchemy async engine connection successful",
            "pool": pool_info,
        }
    except Exception as e:
        logger.error(f"PostgreSQL health check failed: {e}")
        health_status["postgres"] = {"status": "unhealthy", "error": str(e)}

    # 2. Test Redis connection
    try:
        start = time.perf_counter()
        pong = await redis_client.ping()
        ping_ms = round((time.perf_counter() - start) * 1000, 1)
        if pong:
            health_status["redis"] = {
                "status": "healthy",
                "message": "Redis ping successful",
                "ping_ms": ping_ms,
            }
        else:
            health_status["redis"] = {"status": "unhealthy", "error": "Redis ping returned unexpected result"}
    except Exception as e:
        logger.error(f"Redis health check failed: {e}")
        health_status["redis"] = {"status": "unhealthy", "error": str(e)}

    # 3. Test Neo4j connection
    try:
        await neo4j_driver.verify_connectivity()
        health_status["neo4j"] = {"status": "healthy", "message": "Neo4j connection verified"}
    except Exception as e:
        logger.error(f"Neo4j health check failed: {e}")
        health_status["neo4j"] = {"status": "unhealthy", "error": str(e)}

    return health_status


async def close_database_connections() -> None:
    """Closes all database engine, client, and driver connections."""
    logger.info("Closing all database connections...")
    try:
        await async_engine.dispose()
    except Exception as e:
        logger.error(f"Error disposing SQLAlchemy engine: {e}")

    try:
        await redis_client.close()
    except Exception as e:
        logger.error(f"Error closing Redis client: {e}")

    try:
        await neo4j_driver.close()
    except Exception as e:
        logger.error(f"Error closing Neo4j driver: {e}")

    logger.info("Database connections closed.")


async def _init_db_unlocked() -> None:
    """Initializes the database schema by creating all tables if they do not exist."""
    from sqlalchemy import text
    from brain.database import Base
    from brain.database.migrations import apply_migrations

    # The `embeddings` table declares a pgvector column, so the `vector` extension
    # must exist BEFORE create_all — otherwise a fresh database fails with
    # "type vector does not exist". Idempotent; safe on every boot.
    async with async_engine.begin() as conn:
        try:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        except Exception as exc:
            # Fail fast with a clear message instead of letting create_all crash
            # later with an opaque "type vector does not exist" error.
            raise RuntimeError(
                "pgvector extension is required but could not be created "
                f"(is the pgvector image/extension installed?): {exc}"
            ) from exc

    # The harness task ledger (brain/database/harness_models.py) lives in its
    # own schema so it never collides with Brain's own tables — the schema
    # must exist before create_all, since Postgres won't create it implicitly.
    async with async_engine.begin() as conn:
        from brain.database.harness_models import HARNESS_SCHEMA

        await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {HARNESS_SCHEMA}"))

    async with async_engine.begin() as conn:
        from brain.database import harness_models  # noqa: F401 - registers models on Base

        await conn.run_sync(Base.metadata.create_all)

    async with async_engine.begin() as conn:
        await apply_migrations(conn, acquire_lock=False)


async def _initialize_graph_schema() -> bool:
    """Install graph indexes, returning false while Neo4j is still starting."""
    from brain.graph.graph_client import ensure_graph_schema

    try:
        await ensure_graph_schema()
    except Exception as exc:
        # PostgreSQL/Redis-backed diagnostics must still start when Neo4j is the
        # incident being diagnosed. Graph writes will retry schema setup later.
        logger.warning(f"Neo4j graph schema initialization deferred: {type(exc).__name__}: {exc}")
        return False
    return True


async def _init_db_with_advisory_lock() -> None:
    """Serialize the complete API/worker startup schema path across processes."""
    from sqlalchemy import text
    from brain.database.migrations import SCHEMA_MIGRATION_LOCK_ID

    async with async_engine.connect() as lock_conn:
        try:
            res = await lock_conn.execute(text(f"SELECT pg_try_advisory_lock({SCHEMA_MIGRATION_LOCK_ID})"))
            acquired = res.scalar()
            if acquired:
                try:
                    await _init_db_unlocked()
                finally:
                    await lock_conn.execute(text(f"SELECT pg_advisory_unlock({SCHEMA_MIGRATION_LOCK_ID})"))
                    await lock_conn.commit()
            else:
                logger.info("Advisory lock held by concurrent worker; skipping duplicate schema migration.")
        except Exception as exc:
            logger.warning(f"Advisory lock acquiring failed: {exc}; proceeding with unlocked initialization.")
            await _init_db_unlocked()



async def init_db() -> None:
    """Initialize schema once per process, retrying if the first attempt fails.

    API and worker startup each call this before serving work. Job handlers also
    call it defensively, so the process-local guard prevents every job from
    reacquiring PostgreSQL DDL locks while the advisory lock still protects the
    first initialization across processes.
    """
    global _db_initialized, _graph_schema_ready

    if _db_initialized and _graph_schema_ready:
        return
    async with _init_db_lock:
        if not _db_initialized:
            await _init_db_with_advisory_lock()
            _db_initialized = True
        if not _graph_schema_ready:
            _graph_schema_ready = await _initialize_graph_schema()
