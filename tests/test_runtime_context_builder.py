import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from brain.context.runtime_context_builder import RuntimeContextBuilder, _unsupported_domain_query
from brain.retrieval.service import RetrievalCandidate, RetrievalResult


class _Retrieval:
    def __init__(self, result):
        self.result = result

    async def retrieve(self, *_args, **_kwargs):
        return self.result


@pytest.mark.asyncio
async def test_runtime_context_build_abstains_and_drops_irrelevant_candidates(monkeypatch):
    result = RetrievalResult(
        query="How do I configure the Kubernetes deployment for the mobile app?",
        intent="runtime_context",
        repository={"repository_path": "/app", "freshness": {"status": "current"}},
        candidates=[RetrievalCandidate(path=f"apps/api/generic_{idx}.py") for idx in range(12)],
    )
    builder = RuntimeContextBuilder(_Retrieval(result))
    builder._load_slices = AsyncMock(return_value=[{
        "path": "apps/api/generic_0.py", "range": [1, 10], "content": "generic API request handler",
    }])
    monkeypatch.setattr("brain.context.context_cache.get_cached_context", lambda *_: None)
    payload = await builder.build(result.query, "/app")
    assert payload["status"] == "partial"
    assert "no_relevant_candidates" in payload["missing"]
    assert "candidates" not in payload
    assert "slices" not in payload


@pytest.mark.asyncio
async def test_runtime_context_build_preserves_content_only_evidence(monkeypatch):
    result = RetrievalResult(
        query="How does soft deletion cascade through the generic store?",
        intent="runtime_context",
        repository={"repository_path": "/app", "freshness": {"status": "current"}, "repository_id": 1},
        candidates=[RetrievalCandidate(path="brain/database/generic_store.py")],
    )
    builder = RuntimeContextBuilder(_Retrieval(result))
    builder._load_slices = AsyncMock(return_value=[{
        "path": "brain/database/generic_store.py", "range": [1, 10],
        "content": "The soft deletion cascade marks child records deleted.",
    }])
    monkeypatch.setattr("brain.context.context_cache.get_cached_context", lambda *_: None)
    monkeypatch.setattr("brain.context.runtime_context_builder.select_relevant_normative_memory", AsyncMock(return_value=[]))
    monkeypatch.setattr("brain.context.context_cache.put_cached_context", lambda *args, **kwargs: None)
    payload = await builder.build(result.query, "/app")
    assert payload["status"] == "ok"
    assert payload["slices"][0]["content"].startswith("The soft deletion")


@pytest.mark.asyncio
async def test_named_ui_style_context_preserves_nearby_scoped_stylesheet(monkeypatch):
    result = RetrievalResult(query="Fix Bubble CSS clipping", intent="runtime_context",
                             repository={"repository_id": 42, "repository_path": "/app",
                                         "freshness": {"status": "current"}},
                             candidates=[RetrievalCandidate(path="ui/cards/Bubble.tsx")]
                             + [RetrievalCandidate(path=f"src/noise{i}.py") for i in range(11)])
    session = AsyncMock()
    response = MagicMock()
    response.scalars.return_value.all.return_value = ["backend/wrong.css", "ui/theme.scss", "ui/cards/Bubble.css"]
    session.execute.return_value = response
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    monkeypatch.setattr("brain.context.runtime_context_builder.async_session_factory", factory)
    builder = RuntimeContextBuilder(_Retrieval(result))
    builder._load_slices = AsyncMock(return_value=[
        {"path": "ui/cards/Bubble.tsx", "content": "Bubble component", "range": [1, 10]},
        {"path": "ui/cards/Bubble.css", "content": "overflow: visible;", "range": [1, 10]},
    ])
    monkeypatch.setattr("brain.context.context_cache.get_cached_context", lambda *_: None)
    monkeypatch.setattr("brain.context.context_cache.put_cached_context", lambda *a, **k: None)
    monkeypatch.setattr("brain.context.runtime_context_builder.select_relevant_normative_memory", AsyncMock(return_value=[]))
    payload = await builder.build(result.query, "/app")
    assert payload["status"] == "ok"
    assert [c["path"] for c in payload["candidates"]][:2] == ["ui/cards/Bubble.tsx", "ui/cards/Bubble.css"]
    assert len(result.candidates) == 12 and len(payload["candidates"]) == 6
    statement = session.execute.await_args.args[0]
    assert "files.repository_id = 42" in str(statement.compile(compile_kwargs={"literal_binds": True}))


@pytest.mark.parametrize("query", ["Fix Bubble API serialization", "Fix Missing CSS clipping"])
@pytest.mark.asyncio
async def test_style_companion_requires_style_intent_and_a_named_component(monkeypatch, query):
    result = RetrievalResult(query=query, intent="runtime_context", repository={"repository_id": 42},
                             candidates=[RetrievalCandidate(path="ui/Bubble.tsx")])
    factory = MagicMock()
    monkeypatch.setattr("brain.context.runtime_context_builder.async_session_factory", factory)
    await RuntimeContextBuilder()._include_style_companion(result)
    factory.assert_not_called()
    assert len(result.candidates) == 1


@pytest.mark.parametrize("query,paths,expected", [
    ("Fix Bubble global CSS clipping",
     ["ui/cards/access.css", "ui/globals.css"], "ui/globals.css"),
    ("Fix Bubble CSS clipping",
     ["backend/wrong.css", "ui/theme.scss"], "ui/theme.scss"),
])
@pytest.mark.asyncio
async def test_style_companion_prefers_named_stylesheet_then_directory(monkeypatch, query, paths, expected):
    result = RetrievalResult(query=query, intent="runtime_context", repository={"repository_id": 42},
                             candidates=[RetrievalCandidate(path="ui/cards/Bubble.tsx")])
    session = AsyncMock()
    response = MagicMock()
    response.scalars.return_value.all.return_value = paths
    session.execute.return_value = response
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    monkeypatch.setattr("brain.context.runtime_context_builder.async_session_factory", factory)
    await RuntimeContextBuilder()._include_style_companion(result)
    assert result.candidates[1].path == expected


@pytest.mark.asyncio
async def test_small_successful_context_does_not_replace_full_budget_cache(monkeypatch):
    from brain.context.context_cache import clear_context_cache

    result = RetrievalResult(query="find invoice route", intent="runtime_context",
                             repository={"repository_path": "/indexed/budget-cache-fixture",
                                         "freshness": {"status": "current", "source_head_commit": "r1"}},
                             candidates=[RetrievalCandidate(path="src/invoice.py")])
    builder = RuntimeContextBuilder(_Retrieval(result))

    async def slices(_result, max_bytes):
        return [{"path": "src/invoice.py", "range": [1, 10],
                 "content": "x" * min(5000, max_bytes // 2)}]

    builder._load_slices = AsyncMock(side_effect=slices)
    monkeypatch.setattr("brain.context.runtime_context_builder.select_relevant_normative_memory", AsyncMock(return_value=[]))
    clear_context_cache()
    try:
        full = await builder.build(result.query, "/indexed/budget-cache-fixture")
        small = await builder.build(result.query, "/indexed/budget-cache-fixture", max_tokens=500)
        assert full["status"] == small["status"] == "ok"
        assert len(full["slices"][0]["content"]) > len(small["slices"][0]["content"])
        repeat = await builder.build(result.query, "/indexed/budget-cache-fixture")
        assert repeat["_cache_hit"] and repeat["slices"] == full["slices"]
        assert builder._load_slices.await_count == 2
    finally:
        clear_context_cache()


@pytest.mark.asyncio
async def test_runtime_cache_uses_verified_revision_and_respects_smaller_budget(monkeypatch):
    from brain.context.context_cache import clear_context_cache

    clear_context_cache()
    result = RetrievalResult(query="find invoice route", intent="runtime_context",
                             repository={"repository_path": "/indexed/cache-fixture",
                                         "freshness": {"status": "current", "source_head_commit": "r1"}},
                             candidates=[RetrievalCandidate(path="src/invoice.py")])
    builder = RuntimeContextBuilder(_Retrieval(result))
    builder._load_slices = AsyncMock(return_value=[
        {"path": "src/invoice.py", "range": [1, 10], "content": "invoice route " + "x" * 1000},
    ])
    monkeypatch.setattr("brain.context.runtime_context_builder.select_relevant_normative_memory", AsyncMock(return_value=[]))
    try:
        first = await builder.build(result.query, "/indexed/cache-fixture")
        assert first["status"] == "ok"
        repeat = await builder.build(result.query, "/indexed/cache-fixture")
        assert repeat["_cache_hit"]
        assert builder._load_slices.await_count == 1
        small = await builder.build(result.query, "/indexed/cache-fixture", max_tokens=100)
        assert not small.get("_cache_hit")
        assert len(json.dumps(small, ensure_ascii=False, separators=(",", ":")).encode()) <= 400
        result.repository["freshness"]["source_head_commit"] = "r2"
        changed = await builder.build(result.query, "/indexed/cache-fixture")
        assert not changed.get("_cache_hit")
        assert builder._load_slices.await_count == 3
        result.repository["freshness"]["status"] = "stale"
        monkeypatch.setattr("brain.context.runtime_context_builder._queue_auto_reindex", AsyncMock(return_value=False))
        stale = await builder.build(result.query, "/indexed/cache-fixture")
        assert stale["status"] == "stale_blocked" and not stale.get("_cache_hit")
    finally:
        clear_context_cache()


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
        query="find the core route",
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

    payload = await builder.build("find the core route", "/app", max_tokens=500)

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


class _FakeRedis:
    """SET NX EX with a controllable clock."""

    def __init__(self):
        self.now = 0.0
        self.keys: dict[str, float] = {}
        self.set_calls: list[dict] = []

    async def set(self, key, value, nx=False, ex=None):
        self.set_calls.append({"key": key, "nx": nx, "ex": ex})
        expires = self.keys.get(key)
        if nx and expires is not None and expires > self.now:
            return False
        self.keys[key] = self.now + (ex or 0)
        return True

    async def delete(self, key):
        self.keys.pop(key, None)


def _stale_result():
    return RetrievalResult(
        query="change search endpoint",
        intent="runtime_context",
        repository={"repository_path": "/app", "freshness": {"status": "stale"}},
        candidates=[RetrievalCandidate(path="apps/api/routers/core.py")],
    )


@pytest.fixture
def auto_reindex_env(monkeypatch):
    from brain.config.settings import settings

    redis = _FakeRedis()
    queue = SimpleNamespace(enqueue=AsyncMock(return_value="job-1"))
    calls = []

    def fake_queue_for_job(redis_arg, prefix, job_type, *, pools_enabled):
        calls.append({"job_type": job_type, "pools_enabled": pools_enabled, "prefix": prefix})
        return queue

    monkeypatch.setattr("brain.database.session.redis_client", redis)
    monkeypatch.setattr("brain.workers.queue.queue_for_job", fake_queue_for_job)
    monkeypatch.setattr(settings, "BRAIN_WORKER_POOLS_V2_ENABLED", True)
    monkeypatch.setattr(settings, "AUTO_REINDEX_MIN_INTERVAL_S", 3600)
    return SimpleNamespace(redis=redis, queue=queue, calls=calls)


@pytest.mark.asyncio
async def test_stale_blocked_auto_reindex_routes_to_the_reindex_pool(auto_reindex_env):
    builder = RuntimeContextBuilder(_Retrieval(_stale_result()))

    payload = await builder.build("change search endpoint", "/app", max_tokens=1000)

    assert "auto_reindex_queued" in payload["missing"]
    assert auto_reindex_env.calls == [
        {"job_type": "reindex", "pools_enabled": True, "prefix": auto_reindex_env.calls[0]["prefix"]}
    ]
    auto_reindex_env.queue.enqueue.assert_awaited_once_with("reindex", {"repo_path": "/app"})


@pytest.mark.asyncio
async def test_stale_blocked_auto_reindex_is_rate_limited_to_one_hour(auto_reindex_env):
    redis, queue = auto_reindex_env.redis, auto_reindex_env.queue

    async def build():
        return await RuntimeContextBuilder(_Retrieval(_stale_result())).build(
            "change search endpoint", "/app", max_tokens=1000
        )

    first = await build()
    redis.now = 3599
    second = await build()
    redis.now = 3601
    third = await build()

    assert "auto_reindex_queued" in first["missing"]
    assert "auto_reindex_queued" not in second["missing"]
    assert "auto_reindex_queued" in third["missing"]
    assert queue.enqueue.await_count == 2
    assert {c["ex"] for c in redis.set_calls} == {3600}
    assert all(c["nx"] for c in redis.set_calls)


@pytest.mark.asyncio
async def test_failed_auto_reindex_enqueue_releases_the_throttle(auto_reindex_env):
    auto_reindex_env.queue.enqueue = AsyncMock(side_effect=[RuntimeError("redis down"), "job-2"])
    builder = RuntimeContextBuilder(_Retrieval(_stale_result()))

    first = await builder.build("change search endpoint", "/app", max_tokens=1000)
    second = await builder.build("change search endpoint", "/app", max_tokens=1000)

    assert "auto_reindex_queued" not in first["missing"]
    assert "auto_reindex_queued" in second["missing"]
def test_runtime_context_rejects_unrepresented_multi_term_domain():
    result = RetrievalResult(
        query="How do I configure the Kubernetes deployment for the mobile app?",
        intent="runtime_context",
        repository={},
        candidates=[RetrievalCandidate(path="apps/api/main.py")],
    )
    assert _unsupported_domain_query(result.query, result)


def test_runtime_context_keeps_supported_path_and_symbol_evidence():
    result = RetrievalResult(
        query="How does embedding backfill work?",
        intent="runtime_context",
        repository={},
        candidates=[RetrievalCandidate(path="brain/embeddings/backfill.py")],
    )
    assert not _unsupported_domain_query(result.query, result)


def test_runtime_context_ignores_generic_query_verbs():
    result = RetrievalResult(
        query="Explain how API auth handles each request",
        intent="runtime_context",
        repository={},
        candidates=[RetrievalCandidate(path="apps/api/auth.py")],
    )
    assert not _unsupported_domain_query(result.query, result)


def test_runtime_context_keeps_concept_found_only_in_slice_content():
    result = RetrievalResult(
        query="How does soft deletion cascade through the generic store?",
        intent="runtime_context",
        repository={},
        candidates=[RetrievalCandidate(path="brain/database/generic_store.py")],
    )
    slices = [{"path": "brain/database/generic_store.py", "content": "The soft deletion cascade marks child records deleted."}]
    assert not _unsupported_domain_query(result.query, result, slices)


def test_runtime_context_keeps_short_meaningful_provider_terms_in_slice():
    result = RetrievalResult(
        query="How does the LLM provider implement backoff headers?",
        intent="runtime_context",
        repository={},
        candidates=[RetrievalCandidate(path="brain/llm/providers/openai_compatible.py")],
    )
    slices = [{"path": "brain/llm/providers/openai_compatible.py", "content": "LLM provider retries with exponential backoff headers."}]
    assert not _unsupported_domain_query(result.query, result, slices)


def test_runtime_context_ignores_grammatical_stopwords_for_unsupported_query():
    result = RetrievalResult(
        query="Why does the Kubernetes mobile app exist?",
        intent="runtime_context",
        repository={},
        candidates=[RetrievalCandidate(path="apps/api/main.py")],
    )
    assert _unsupported_domain_query(result.query, result, [])
