"""Deterministic context pack cache for equivalent repeated engineering queries."""
from __future__ import annotations

import hashlib
import time
from typing import Any, Optional
from loguru import logger

_CONTEXT_CACHE: dict[str, tuple[float, str, dict[str, Any]]] = {}
_MAX_CACHE_ENTRIES = 500
_CACHE_TTL_SECONDS = 3600  # 1 hour


def _make_key(repo_scope: str, task_description: str, source_revision: Optional[str] = None) -> str:
    norm_repo = (repo_scope or "").strip().lower()
    norm_task = " ".join((task_description or "").strip().lower().split())
    norm_rev = (source_revision or "").strip().lower()
    raw = f"{norm_repo}:{norm_rev}:{norm_task}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_cached_context(repo_scope: str, task_description: str, source_revision: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Retrieve cached context payload if unexpired and revision matches."""
    key = _make_key(repo_scope, task_description, source_revision)
    entry = _CONTEXT_CACHE.get(key)
    if not entry:
        return None
    created_at, rev, payload = entry
    if time.time() - created_at > _CACHE_TTL_SECONDS:
        _CONTEXT_CACHE.pop(key, None)
        return None
    if source_revision and rev != source_revision:
        _CONTEXT_CACHE.pop(key, None)
        return None
    logger.info(f"ContextCache: Hit for key {key[:12]} (rev={source_revision})")
    return payload


def put_cached_context(repo_scope: str, task_description: str, payload: dict[str, Any], source_revision: Optional[str] = None) -> None:
    """Store context payload in LRU cache."""
    key = _make_key(repo_scope, task_description, source_revision)
    if len(_CONTEXT_CACHE) >= _MAX_CACHE_ENTRIES:
        # Evict oldest entry
        oldest_key = min(_CONTEXT_CACHE.keys(), key=lambda k: _CONTEXT_CACHE[k][0])
        _CONTEXT_CACHE.pop(oldest_key, None)
    _CONTEXT_CACHE[key] = (time.time(), source_revision or "", payload)
    logger.debug(f"ContextCache: Stored key {key[:12]} (rev={source_revision})")


def clear_context_cache() -> None:
    _CONTEXT_CACHE.clear()
