import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from brain.context.runtime_context_builder import RuntimeContextBuilder
from brain.retrieval.service import RetrievalCandidate, RetrievalResult


class _Retrieval:
    def __init__(self, result):
        self.result = result

    async def retrieve(self, *_args, **_kwargs):
        return self.result


@pytest.mark.asyncio
async def test_runtime_context_fails_closed_before_loading_slices_when_index_is_stale():
    result = RetrievalResult(
        query="change search endpoint",
        intent="runtime_context",
        repository={
            "repository_path": "/app",
            "freshness": {"status": "stale", "source_head_commit": "current"},
        },
        candidates=[RetrievalCandidate(path="apps/api/routers/core.py")],
    )
    builder = RuntimeContextBuilder(_Retrieval(result))
    builder._load_slices = AsyncMock()

    payload = await builder.build("change search endpoint", "/app", max_tokens=1000)

    assert payload["status"] == "stale_blocked"
    assert "current_source_required_for_code_change" in payload["missing"]
    builder._load_slices.assert_not_awaited()
    assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 4000


@pytest.mark.asyncio
async def test_runtime_context_marks_empty_candidates_as_insufficient_evidence():
    result = RetrievalResult(
        query="find the route",
        intent="runtime_context",
        repository={"repository_path": "/app", "freshness": {"status": "current"}},
        candidates=[],
    )
    builder = RuntimeContextBuilder(_Retrieval(result))
    builder._load_slices = AsyncMock()

    payload = await builder.build("find the route", "/app")

    assert payload["status"] == "partial"
    assert "no_retrieval_candidates" in payload["missing"]
    builder._load_slices.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_context_marks_empty_slices_as_insufficient_evidence(monkeypatch):
    result = RetrievalResult(
        query="find the route",
        intent="runtime_context",
        repository={"repository_path": "/app", "freshness": {"status": "current"}},
        candidates=[RetrievalCandidate(path="apps/api/routers/core.py")],
    )
    builder = RuntimeContextBuilder(_Retrieval(result))
    builder._load_slices = AsyncMock(return_value=[])
    relevance = AsyncMock()
    monkeypatch.setattr(
        "brain.context.runtime_context_builder.select_relevant_normative_memory", relevance
    )

    payload = await builder.build("find the route", "/app")

    assert payload["status"] == "partial"
    assert "no_code_slices" in payload["missing"]
    relevance.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_context_does_not_report_success_when_budget_excludes_all_code(monkeypatch):
    result = RetrievalResult(
        query="budget gap",
        intent="runtime_context",
        repository={"repository_path": "/app", "freshness": {"status": "current"}},
        candidates=[RetrievalCandidate(path="apps/api/routers/core.py")],
    )
    builder = RuntimeContextBuilder(_Retrieval(result))
    builder._load_slices = AsyncMock(return_value=[{
        "path": "apps/api/routers/core.py", "range": [1, 50], "content": "x" * 2500,
    }])
    monkeypatch.setattr(
        "brain.context.runtime_context_builder.select_relevant_normative_memory",
        AsyncMock(return_value={"rules": [], "decisions": []}),
    )
    cache_write = AsyncMock()
    monkeypatch.setattr("brain.context.context_cache.put_cached_context", cache_write)

    payload = await builder.build("budget gap", "/app", max_tokens=500)

    assert payload["status"] == "partial"
    assert "code_slices_excluded_by_budget" in payload["missing"]
    assert not payload.get("slices")
    cache_write.assert_not_called()
    assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) <= 2000


@pytest.mark.asyncio
async def test_slice_loader_is_repository_scoped_and_matches_symbol_overlap(monkeypatch):
    """Same relative paths in another repository must never reach a runtime pack."""
    result = RetrievalResult(
        query="How does the session manager work?",
        intent="runtime_context",
        repository={"repository_id": 42},
        candidates=[RetrievalCandidate(path="brain/context/session.py", symbols=["get_session", "init_session"], ranges=[(100, 130)])],
    )
    statements = []
    rows = [
        (SimpleNamespace(start_line=80, end_line=110, chunk_index=1, content="overlap"), "brain/context/session.py"),
        (SimpleNamespace(start_line=131, end_line=160, chunk_index=2, content="outside"), "brain/context/session.py"),
    ]

    class _Result:
        def __init__(self, data):
            self._data = data

        def all(self):
            return self._data

    class _Session:
        def __init__(self):
            self._call = 0

        async def execute(self, statement):
            statements.append(statement)
            self._call += 1
            # First call: symbol enrichment query (returns 3-tuples)
            # Second call: chunk query (returns 2-tuples)
            if self._call == 1:
                return _Result([(100, 130, "brain/context/session.py")])
            return _Result(rows)

    class _SessionFactory:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(
        "brain.context.runtime_context_builder.async_session_factory", lambda: _SessionFactory()
    )
    slices = await RuntimeContextBuilder()._load_slices(result, max_bytes=12_000)

    # Overlapping chunk is always loaded; non-overlapping chunks are allowed
    # up to 2 per file for broader context.
    assert len(slices) >= 1
    assert slices[0] == {"path": "brain/context/session.py", "range": [80, 110], "content": "overlap"}
    compiled = str(statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "files.repository_id = 42" in compiled
