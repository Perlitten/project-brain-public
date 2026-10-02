"""Redis-backed cache for query embedding vectors."""

import hashlib
import json
from typing import List, Optional

from loguru import logger

from brain.database.session import redis_client

_EMBEDDING_CACHE_TTL_SECONDS = 3600
_CACHE_PREFIX = "brain:embed:"


def _cache_key(text: str, input_type: str) -> str:
    digest = hashlib.sha256(f"{input_type}:{text}".encode("utf-8")).hexdigest()
    return f"{_CACHE_PREFIX}{digest}"


async def get_cached_embedding(text: str, input_type: str = "query") -> Optional[List[float]]:
    """Load a cached embedding vector from Redis, if present."""
    try:
        raw = await redis_client.get(_cache_key(text, input_type))
        if raw:
            return json.loads(raw)
    except Exception as exc:
        logger.debug(f"Embedding cache read failed: {exc}")
    return None


async def set_cached_embedding(
    text: str,
    vector: List[float],
    input_type: str = "query",
    ttl_seconds: int = _EMBEDDING_CACHE_TTL_SECONDS,
) -> None:
    """Store an embedding vector in Redis."""
    if not vector:
        return
    try:
        await redis_client.setex(
            _cache_key(text, input_type),
            ttl_seconds,
            json.dumps(vector),
        )
    except Exception as exc:
        logger.debug(f"Embedding cache write failed: {exc}")
