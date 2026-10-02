from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis
from neo4j import AsyncDriver

from brain.database.session import get_async_session, redis_client, neo4j_driver

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency to get a PostgreSQL async database session."""
    async for session in get_async_session():
        yield session

async def get_redis() -> Redis:
    """FastAPI dependency to get the Redis client."""
    return redis_client

async def get_neo4j() -> AsyncDriver:
    """FastAPI dependency to get the Neo4j driver."""
    return neo4j_driver
