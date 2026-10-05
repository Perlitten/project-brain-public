from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from brain.analyzers.impact_analyzer import ImpactAnalyzer


class _Node(dict):
    labels = {"File"}
    element_id = "node-1"


class _Result:
    def __init__(self, records):
        self.records = records

    def __aiter__(self):
        self._items = iter(self.records)
        return self

    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _Session:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def run(self, query, **params):
        self.calls.append((query, params))
        return _Result([{"n": _Node(path="apps/api/routers/core.py"), "m": None, "distance": 0}])


@pytest.mark.asyncio
async def test_bounded_impact_uses_one_graph_query_and_no_llm():
    session = _Session()
    graph = SimpleNamespace(driver=SimpleNamespace(session=lambda: session))
    with (
        patch("brain.analyzers.impact_analyzer.get_model_router"),
        patch(
            "brain.analyzers.impact_analyzer.require_repository_by_path",
            new=AsyncMock(return_value=SimpleNamespace(id=7)),
        ),
        patch("brain.analyzers.impact_analyzer.GraphClient", return_value=graph),
    ):
        result = await ImpactAnalyzer("/app").analyze_bounded("change core endpoint security")

    assert len(session.calls) == 1
    query, params = session.calls[0]
    assert "UNWIND $keywords" in query
    assert params["repository_id"] == 7
    assert result["status"] == "ok"
    assert result["affected"][0]["name"] == "apps/api/routers/core.py"


from brain.analyzers.impact_analyzer import _is_noise_node


def test_noise_filter_drops_sql_and_prose_debris():
    for junk in ["WHERE", "VARCHAR", "btrim", "COALESCE", "AND", "The", "in", "OK", "width", "Unknown", "", None]:
        assert _is_noise_node(junk), junk


def test_noise_filter_keeps_real_code():
    for real in [
        "resolve_embedding_dimension",
        "brain/embeddings/config.py",
        "brain.llm.presets",
        "/health",
        "ValueError",
        "get_db",
    ]:
        assert not _is_noise_node(real), real
