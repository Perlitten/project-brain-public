import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.context.context_cache import clear_context_cache
from brain.context.runtime_context_builder import RuntimeContextBuilder
from brain.memory.learning_retrieval import select_learnings_for_context
from brain.retrieval.service import RetrievalCandidate, RetrievalResult


@pytest.mark.asyncio
async def test_memory_changes_apply_on_code_cache_hits(monkeypatch):
    result = RetrievalResult(query="invoice approval", intent="runtime_context",
        repository={"repository_path": "/app", "freshness": {"status": "current", "source_head_commit": "r1"}},
        candidates=[RetrievalCandidate(path="src/invoice.py")])
    retrieval = SimpleNamespace(retrieve=AsyncMock(return_value=result))
    builder = RuntimeContextBuilder(retrieval)
    builder._load_slices = AsyncMock(return_value=[
        {"path": "src/invoice.py", "range": [1, 4], "content": "invoice approval " + "x" * 1300},
    ])
    normative = AsyncMock(side_effect=[{"rules": [{"id": 10, "description": "require approval"}]},
                                       {"rules": []}])
    learnings = AsyncMock(side_effect=[[{"id": 20, "statement": "invoice lesson"}], []])
    monkeypatch.setattr("brain.context.runtime_context_builder.select_relevant_normative_memory", normative)
    monkeypatch.setattr("brain.context.runtime_context_builder.select_learnings_for_context", learnings)
    monkeypatch.setattr("brain.context.runtime_context_builder.settings.MEMORY_SKILLS_IN_ASK", True)
    skills = AsyncMock(side_effect=[[{"id": 30, "name": "invoice review", "workflow": ["approve"]}], []])
    monkeypatch.setattr("brain.memory.skill_store.select_skills_for_context", skills)
    clear_context_cache()
    try:
        first = await builder.build(result.query, "/app", max_tokens=1000)
        second = await builder.build(result.query, "/app", max_tokens=1000)
        assert first["memory"]["learnings"][0]["id"] == 20
        assert first["memory"]["rules"][0]["id"] == 10
        assert first["procedures"]["skills_used"] == [30]
        assert second["_cache_hit"] and second["slices"] == first["slices"]
        assert not second["memory"] and "procedures" not in second
        assert builder._load_slices.await_count == 1
        assert normative.await_count == learnings.await_count == skills.await_count == 2
        assert len(json.dumps(first, ensure_ascii=False, separators=(",", ":")).encode()) <= 4000
    finally:
        clear_context_cache()


@pytest.mark.asyncio
async def test_learning_projection_is_scoped_live_relevant_and_bounded(monkeypatch):
    rows = [SimpleNamespace(id=i, statement="invoice approval lesson " + "z" * 1000,
             confidence=0.8, repo_scope="/app", promoted_from="episode") for i in range(64)]
    # Even a LIKE hit caused by a substring must not pass token relevance.
    rows.append(SimpleNamespace(id=100, statement="invoiceable approvals", confidence=1.0))
    session = AsyncMock()
    response = MagicMock()
    response.scalars.return_value.all.return_value = rows
    session.execute.return_value = response
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    monkeypatch.setattr("brain.memory.learning_retrieval.async_session_factory", factory)
    results = await select_learnings_for_context("invoice approval", "/app", max_bytes=600)
    assert len(results) == 1 and results[0]["id"] == 63
    assert results[0]["kind"] == "learned_guidance"
    assert len(json.dumps(results, separators=(",", ":")).encode()) <= 600
    sql = str(session.execute.await_args.args[0].compile(compile_kwargs={"literal_binds": True}))
    assert "memory_learnings.status = 'active'" in sql
    assert "memory_learnings.valid_until > now()" in sql
    assert "memory_learnings.repo_scope IS NULL" in sql and "'/app'" in sql
    assert "LIMIT 64" in sql
    session.reset_mock()
    assert await select_learnings_for_context("что как для", "/app") == []
    session.execute.assert_not_awaited()
