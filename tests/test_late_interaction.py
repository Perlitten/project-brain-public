from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
from unittest.mock import AsyncMock

import httpx
import numpy as np
import pytest
from sqlalchemy.dialects import postgresql

from brain.config.settings import Settings, settings
from brain.database.models import LateInteractionEmbedding
from brain.late_interaction.codec import (
    LateInteractionCodecError,
    decode_matrix,
    encode_matrix,
    maxsim_score,
)
from brain.late_interaction.provider import LfmColbertProvider
from brain.late_interaction.store import (
    LateInteractionInventory,
    rerank_candidate_paths,
    upsert_late_interaction_embedding,
)
from brain.indexers.file_indexer import FileIndexer
from brain.retrieval.pipeline import (
    _apply_late_interaction_scores,
    _await_late_rerank,
    _late_canary_selected,
)
from brain.retrieval.types import ChannelCandidate
from scripts.validate_lfm_release_gate import release_binding_digest


def test_late_interaction_defaults_cannot_change_production_ranking():
    assert settings.LATE_INTERACTION_ENABLED is False
    assert settings.LATE_INTERACTION_DUAL_WRITE_ENABLED is False
    assert settings.LATE_INTERACTION_SHADOW_ENABLED is False
    assert settings.LATE_INTERACTION_SHADOW_PERSIST_ENABLED is True
    assert settings.LATE_INTERACTION_RERANK_ENABLED is False
    assert settings.LATE_INTERACTION_CANARY_PERCENT == 0
    assert settings.LATE_INTERACTION_EXPERIMENT_AUTHORIZED is False
    assert settings.LATE_INTERACTION_PRODUCTION_GATES_PASSED is False


def test_blank_optional_lfm_approval_integers_are_none_while_lfm_is_off():
    configured = Settings(
        _env_file=None,
        LATE_INTERACTION_APPROVED_REPOSITORY_ID="  ",
        LATE_INTERACTION_APPROVED_DOCUMENT_COUNT="",
    )
    assert configured.LATE_INTERACTION_APPROVED_REPOSITORY_ID is None
    assert configured.LATE_INTERACTION_APPROVED_DOCUMENT_COUNT is None


def _late_interaction_approval_binding() -> dict[str, object]:
    return {
        "BRAIN_BUILD_SHA": "a" * 40,
        "BRAIN_SOURCE_DIGEST": "d" * 64,
        "LATE_INTERACTION_APPROVED_REPOSITORY_ID": 7,
        "LATE_INTERACTION_APPROVED_BUILD_SHA": "a" * 40,
        "LATE_INTERACTION_APPROVED_SOURCE_DIGEST": "d" * 64,
        "LATE_INTERACTION_APPROVED_MODEL_REVISION": (
            "bc240003aba07253e261a8aaf0d2c9683318a967"
        ),
        "LATE_INTERACTION_REMOTE_EXPECTED_INDEX_REVISION": "r1294",
        "LATE_INTERACTION_APPROVED_INDEX_REVISION": "r1294",
        "LATE_INTERACTION_APPROVED_LINEAGE_ID": "lineage-r1",
        "LATE_INTERACTION_APPROVED_IDENTITY_DIGEST": "e" * 64,
        "LATE_INTERACTION_APPROVED_DOCUMENT_COUNT": 1294,
    }


def _write_runtime_release_approval(tmp_path) -> tuple[object, str]:
    binding = {
        "repository_id": 7,
        "build_sha": "a" * 40,
        "source_digest": "d" * 64,
        "model_revision": "bc240003aba07253e261a8aaf0d2c9683318a967",
        "index_revision": "r1294",
        "lineage_id": "lineage-r1",
        "identity_digest": "e" * 64,
        "document_count": 1294,
    }
    summaries = {
        "runtime_latency_slo": {
            "p95_end_to_end_increase_ms": 400.0,
            "provider_failure_rate": 0.0005,
            "fail_open_verified": True,
            "sample_count": 500,
        },
        "runtime_parity": {
            "sample_count": 10,
            "mean_top_k_overlap": 0.95,
            "mean_spearman": 0.97,
            "mean_kendall": 0.93,
            "max_abs_ndcg_delta": 0.01,
        },
        "production_shadow": {
            "production_real": True,
            "window_days": 7.0,
            "successfully_recorded_eligible_queries": 500,
            "scored_count": 500,
            "skipped_count": 0,
            "error_count": 0,
        },
    }
    artifacts = {
        "quality": {
            "schema_version": 2,
            "release_binding": binding,
            "overall_gate": {"passed": True},
            "evaluation_provenance": {
                "policy": "lfm-production-v1",
                "production_gate": True,
                "fixed_thresholds": True,
                "thresholds": {
                    "median_ndcg_delta_min": 0.03,
                    "improved_ratio_min": 0.60,
                    "hit3_regression_max": 0.10,
                    "slice_regression_max": 0.05,
                },
            },
        },
        "runtime_latency_slo": {
            "schema": "project-brain.lfm-runtime-latency",
            "schema_version": 1,
            "passed": True,
            "release_binding": binding,
            "summary": summaries["runtime_latency_slo"],
        },
        "runtime_parity": {
            "schema_version": 1,
            "release_binding": binding,
            "gate": {
                "passed": True,
                "thresholds": {
                    "overlap_min": 0.90,
                    "spearman_min": 0.95,
                    "kendall_min": 0.90,
                    "ndcg_abs_delta_max": 0.02,
                },
            },
            "summary": {
                "count": 10,
                **{
                    key: value
                    for key, value in summaries["runtime_parity"].items()
                    if key != "sample_count"
                },
            },
        },
        "production_shadow": {
            "schema": "project-brain.lfm-production-shadow",
            "schema_version": 1,
            "passed": True,
            "release_binding": binding,
            "summary": summaries["production_shadow"],
        },
    }
    binding_hash = release_binding_digest(binding)
    gates = {}
    for name, artifact in artifacts.items():
        artifact_path = tmp_path / f"{name}.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True), encoding="utf-8")
        gates[name] = {
            "schema_version": 1,
            "passed": True,
            "binding_sha256": binding_hash,
            "evidence_path": artifact_path.name,
            "evidence_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        }
    gates["quality"].update(
        {
            "report_schema_version": 2,
            "overall_gate_passed": True,
            "production_gate": True,
            "fixed_thresholds": True,
            "policy": "lfm-production-v1",
            "thresholds": {
                "median_ndcg_delta_min": 0.03,
                "improved_ratio_min": 0.60,
                "hit3_regression_max": 0.10,
                "slice_regression_max": 0.05,
            },
        }
    )
    for name, summary in summaries.items():
        gates[name].update(summary)
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(
        json.dumps(
            {
                "schema": "project-brain.lfm-release-approval",
                "schema_version": 1,
                "passed": True,
                "release_binding": binding,
                "gates": gates,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return approval_path, hashlib.sha256(approval_path.read_bytes()).hexdigest()


def test_late_interaction_release_authorization_is_enforced_at_runtime(tmp_path):
    evidence_path, evidence_hash = _write_runtime_release_approval(tmp_path)

    with pytest.raises(ValueError, match="EXPERIMENT_AUTHORIZED"):
        Settings(_env_file=None, LATE_INTERACTION_ENABLED="1")

    with pytest.raises(ValueError, match="immutable build"):
        Settings(
            _env_file=None,
            LATE_INTERACTION_ENABLED=True,
            LATE_INTERACTION_EXPERIMENT_AUTHORIZED=True,
            **(_late_interaction_approval_binding() | {
                "LATE_INTERACTION_APPROVED_BUILD_SHA": "b" * 40,
            }),
        )

    with pytest.raises(ValueError, match="source digest"):
        Settings(
            _env_file=None,
            LATE_INTERACTION_ENABLED=True,
            LATE_INTERACTION_EXPERIMENT_AUTHORIZED=True,
            **(
                _late_interaction_approval_binding()
                | {"LATE_INTERACTION_APPROVED_SOURCE_DIGEST": "e" * 64}
            ),
        )

    experiment = Settings(
        _env_file=None,
        LATE_INTERACTION_ENABLED=True,
        LATE_INTERACTION_SHADOW_ENABLED=True,
        LATE_INTERACTION_EXPERIMENT_AUTHORIZED=True,
        **_late_interaction_approval_binding(),
    )
    assert experiment.LATE_INTERACTION_SHADOW_ENABLED is True

    with pytest.raises(ValueError, match="PRODUCTION_GATES_PASSED"):
        Settings(
            _env_file=None,
            LATE_INTERACTION_ENABLED=True,
            LATE_INTERACTION_RERANK_ENABLED=True,
            LATE_INTERACTION_EXPERIMENT_AUTHORIZED=True,
            **_late_interaction_approval_binding(),
        )

    with pytest.raises(ValueError, match="evidence bundle"):
        Settings(
            _env_file=None,
            LATE_INTERACTION_ENABLED=True,
            LATE_INTERACTION_RERANK_ENABLED=True,
            LATE_INTERACTION_EXPERIMENT_AUTHORIZED=True,
            LATE_INTERACTION_PRODUCTION_GATES_PASSED=True,
            LATE_INTERACTION_PRODUCTION_EVIDENCE_SHA256="not-a-digest",
            **_late_interaction_approval_binding(),
        )

    production = Settings(
        _env_file=None,
        REPORT_OUTPUT_DIR=str(tmp_path),
        LATE_INTERACTION_ENABLED=True,
        LATE_INTERACTION_RERANK_ENABLED=True,
        LATE_INTERACTION_EXPERIMENT_AUTHORIZED=True,
        LATE_INTERACTION_PRODUCTION_GATES_PASSED=True,
        LATE_INTERACTION_PRODUCTION_EVIDENCE_PATH=evidence_path.name,
        LATE_INTERACTION_PRODUCTION_EVIDENCE_SHA256=evidence_hash,
        **_late_interaction_approval_binding(),
    )
    assert production.LATE_INTERACTION_RERANK_ENABLED is True


def test_runtime_gate_rejects_symlinked_evidence(tmp_path):
    target = tmp_path / "real.json"
    target.write_text('{"approved":true}\n', encoding="utf-8")
    link = tmp_path / "approval.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError, match="symlink"):
        Settings(
            _env_file=None,
            REPORT_OUTPUT_DIR=str(tmp_path),
            LATE_INTERACTION_ENABLED=True,
            LATE_INTERACTION_RERANK_ENABLED=True,
            LATE_INTERACTION_EXPERIMENT_AUTHORIZED=True,
            LATE_INTERACTION_PRODUCTION_GATES_PASSED=True,
            LATE_INTERACTION_PRODUCTION_EVIDENCE_PATH=link.name,
            LATE_INTERACTION_PRODUCTION_EVIDENCE_SHA256=hashlib.sha256(
                target.read_bytes()
            ).hexdigest(),
            **_late_interaction_approval_binding(),
        )


def test_production_lfm_uses_baked_manifest_not_spoofable_build_env(
    monkeypatch,
):
    settings_module = importlib.import_module("brain.config.settings")
    monkeypatch.setattr(
        settings_module,
        "read_baked_source_manifest",
        lambda: {
            "source_revision": "immutable-release",
            "content_digest": "e" * 64,
        },
    )
    binding = _late_interaction_approval_binding() | {
        "BRAIN_BUILD_SHA": "spoofed-release",
        "BRAIN_SOURCE_DIGEST": "d" * 64,
        "LATE_INTERACTION_APPROVED_BUILD_SHA": "spoofed-release",
        "LATE_INTERACTION_APPROVED_SOURCE_DIGEST": "e" * 64,
        "LATE_INTERACTION_APPROVED_MODEL_REVISION": (
            "59633c2e31717b3502343ff566bee9fda3261943"
        ),
    }
    with pytest.raises(ValueError, match="immutable build"):
        Settings(
            _env_file=None,
            ENVIRONMENT="production",
            POSTGRES_PASSWORD="safe-postgres-secret",
            NEO4J_PASSWORD="safe-neo4j-secret",
            LATE_INTERACTION_ENABLED=True,
            LATE_INTERACTION_EXPERIMENT_AUTHORIZED=True,
            LATE_INTERACTION_REMOTE_ENABLED=True,
            LATE_INTERACTION_REMOTE_TOKEN="secret",
            **binding,
        )


def test_late_interaction_settings_reject_unsafe_ranges():
    with pytest.raises(ValueError, match="CANARY_PERCENT"):
        Settings(_env_file=None, LATE_INTERACTION_CANARY_PERCENT=101)
    with pytest.raises(ValueError, match="ratios"):
        Settings(_env_file=None, LATE_INTERACTION_MIN_CANDIDATE_COVERAGE=1.1)
    with pytest.raises(ValueError, match="float16"):
        Settings(_env_file=None, LATE_INTERACTION_STORAGE_DTYPE="json")
    with pytest.raises(ValueError, match="timeouts"):
        Settings(_env_file=None, LATE_INTERACTION_TIMEOUT_S=-1)
    with pytest.raises(ValueError, match="ALLOW_REMOTE"):
        Settings(_env_file=None, LATE_INTERACTION_PROVIDER_URL="http://example.net:8080")
    remote = Settings(
        _env_file=None,
        LATE_INTERACTION_PROVIDER_URL="http://lfm-colbert-canary:8080/",
        LATE_INTERACTION_ALLOW_REMOTE_PROVIDER=True,
    )
    assert remote.LATE_INTERACTION_PROVIDER_URL == "http://lfm-colbert-canary:8080"


def test_late_interaction_model_is_additive_and_cascades_with_chunk():
    table = LateInteractionEmbedding.__table__
    assert table.name == "late_interaction_embeddings"
    assert table.c.vector_data.type.__class__.__name__ == "LargeBinary"
    foreign_keys = {str(fk.column): fk.ondelete for fk in table.foreign_keys}
    assert foreign_keys["file_chunks.id"] == "CASCADE"
    assert foreign_keys["repositories.id"] == "CASCADE"
    assert {constraint.name for constraint in table.constraints} >= {
        "ck_late_embedding_dimension",
        "ck_late_embedding_token_count",
    }
    assert table.c.vector_dtype.server_default is not None
    assert table.c.was_truncated.server_default is not None


def test_empty_late_interaction_inventory_is_not_complete():
    inventory = LateInteractionInventory(
        repository_id=1,
        model="model",
        model_revision="revision",
    )
    assert inventory.coverage_pct == 0.0


def test_float16_codec_round_trip_and_maxsim():
    source = np.asarray([[1.0, 0.0], [0.0, 0.5]], dtype=np.float32)
    payload = encode_matrix(source)
    assert len(payload) == 2 * 2 * 2
    decoded = decode_matrix(payload, token_count=2, dimension=2)
    assert np.allclose(decoded, source, atol=1e-3)
    query = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    assert maxsim_score(query, decoded) == pytest.approx(1.5, abs=1e-3)


def test_codec_rejects_corrupt_payload():
    with pytest.raises(LateInteractionCodecError, match="expected"):
        decode_matrix(b"\x00", token_count=2, dimension=128)


@pytest.mark.asyncio
async def test_llama_provider_preprocesses_normalizes_and_filters_documents():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else {}
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/tokenize":
            content = body["content"]
            if content == "<|im_end|>":
                tokens = [2]
            elif len(content) == 1 and content in "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~":
                tokens = [100]
            elif content.startswith("[Q] "):
                tokens = [10, 11]
            else:
                tokens = [10, 100, 12]
            return httpx.Response(200, json={"tokens": tokens})
        if request.url.path == "/embedding":
            tokens = body["content"]
            matrix = []
            for token in tokens:
                row = [0.0] * 128
                row[token % 128] = 2.0
                matrix.append(row)
            return httpx.Response(200, json=[{"embedding": matrix}])
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = LfmColbertProvider(base_url="http://sidecar", client=client)
        health = await provider.health()
        query = await provider.embed("find the parser", is_query=True)
        document = await provider.embed("parser.py", is_query=False)

    assert health["status"] == "healthy"
    assert query.vectors.shape == (32, 128)
    assert document.vectors.shape == (2, 128)
    assert np.allclose(np.linalg.norm(query.vectors, axis=1), 1.0)
    assert np.allclose(np.linalg.norm(document.vectors, axis=1), 1.0)


@pytest.mark.asyncio
async def test_special_token_discovery_is_cached_across_provider_instances():
    tokenized: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if request.url.path == "/tokenize":
            content = body["content"]
            tokenized.append(content)
            if content == "<|im_end|>":
                return httpx.Response(200, json={"tokens": [2]})
            if len(content) == 1:
                return httpx.Response(200, json={"tokens": [100]})
            return httpx.Response(200, json={"tokens": [10, 11]})
        if request.url.path == "/embedding":
            matrix = [[1.0] + [0.0] * 127 for _ in body["content"]]
            return httpx.Response(200, json=[{"embedding": matrix}])
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = LfmColbertProvider(base_url="http://cache-test", client=client)
        second = LfmColbertProvider(base_url="http://cache-test", client=client)
        await first.embed("first", is_query=True)
        after_first = len(tokenized)
        await second.embed("second", is_query=True)

    assert len(tokenized) - after_first == 1


@pytest.mark.asyncio
async def test_upsert_uses_atomic_postgres_conflict_update():
    session = AsyncMock()
    provider = LfmColbertProvider(base_url="http://sidecar")
    encoded = type(
        "Encoded",
        (),
        {
            "vectors": np.ones((2, 128), dtype=np.float32),
            "token_count": 2,
            "truncated": True,
        },
    )()
    await upsert_late_interaction_embedding(
        session,
        repository_id=1,
        chunk_id=2,
        source_text="content",
        encoded=encoded,
        provider=provider,
    )
    statement = session.execute.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (chunk_id, model, model_revision) DO UPDATE" in sql
    assert "was_truncated" in sql


@pytest.mark.asyncio
async def test_rerank_failure_is_fail_open(monkeypatch):
    async def fake_coverage(_repository_id, _paths):
        return {"a.py": 1}, 1, 1

    class FailingProvider:
        async def embed(self, _text, *, is_query):
            raise RuntimeError("sidecar unavailable")

    monkeypatch.setattr("brain.late_interaction.store._candidate_coverage", fake_coverage)
    result = await rerank_candidate_paths(
        "query",
        repository_id=1,
        paths=["a.py"],
        provider=FailingProvider(),
    )
    assert result.status == "failed_open"
    assert result.scores == {}
    assert result.error == "sidecar unavailable"


@pytest.mark.asyncio
async def test_rerank_coverage_counts_unresolvable_candidates(monkeypatch):
    async def fake_coverage(_repository_id, _paths):
        return {}, 0, 0

    monkeypatch.setattr("brain.late_interaction.store._candidate_coverage", fake_coverage)
    result = await rerank_candidate_paths(
        "query",
        repository_id=1,
        paths=["missing.py", "also-missing.py"],
    )
    assert result.status == "insufficient_coverage"
    assert result.candidate_count == 2
    assert result.resolvable_candidates == 0
    assert result.coverage_ratio == 0.0


@pytest.mark.asyncio
async def test_rerank_refuses_candidate_set_over_chunk_budget(monkeypatch):
    async def fake_coverage(_repository_id, _paths):
        return {"large.py": 501}, 1, 501

    provider = type("Provider", (), {"embed": AsyncMock()})()
    monkeypatch.setattr("brain.late_interaction.store._candidate_coverage", fake_coverage)
    monkeypatch.setattr(settings, "LATE_INTERACTION_MAX_RERANK_CHUNKS", 500)
    result = await rerank_candidate_paths(
        "query",
        repository_id=1,
        paths=["large.py"],
        provider=provider,
    )

    assert result.status == "chunk_budget_exceeded"
    assert result.candidate_chunks == 501
    assert result.chunk_budget == 500
    provider.embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_dual_write_circuit_opens_and_closes_provider(monkeypatch):
    provider = type("Provider", (), {"aclose": AsyncMock()})()
    indexer = object.__new__(FileIndexer)
    indexer._late_provider = provider
    indexer._late_dual_write_failures = 0
    indexer._late_dual_write_circuit_open = False
    monkeypatch.setattr(
        settings,
        "LATE_INTERACTION_DUAL_WRITE_MAX_CONSECUTIVE_FAILURES",
        2,
    )

    await indexer._record_late_dual_write_failure()
    assert indexer._late_dual_write_circuit_open is False
    await indexer._record_late_dual_write_failure()

    assert indexer._late_dual_write_circuit_open is True
    assert indexer._late_provider is None
    provider.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_late_rerank_timeout_is_caught_on_python_310(monkeypatch):
    async def slow_result():
        await asyncio.sleep(0.05)
        return "late"

    monkeypatch.setattr(settings, "LATE_INTERACTION_RERANK_TIMEOUT_S", 0.001)
    result, timed_out = await _await_late_rerank(slow_result())

    assert result is None
    assert timed_out is True


def test_canary_sampling_respects_zero_and_hundred_percent(monkeypatch):
    monkeypatch.setattr(settings, "LATE_INTERACTION_RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_CANARY_PERCENT", 0.0)
    assert not _late_canary_selected(1, "query")
    monkeypatch.setattr(settings, "LATE_INTERACTION_CANARY_PERCENT", 100.0)
    assert _late_canary_selected(1, "query")


def test_late_scores_apply_bounded_rank_bonus(monkeypatch):
    monkeypatch.setattr(settings, "LATE_INTERACTION_SCORE_WEIGHT", 0.25)
    candidates = [
        ChannelCandidate("fused", "a.py", 0.1, reranker_score=0.1),
        ChannelCandidate("fused", "b.py", 0.1, reranker_score=0.1),
        ChannelCandidate("fused", "c.py", 0.1, reranker_score=0.1),
    ]
    applied = _apply_late_interaction_scores(candidates, {"a.py": 4.0, "b.py": 8.0, "c.py": 2.0})
    assert applied == 3
    assert [candidate.item_id for candidate in candidates] == ["b.py", "a.py", "c.py"]
    assert candidates[0].reranker_score == pytest.approx(0.35)
    assert candidates[0].metadata["late_interaction_rank"] == 1
