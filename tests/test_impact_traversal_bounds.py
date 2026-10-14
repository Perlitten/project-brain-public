"""The impact traversal must stay bounded and register compact rows by distance."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import brain.analyzers.impact_analyzer as ia


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def __aiter__(self):
        async def gen():
            for row in self._rows:
                yield row
        return gen()


@pytest.mark.asyncio
async def test_traversal_is_bounded_and_registers_by_distance(tmp_path):
    row = {
        "n": {"id": "a", "name": "apps/api/routers/web_login.py", "path": "apps/api/routers/web_login.py", "labels": ["File"]},
        "d1": [{"id": "b", "name": "tests/test_web_login.py", "path": "tests/test_web_login.py", "labels": ["File"]}],
        "d2": [[{"id": "c", "name": "apps/api/main.py", "path": "apps/api/main.py", "labels": ["File"]}]],
    }
    session = MagicMock()
    session.run = AsyncMock(return_value=_Result([row]))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    graph = SimpleNamespace(driver=SimpleNamespace(session=lambda: cm))
    with (
        patch.object(ia, "require_repository_by_path", AsyncMock(return_value=SimpleNamespace(id=7))),
        patch.object(ia, "GraphClient", return_value=graph),
    ):
        result = await ia.ImpactAnalyzer(tmp_path).analyze_impact("rate limit web login", fast=True)

    cypher = session.run.call_args.args[0]
    kwargs = session.run.call_args.kwargs
    assert "LIMIT $start_limit" in cypher and "[..$fanout]" in cypher
    assert "RETURN n, path" not in cypher
    assert kwargs["start_limit"] == ia.IMPACT_START_NODES_PER_KEYWORD
    assert kwargs["fanout"] == ia.IMPACT_FANOUT_PER_HOP
    assert "apps/api/routers/web_login.py" in result["directly_affected"]
    assert "tests/test_web_login.py" in result["directly_affected"]
    assert "apps/api/main.py" in result["indirectly_affected"]


def test_noise_filters_drop_dunders_and_function_words():
    assert ia._is_noise_node("__init__")
    assert ia._is_noise_node("all")
    assert not ia._is_noise_node("run_scheduler")
    assert not ia._is_noise_node("brain/workers/scheduler.py")
    keywords = ia._deterministic_keywords("Change how context packs are built for agents")
    assert "how" not in [k.lower() for k in keywords]
    assert "are" not in [k.lower() for k in keywords]
    assert "context" in [k.lower() for k in keywords]
