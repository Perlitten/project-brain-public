"""Blank chunks (legacy rows from empty files) must not count as missing embeddings.

Embedding providers reject empty input with HTTP 400, so such a chunk can never
be embedded; counting it kept every nightly verification of a repository with an
empty ``__init__.py`` permanently "degraded".
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from brain.embeddings import backfill, integrity


def _session_factory(rows):
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)))

    @asynccontextmanager
    async def factory():
        yield session

    return factory


def test_inventory_ignores_blank_chunks():
    rows = [
        ("src/pkg/__init__.py", "", None, None, None, None, None, None, False),
        ("src/pkg/empty.py", "  \n\t", None, None, None, None, None, None, False),
        ("src/pkg/real.py", "def f():\n    return 1\n", None, None, None, None, None, None, False),
    ]
    with (
        patch.object(integrity, "async_session_factory", _session_factory(rows)),
        patch.object(integrity, "_pgvector_health", AsyncMock(return_value=(True, True))),
    ):
        inventory = asyncio.run(integrity.collect_embedding_inventory(repository_id=1))
    assert inventory.total_eligible_chunks == 1
    assert inventory.missing_embeddings == 1


def test_fast_inventory_ignores_zero_length_chunks():
    rows = [
        ("src/pkg/__init__.py", 0, None, None, None, None, None, None, False),
        ("src/pkg/real.py", 24, None, None, None, None, None, None, False),
    ]
    with (
        patch.object(integrity, "async_session_factory", _session_factory(rows)),
        patch.object(integrity, "_pgvector_health", AsyncMock(return_value=(True, True))),
    ):
        inventory = asyncio.run(integrity.collect_embedding_inventory(repository_id=1, fast=True))
    assert inventory.total_eligible_chunks == 1


def test_backfill_does_not_send_blank_chunks_to_the_provider():
    rows = [
        (1, "", "src/pkg/__init__.py", None, None, None, None, None, None, False),
        (2, "def f():\n    return 1\n", "src/pkg/real.py", None, None, None, None, None, None, False),
    ]
    with patch.object(backfill, "async_session_factory", _session_factory(rows)):
        pending = asyncio.run(backfill._chunks_needing_regeneration(1, integrity.get_embedding_config()))
    assert [item.chunk_id for item in pending] == [2]
