from __future__ import annotations

import asyncio
import hashlib
import sys
import types
from contextlib import asynccontextmanager
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from brain.config.settings import settings
from brain.database.models import File, FileChunk
from brain.embeddings.constants import VectorSearchStatus
from brain.late_interaction.application import (
    apply_late_interaction,
    late_interaction_candidate_budget,
)
from brain.late_interaction.metrics import reset_for_tests, snapshot
from brain.late_interaction.shadow import (
    ShadowEvent,
    ShadowRankedItem,
    hash_query,
    make_shadow_idempotency_key,
)
from brain.retrieval.pipeline import (
    _rerank_late_counterfactual,
    _resolve_late_interaction_candidates,
)
from brain.retrieval.reranker import RerankCandidate, RerankResult
from brain.search.code_search import VectorSearchResult, search_code


@dataclass
class _Candidate:
    chunk_id: int
    path: str
    content_hash: str = ""


@dataclass
class _Score:
    chunk_id: int
    path: str
    score: float


@dataclass
class _Result:
    status: str
    scores: list[_Score]
    coverage: float = 1.0
    latency_ms: float = 7.0
    model_revision: str = "test-revision"
    index_revision: str = "test-index"
    reason: str = ""


def _install_contract(
    monkeypatch,
    rerank,
    shadow_events=None,
    shadow_error=None,
    shadow_assertion=None,
):
    client_module = types.ModuleType("brain.late_interaction.client")
    client_module.LateInteractionCandidate = _Candidate
    client_module.get_late_interaction_client = lambda: types.SimpleNamespace(
        rerank=rerank
    )
    monkeypatch.setitem(sys.modules, "brain.late_interaction.client", client_module)

    async def record(event):
        if shadow_assertion is not None:
            shadow_assertion()
        if shadow_error:
            raise shadow_error
        if shadow_events is not None:
            shadow_events.append(event)

    shadow_module = types.ModuleType("brain.late_interaction.shadow")
    shadow_module.ShadowEvent = ShadowEvent
    shadow_module.ShadowRankedItem = ShadowRankedItem
    shadow_module.hash_query = hash_query
    shadow_module.make_shadow_idempotency_key = make_shadow_idempotency_key
    shadow_module.record_shadow_event = record
    monkeypatch.setitem(sys.modules, "brain.late_interaction.shadow", shadow_module)


@asynccontextmanager
async def _empty_session_factory():
    yield types.SimpleNamespace(execute=AsyncMock())


@pytest.fixture
def isolated_late_metrics():
    reset_for_tests()
    yield
    reset_for_tests()


def _vector_matches(count: int, repository_id: int = 7):
    matches = []
    for index in range(1, count + 1):
        file_obj = File(
            id=index,
            repository_id=repository_id,
            path=f"src/file_{index}.py",
            language="python",
            file_type="source_code",
            summary=f"file {index}",
        )
        chunk = FileChunk(
            id=index,
            file_id=index,
            chunk_index=0,
            content=f"content {index}",
            summary=f"chunk {index}",
            start_line=index,
            end_line=index,
        )
        matches.append((1.0 - index / 100.0, chunk, file_obj))
    return matches


def test_remote_chunk_budget_does_not_reuse_legacy_file_limit(monkeypatch):
    monkeypatch.setattr(settings, "LATE_INTERACTION_RERANK_LIMIT", 1)
    monkeypatch.setattr(settings, "LATE_INTERACTION_MAX_RERANK_CHUNKS", 3)

    assert late_interaction_candidate_budget() == 3


@pytest.mark.asyncio
async def test_deep_application_uses_injected_client_with_global_flags_off(
    monkeypatch,
):
    events = []
    rerank = AsyncMock(
        return_value=_Result(
            status="scored",
            scores=[_Score(1, "a.py", 0.1), _Score(2, "b.py", 0.9)],
        )
    )
    injected = types.SimpleNamespace(rerank=rerank)
    _install_contract(monkeypatch, AsyncMock(), shadow_events=events)
    monkeypatch.setattr(settings, "LATE_INTERACTION_ENABLED", False)
    monkeypatch.setattr(settings, "LATE_INTERACTION_SHADOW_ENABLED", False)
    monkeypatch.setattr(settings, "LATE_INTERACTION_RERANK_ENABLED", False)
    monkeypatch.setattr(settings, "LATE_INTERACTION_CANARY_PERCENT", 0.0)

    result = await apply_late_interaction(
        query="slow exact retrieval",
        repository_id=7,
        candidates=[_Candidate(1, "a.py"), _Candidate(2, "b.py")],
        baseline_top_k=["a.py", "b.py"],
        build_counterfactual=lambda _result: ["b.py", "a.py"],
        query_class="deep_context",
        mode="deep",
        client=injected,
        timeout_s=30.0,
    )

    assert result.applied is True
    assert result.effective_top_k == ["b.py", "a.py"]
    assert result.execution_mode == "deep"
    assert result.canary_selected is True
    assert result.to_debug()["execution_mode"] == "deep"
    rerank.assert_awaited_once()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_shared_application_shadow_records_counterfactual_without_raw_query(
    monkeypatch,
):
    events = []
    rerank = AsyncMock(
        return_value=_Result(
            status="scored",
            scores=[_Score(1, "a.py", 0.1), _Score(2, "b.py", 0.9)],
        )
    )
    _install_contract(monkeypatch, rerank, shadow_events=events)
    monkeypatch.setattr(settings, "LATE_INTERACTION_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_SHADOW_PERSIST_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_RERANK_ENABLED", False)

    result = await apply_late_interaction(
        query="найди parser",
        repository_id=7,
        candidates=[_Candidate(1, "a.py"), _Candidate(2, "b.py")],
        baseline_top_k=["a.py", "b.py"],
        build_counterfactual=lambda _result: ["b.py", "a.py"],
        query_class="code_search",
        index_revision="abc",
    )

    assert result.counterfactual_top_k == ["b.py", "a.py"]
    assert result.effective_top_k == ["a.py", "b.py"]
    assert result.applied is False
    assert result.to_debug()["index_revision"] == "test-index"
    assert len(events) == 1
    assert events[0].language == "ru"
    assert events[0].query_hash == hash_query(7, "найди parser")
    assert [item.path for item in events[0].baseline_final_top_k] == ["a.py", "b.py"]
    assert [item.path for item in events[0].counterfactual_final_top_k] == ["b.py", "a.py"]
    assert events[0].status == "scored"
    assert events[0].skip_reason is None
    assert not hasattr(events[0], "query")


@pytest.mark.asyncio
async def test_shared_application_can_score_without_persisting_fixture_shadow(
    monkeypatch,
):
    events = []
    rerank = AsyncMock(
        return_value=_Result(
            status="scored",
            scores=[_Score(1, "a.py", 0.1), _Score(2, "b.py", 0.9)],
        )
    )
    _install_contract(monkeypatch, rerank, shadow_events=events)
    monkeypatch.setattr(settings, "LATE_INTERACTION_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_SHADOW_PERSIST_ENABLED", False)
    monkeypatch.setattr(settings, "LATE_INTERACTION_RERANK_ENABLED", False)

    result = await apply_late_interaction(
        query="curated fixture",
        repository_id=7,
        candidates=[_Candidate(1, "a.py"), _Candidate(2, "b.py")],
        baseline_top_k=["a.py", "b.py"],
        build_counterfactual=lambda _result: ["b.py", "a.py"],
        query_class="evaluation_fixture",
    )

    assert result.status == "scored"
    assert result.counterfactual_top_k == ["b.py", "a.py"]
    assert events == []


@pytest.mark.asyncio
async def test_shadow_request_id_deduplicates_retries_and_uuid_is_only_fallback(
    monkeypatch,
):
    events = []
    fallback_ids = iter(("fallback-one", "fallback-two"))
    monkeypatch.setattr(
        "brain.late_interaction.application.uuid.uuid4",
        lambda: types.SimpleNamespace(hex=next(fallback_ids)),
    )
    rerank = AsyncMock(
        return_value=_Result(
            status="scored",
            scores=[_Score(1, "a.py", 1.0)],
        )
    )
    _install_contract(monkeypatch, rerank, shadow_events=events)
    monkeypatch.setattr(settings, "LATE_INTERACTION_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_RERANK_ENABLED", False)

    async def record_once(request_id=None):
        return await apply_late_interaction(
            query="parser",
            repository_id=7,
            candidates=[_Candidate(1, "a.py")],
            baseline_top_k=["a.py"],
            build_counterfactual=lambda _result: ["a.py"],
            query_class="code_search",
            request_id=request_id,
        )

    await record_once("surface-request-123")
    await record_once("surface-request-123")
    await record_once()
    await record_once()

    keys = [event.idempotency_key for event in events]
    assert keys[:2] == [
        make_shadow_idempotency_key(7, "surface-request-123"),
        make_shadow_idempotency_key(7, "surface-request-123"),
    ]
    assert keys[2:] == [
        make_shadow_idempotency_key(7, "fallback-one"),
        make_shadow_idempotency_key(7, "fallback-two"),
    ]
    assert keys[2] != keys[3]


@pytest.mark.asyncio
async def test_shared_application_partial_coverage_never_changes_active_output(
    monkeypatch,
):
    rerank = AsyncMock(
        return_value=_Result(
            status="scored",
            scores=[_Score(2, "b.py", 0.9)],
            coverage=0.5,
        )
    )
    _install_contract(monkeypatch, rerank)
    monkeypatch.setattr(settings, "LATE_INTERACTION_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_SHADOW_ENABLED", False)
    monkeypatch.setattr(settings, "LATE_INTERACTION_RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_CANARY_PERCENT", 100.0)
    monkeypatch.setattr(settings, "LATE_INTERACTION_MIN_CANDIDATE_COVERAGE", 1.0)

    baseline = ["a.py", "b.py"]
    result = await apply_late_interaction(
        query="parser",
        repository_id=7,
        candidates=[_Candidate(1, "a.py"), _Candidate(2, "b.py")],
        baseline_top_k=baseline,
        build_counterfactual=lambda _result: ["b.py", "a.py"],
        query_class="code_search",
    )

    assert result.counterfactual_built is True
    assert result.counterfactual_top_k == ["b.py", "a.py"]
    assert result.status == "partial"
    assert result.reason == "insufficient_candidate_coverage"
    assert result.effective_top_k == baseline
    assert result.applied is False


@pytest.mark.asyncio
async def test_shared_application_active_canary_and_shadow_failure_are_fail_open(
    monkeypatch,
    isolated_late_metrics,
):
    rerank = AsyncMock(
        return_value=_Result(
            status="scored",
            scores=[_Score(1, "a.py", 0.1), _Score(2, "b.py", 0.9)],
        )
    )
    _install_contract(
        monkeypatch,
        rerank,
        shadow_error=RuntimeError("shadow database unavailable"),
    )
    monkeypatch.setattr(settings, "LATE_INTERACTION_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_CANARY_PERCENT", 100.0)

    result = await apply_late_interaction(
        query="parser",
        repository_id=7,
        candidates=[_Candidate(1, "a.py"), _Candidate(2, "b.py")],
        baseline_top_k=["a.py", "b.py"],
        build_counterfactual=lambda _result: ["b.py", "a.py"],
        query_class="code_search",
    )

    assert result.applied is True
    assert result.effective_top_k == ["b.py", "a.py"]
    metrics = snapshot()
    assert metrics["rerank_requests"] == 1
    assert metrics["rerank_applied"] == 1
    assert metrics["rerank_fail_open"] == 0
    assert metrics["rerank_timeouts"] == 0
    assert metrics["rerank_latency_ms_total"] >= 0
    # The application layer owns rerank metrics only. Provider attempts are
    # counted once by the concrete remote client.
    assert metrics["provider_requests"] == 0


@pytest.mark.asyncio
async def test_shared_application_timeout_returns_byte_identical_baseline(
    monkeypatch,
    isolated_late_metrics,
):
    async def slow_rerank(**_kwargs):
        await asyncio.sleep(0.05)

    _install_contract(monkeypatch, slow_rerank)
    monkeypatch.setattr(settings, "LATE_INTERACTION_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_SHADOW_ENABLED", False)
    monkeypatch.setattr(settings, "LATE_INTERACTION_RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_CANARY_PERCENT", 100.0)
    monkeypatch.setattr(settings, "LATE_INTERACTION_RERANK_TIMEOUT_S", 0.001)
    callback = AsyncMock()
    baseline = ["a.py", "b.py"]

    result = await apply_late_interaction(
        query="parser",
        repository_id=7,
        candidates=[_Candidate(1, "a.py")],
        baseline_top_k=baseline,
        build_counterfactual=callback,
        query_class="code_search",
    )

    assert result.status == "timed_out"
    assert result.effective_top_k == baseline
    assert result.applied is False
    callback.assert_not_awaited()
    metrics = snapshot()
    assert metrics["rerank_requests"] == 1
    assert metrics["rerank_applied"] == 0
    assert metrics["rerank_fail_open"] == 1
    assert metrics["rerank_timeouts"] == 1
    assert metrics["rerank_latency_ms_total"] > 0


@pytest.mark.asyncio
async def test_inventory_mismatch_is_counted_as_fail_open(
    monkeypatch,
    isolated_late_metrics,
):
    rerank = AsyncMock(
        return_value=_Result(
            status="inventory_mismatch",
            scores=[],
            reason="identity_digest_mismatch",
        )
    )
    _install_contract(monkeypatch, rerank)
    monkeypatch.setattr(settings, "LATE_INTERACTION_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_SHADOW_ENABLED", False)
    monkeypatch.setattr(settings, "LATE_INTERACTION_RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_CANARY_PERCENT", 100.0)

    result = await apply_late_interaction(
        query="parser",
        repository_id=7,
        candidates=[_Candidate(1, "a.py")],
        baseline_top_k=["a.py"],
        build_counterfactual=lambda _result: ["a.py"],
        query_class="code_search",
    )

    assert result.status == "inventory_mismatch"
    assert result.effective_top_k == ["a.py"]
    metrics = snapshot()
    assert metrics["rerank_fail_open"] == 1
    assert metrics["rerank_latency_sample_count"] == 1
    assert metrics["window"] == "current API process; last 512 latency samples"
    assert metrics["durable"] is False


@pytest.mark.asyncio
async def test_code_search_flags_off_keeps_narrow_call_and_legacy_payload(
    monkeypatch,
):
    captured = {}

    async def vector_search(_query, top_k, repository_id):
        captured.update(top_k=top_k, repository_id=repository_id)
        return VectorSearchResult(
            matches=_vector_matches(3),
            status=VectorSearchStatus.OK,
        )

    monkeypatch.setattr(settings, "LATE_INTERACTION_ENABLED", False)
    monkeypatch.setattr(
        "brain.search.code_search.async_session_factory",
        _empty_session_factory,
    )
    monkeypatch.setattr("brain.search.code_search.extract_keywords", lambda _query: [])
    monkeypatch.setattr("brain.search.code_search.vector_search_chunks", vector_search)

    response = await search_code("parser", limit=2, repository_id=7)

    assert captured == {"top_k": 2, "repository_id": 7}
    assert response["files"] == []
    assert response["symbols"] == []
    assert [chunk["file_path"] for chunk in response["chunks"]] == [
        "src/file_1.py",
        "src/file_2.py",
    ]
    assert "chunk_id" not in response["chunks"][0]
    assert "late_interaction" not in response


@pytest.mark.asyncio
async def test_code_search_shadow_uses_wide_pool_without_changing_baseline(
    monkeypatch,
):
    calls = []
    narrow_matches = _vector_matches(2)
    wide_matches = list(reversed(_vector_matches(4)))

    async def vector_search(_query, top_k, repository_id):
        calls.append((top_k, repository_id))
        matches = narrow_matches if top_k == 2 else wide_matches
        return VectorSearchResult(matches=matches, status=VectorSearchStatus.OK)

    rerank = AsyncMock(
        return_value=_Result(
            status="scored",
            scores=[
                _Score(4, "src/file_4.py", 0.9),
                _Score(3, "src/file_3.py", 0.8),
                _Score(2, "src/file_2.py", 0.2),
                _Score(1, "src/file_1.py", 0.1),
            ],
        )
    )
    shadow_events = []
    _install_contract(monkeypatch, rerank, shadow_events=shadow_events)
    monkeypatch.setattr(settings, "LATE_INTERACTION_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_RERANK_ENABLED", False)
    monkeypatch.setattr(settings, "LATE_INTERACTION_MAX_RERANK_CHUNKS", 4)
    monkeypatch.setattr(settings, "LATE_INTERACTION_SEARCH_CANDIDATE_LIMIT", 4)
    monkeypatch.setattr(
        "brain.search.code_search.async_session_factory",
        _empty_session_factory,
    )
    monkeypatch.setattr("brain.search.code_search.extract_keywords", lambda _query: [])
    monkeypatch.setattr("brain.search.code_search.vector_search_chunks", vector_search)

    response = await search_code("parser", limit=2, repository_id=7)

    assert calls == [(2, 7), (4, 7)]
    assert response["files"] == []
    assert response["symbols"] == []
    assert [chunk["file_path"] for chunk in response["chunks"]] == [
        "src/file_1.py",
        "src/file_2.py",
    ]
    rerank.assert_awaited_once()
    assert [candidate.chunk_id for candidate in rerank.await_args.kwargs["candidates"]] == [
        4,
        3,
        2,
        1,
    ]
    assert len(shadow_events) == 1
    assert [item.path for item in shadow_events[0].baseline_final_top_k] == [
        "src/file_1.py",
        "src/file_2.py",
    ]


@pytest.mark.asyncio
async def test_code_search_active_reranks_wide_chunk_pool_only(monkeypatch):
    calls = []
    narrow_matches = _vector_matches(2)
    wide_matches = _vector_matches(4)

    async def vector_search(_query, top_k, repository_id):
        calls.append((top_k, repository_id))
        matches = narrow_matches if top_k == 2 else wide_matches
        return VectorSearchResult(matches=matches, status=VectorSearchStatus.OK)

    rerank = AsyncMock(
        return_value=_Result(
            status="scored",
            scores=[
                _Score(1, "src/file_1.py", 0.1),
                _Score(2, "src/file_2.py", 0.2),
                _Score(3, "src/file_3.py", 0.8),
                _Score(4, "src/file_4.py", 0.9),
            ],
        )
    )
    _install_contract(monkeypatch, rerank)
    monkeypatch.setattr(settings, "LATE_INTERACTION_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_SHADOW_ENABLED", False)
    monkeypatch.setattr(settings, "LATE_INTERACTION_RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_CANARY_PERCENT", 100.0)
    monkeypatch.setattr(settings, "LATE_INTERACTION_MAX_RERANK_CHUNKS", 4)
    monkeypatch.setattr(settings, "LATE_INTERACTION_SEARCH_CANDIDATE_LIMIT", 4)
    monkeypatch.setattr(
        "brain.search.code_search.async_session_factory",
        _empty_session_factory,
    )
    monkeypatch.setattr("brain.search.code_search.extract_keywords", lambda _query: [])
    monkeypatch.setattr("brain.search.code_search.vector_search_chunks", vector_search)

    response = await search_code("parser", limit=2, repository_id=7)

    assert calls == [(2, 7), (4, 7)]
    assert response["files"] == []
    assert response["symbols"] == []
    assert [chunk["file_path"] for chunk in response["chunks"]] == [
        "src/file_4.py",
        "src/file_3.py",
    ]
    rerank.assert_awaited_once()
    assert rerank.await_args.kwargs["repository_id"] == 7
    assert [candidate.chunk_id for candidate in rerank.await_args.kwargs["candidates"]] == [
        1,
        2,
        3,
        4,
    ]


@pytest.mark.asyncio
async def test_code_search_releases_lexical_session_before_remote_work(monkeypatch):
    state = {"active": False, "exited": False, "entries": 0}
    remote_checks = []

    @asynccontextmanager
    async def tracking_session_factory():
        assert state["active"] is False
        state["active"] = True
        state["entries"] += 1
        try:
            yield types.SimpleNamespace(execute=AsyncMock())
        finally:
            state["active"] = False
            state["exited"] = True

    def assert_session_released():
        assert state["active"] is False
        assert state["exited"] is True
        remote_checks.append("released")

    async def vector_search(_query, top_k, repository_id):
        assert_session_released()
        return VectorSearchResult(
            matches=_vector_matches(top_k, repository_id=repository_id),
            status=VectorSearchStatus.OK,
        )

    async def rerank(**_kwargs):
        assert_session_released()
        return _Result(
            status="scored",
            scores=[
                _Score(index, f"src/file_{index}.py", 1.0 / index)
                for index in range(1, 5)
            ],
        )

    _install_contract(
        monkeypatch,
        rerank,
        shadow_events=[],
        shadow_assertion=assert_session_released,
    )
    monkeypatch.setattr(settings, "LATE_INTERACTION_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_RERANK_ENABLED", False)
    monkeypatch.setattr(settings, "LATE_INTERACTION_MAX_RERANK_CHUNKS", 4)
    monkeypatch.setattr(settings, "LATE_INTERACTION_SEARCH_CANDIDATE_LIMIT", 4)
    monkeypatch.setattr(
        "brain.search.code_search.async_session_factory",
        tracking_session_factory,
    )
    monkeypatch.setattr("brain.search.code_search.extract_keywords", lambda _query: [])
    monkeypatch.setattr("brain.search.code_search.vector_search_chunks", vector_search)

    response = await search_code("parser", limit=2, repository_id=7)

    assert state == {"active": False, "exited": True, "entries": 1}
    assert remote_checks == ["released", "released", "released", "released"]
    assert [chunk["file_path"] for chunk in response["chunks"]] == [
        "src/file_1.py",
        "src/file_2.py",
    ]


@pytest.mark.asyncio
async def test_pipeline_candidate_resolution_is_dense_first_bounded_and_repo_scoped(
    monkeypatch,
):
    _install_contract(monkeypatch, AsyncMock())
    monkeypatch.setattr(settings, "LATE_INTERACTION_MAX_RERANK_CHUNKS", 3)
    dense = _vector_matches(1, repository_id=11)
    captured = {}

    class _Rows:
        def all(self):
            return [
                (20, "second content", "b.py"),
                (30, "third content", "a.py"),
            ]

    class _Session:
        async def execute(self, statement):
            captured["sql"] = str(statement)
            return _Rows()

    @asynccontextmanager
    async def session_factory():
        yield _Session()

    monkeypatch.setattr(
        "brain.retrieval.pipeline.async_session_factory",
        session_factory,
    )
    dense[0][2].path = "dense.py"
    candidates = await _resolve_late_interaction_candidates(
        repository_id=11,
        pool_paths=["dense.py", "b.py", "a.py"],
        dense_matches=dense,
    )

    assert [(candidate.chunk_id, candidate.path) for candidate in candidates] == [
        (1, "dense.py"),
        (20, "b.py"),
        (30, "a.py"),
    ]
    assert candidates[0].content_hash == hashlib.sha256(
        b"content 1"
    ).hexdigest()
    assert "files.repository_id" in captured["sql"]
    assert len(candidates) == 3


@pytest.mark.asyncio
async def test_pipeline_counterfactual_never_reuses_baseline_cache_or_llm(
    monkeypatch,
):
    expected = RerankResult(top_paths=["a.py"], candidates=[])
    rerank = AsyncMock(return_value=expected)
    monkeypatch.setattr("brain.retrieval.pipeline.rerank_top_k", rerank)
    pool = [RerankCandidate(path="a.py", reranker_score=1.0)]

    result = await _rerank_late_counterfactual(
        pool,
        task_description="find parser",
        task_type="bugfix",
        repository_name="brain",
        precision_k=1,
        expected_surface="engine",
        two_stage=True,
        commit_hash="abc",
    )

    assert result is expected
    rerank.assert_awaited_once_with(
        pool,
        task_description="find parser",
        task_type="bugfix",
        repo_hash="brain",
        top_k=1,
        use_cache=False,
        use_llm=False,
        expected_surface="engine",
        two_stage=True,
        commit_hash="abc",
    )


@pytest.mark.asyncio
async def test_api_ask_and_search_share_integrated_code_search(monkeypatch):
    from apps.api.routers import core
    from apps.api.schemas import AskRequest, SearchRequest

    payload = {
        "files": [],
        "symbols": [],
        "chunks": [],
        "vector_status": VectorSearchStatus.OK,
        "repository_scope": {
            "found": True,
            "repository_path": "/app",
        },
    }
    hybrid = AsyncMock(return_value=payload)
    generate = AsyncMock(return_value="answer")
    monkeypatch.setattr(core, "hybrid_search_code", hybrid)
    monkeypatch.setattr(core.RuleStore, "list_active_rules", AsyncMock(return_value=[]))
    monkeypatch.setattr(core.DecisionStore, "list_decisions", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        core,
        "get_model_router",
        lambda: types.SimpleNamespace(
            llm=lambda _kind: types.SimpleNamespace(generate=generate)
        ),
    )
    request = types.SimpleNamespace(headers={"x-request-id": "request-42"})

    assert await core.ask_project(
        AskRequest(query="parser", repo_path="/app"),
        request,
    ) == {"answer": "answer", "learnings_used": []}
    assert await core.search_code_endpoint(
        SearchRequest(query="parser", repo_path="/app", limit=3),
        request,
    ) == payload
    assert hybrid.await_args_list[0].kwargs == {
        "limit": core.ASK_RETRIEVAL_LIMIT,
        "repo_path": "/app",
        "request_id": "request-42",
    }
    assert hybrid.await_args_list[0].args == ("parser",)
    assert hybrid.await_args_list[1].args == ("parser",)
    assert hybrid.await_args_list[1].kwargs == {
        "limit": 3,
        "repo_path": "/app",
        "request_id": "request-42",
    }


@pytest.mark.asyncio
async def test_local_mcp_ask_and_search_share_integrated_code_search(monkeypatch):
    from apps.mcp_server import server

    payload = {
        "files": [],
        "symbols": [],
        "chunks": [],
        "vector_status": VectorSearchStatus.OK,
        "repository_scope": {
            "found": True,
            "repository_path": "/app",
        },
    }
    hybrid = AsyncMock(return_value=payload)
    generate = AsyncMock(return_value="answer")
    monkeypatch.setattr(server, "hybrid_search_code", hybrid)
    monkeypatch.setattr(server.RuleStore, "list_active_rules", AsyncMock(return_value=[]))
    monkeypatch.setattr(server.DecisionStore, "list_decisions", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        server,
        "get_model_router",
        lambda: types.SimpleNamespace(
            llm=lambda _kind: types.SimpleNamespace(generate=generate)
        ),
    )

    assert await server.ask_project("parser", repo_path="/app") == "answer"
    assert await server.search_code("parser", repo_path="/app") == payload
    assert hybrid.await_args_list[0].args == ("parser",)
    assert hybrid.await_args_list[0].kwargs == {
        "limit": server.ASK_RETRIEVAL_LIMIT,
        "repo_path": "/app",
    }
    assert hybrid.await_args_list[1].args == ("parser",)
    assert hybrid.await_args_list[1].kwargs == {"limit": 5, "repo_path": "/app"}
