from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from brain.context.runtime_context_builder import RuntimeContextBuilder
from brain.retrieval.service import RetrievalCandidate, RetrievalResult


@pytest.mark.asyncio
async def test_contract_identifiers_keep_implementation_body_ahead_of_test_assertions(monkeypatch):
    paths = [f"tests/example_{i}.py" for i in range(11)] + ["src/runtime.py"]
    result = RetrievalResult(
        query="Explain stale_blocked and persist=false",
        intent="runtime_context", repository={"repository_id": 42},
        candidates=[RetrievalCandidate(path=p, ranges=[(1, 100)]) for p in paths],
    )
    rows = []
    for p in paths:
        rows.append((SimpleNamespace(start_line=1, end_line=1, chunk_index=0,
                                     content="class RuntimeContext: pass"), p))
        rows.append((SimpleNamespace(start_line=10, end_line=12, chunk_index=1,
                                     content='if not persist:\n    return {"status": "stale_blocked"}'), p))

    class Session:
        async def execute(self, statement):
            return SimpleNamespace(all=lambda: rows)

    class Scope:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_):
            return False

    monkeypatch.setattr("brain.context.runtime_context_builder.async_session_factory", Scope)
    slices = await RuntimeContextBuilder()._load_slices(result, 18_000)
    assert slices[0]["path"] == "src/runtime.py"
    assert slices[0]["range"] == [10, 12]
    assert "stale_blocked" in slices[0]["content"]
    assert result.candidates[0].path == "src/runtime.py"
    assert len(slices) <= 22
    assert len(result.candidates) == 12


@pytest.mark.asyncio
async def test_code_cache_keeps_public_locators_aligned_with_implementation(monkeypatch):
    from brain.context.context_cache import clear_context_cache
    from brain.config.settings import settings

    test = RetrievalCandidate(path="tests/contracts.py")
    implementation = RetrievalCandidate(path="src/runtime.py")
    result = RetrievalResult(
        query="Explain stale_blocked and persist=false", intent="runtime_context",
        repository={"repository_path": "/indexed/identifier-cache-test",
                    "freshness": {"status": "current", "source_head_commit": "r1"}},
        candidates=[test, implementation],
    )
    retrieval = SimpleNamespace(retrieve=AsyncMock(return_value=result))
    builder = RuntimeContextBuilder(retrieval)
    builder._load_slices = AsyncMock(return_value=[
        {"path": "src/runtime.py", "range": [10, 12],
         "content": 'if not persist:\n    return {"status": "stale_blocked", "context": False}'},
    ])
    monkeypatch.setattr("brain.context.runtime_context_builder.select_relevant_normative_memory", AsyncMock(return_value={}))
    monkeypatch.setattr("brain.context.runtime_context_builder.select_learnings_for_context", AsyncMock(return_value=[]))
    monkeypatch.setattr(settings, "MEMORY_SKILLS_IN_ASK", False)
    clear_context_cache()
    try:
        first = await builder.build(result.query, "/indexed/identifier-cache-test")
        result.candidates = [test, implementation]
        second = await builder.build(result.query, "/indexed/identifier-cache-test")
        assert first["status"] == second["status"] == "ok"
        assert second["_cache_hit"]
        assert first["candidates"][0]["path"] == second["candidates"][0]["path"] == "src/runtime.py"
        assert builder._load_slices.await_count == 1
    finally:
        clear_context_cache()
