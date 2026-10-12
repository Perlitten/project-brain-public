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
    # Lexical-only degradation path (no embedder): one SQL query, no provider call.
    monkeypatch.setattr("brain.memory.learning_retrieval.settings.MEMORY_RETRIEVAL_SEMANTIC", False)
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
    assert await select_learnings_for_context("the and for", "/app") == []
    session.execute.assert_not_awaited()


def _hybrid_session(semantic_rows, lexical_rows):
    """First execute → (learning, cosine_distance) tuples, second → lexical scalars."""
    session = AsyncMock()
    semantic = MagicMock()
    semantic.all.return_value = semantic_rows
    lexical = MagicMock()
    lexical.scalars.return_value.all.return_value = lexical_rows
    session.execute.side_effect = [semantic, lexical]
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    return session, factory


@pytest.mark.asyncio
async def test_learning_projection_hybrid_merges_semantic_and_lexical(monkeypatch):
    monkeypatch.setattr("brain.memory.learning_retrieval.settings.MEMORY_RETRIEVAL_SEMANTIC", True)
    embedder = SimpleNamespace(embed=AsyncMock(return_value=[1.0, 0.0]), model="test-embedder")
    monkeypatch.setattr("brain.llm.get_embedding_provider", lambda: embedder)
    paraphrase = SimpleNamespace(id=1, statement="bills need a sign-off before payment",
                                 confidence=0.7, repo_scope="/app", promoted_from="episode")
    weak = SimpleNamespace(id=2, statement="unrelated neo4j tuning note",
                           confidence=0.9, repo_scope="/app", promoted_from="episode")
    lexical_hit = SimpleNamespace(id=3, statement="invoice approval requires two reviewers",
                                  confidence=0.5, repo_scope="/app", promoted_from="episode")
    session, factory = _hybrid_session([(paraphrase, 0.1), (weak, 0.7)], [lexical_hit])
    monkeypatch.setattr("brain.memory.learning_retrieval.async_session_factory", factory)
    results = await select_learnings_for_context("invoice approval", "/app")
    ids = [r["id"] for r in results]
    # Paraphrase (cos 0.9, no shared tokens) wins over the lexical-only hit;
    # the weak semantic-only row (cos 0.3 < 0.45) is dropped.
    assert ids == [1, 3]
    embedder.embed.assert_awaited_once_with("invoice approval")
    assert session.execute.await_count == 2
    semantic_sql = str(session.execute.await_args_list[0].args[0].compile(compile_kwargs={"literal_binds": True}))
    assert "memory_learnings.embedding_model = 'test-embedder'" in semantic_sql
    assert "memory_learnings.embedding IS NOT NULL" in semantic_sql
    assert "memory_learnings.status = 'active'" in semantic_sql
    lexical_sql = str(session.execute.await_args_list[1].args[0].compile(compile_kwargs={"literal_binds": True}))
    # Default dialect renders ILIKE as lower(...) LIKE lower(...).
    assert "LIKE" in lexical_sql and "LIMIT 64" in lexical_sql


@pytest.mark.asyncio
async def test_learning_projection_degrades_when_embedder_fails(monkeypatch):
    monkeypatch.setattr("brain.memory.learning_retrieval.settings.MEMORY_RETRIEVAL_SEMANTIC", True)

    def broken_provider():
        raise RuntimeError("no embedding key")

    monkeypatch.setattr("brain.llm.get_embedding_provider", broken_provider)
    row = SimpleNamespace(id=5, statement="invoice approval lesson", confidence=0.8,
                          repo_scope="/app", promoted_from="episode")
    session = AsyncMock()
    response = MagicMock()
    response.scalars.return_value.all.return_value = [row]
    session.execute.return_value = response
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    monkeypatch.setattr("brain.memory.learning_retrieval.async_session_factory", factory)
    results = await select_learnings_for_context("invoice approval", "/app")
    assert [r["id"] for r in results] == [5]
    # Only the lexical query ran.
    assert session.execute.await_count == 1
