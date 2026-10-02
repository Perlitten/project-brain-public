from __future__ import annotations

import hashlib
import importlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence
from unittest.mock import AsyncMock

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from brain.config.settings import settings
from brain.indexers.file_indexer import FileIndexer
from brain.late_interaction.client import (
    LateInteractionCandidate,
    LateInteractionDocument,
    LateInteractionIndexEntry,
    LateInteractionIndexPage,
    LateInteractionMutationResult,
    LateInteractionReleaseUnavailableError,
    RemoteLateInteractionClient,
    validate_remote_late_interaction_release,
)
from brain.late_interaction.gpu_service import (
    SQLiteMatrixStore,
    ServiceConfig,
    StoredMatrix,
    create_app,
)
from brain.late_interaction.metrics import reset_for_tests, snapshot
from brain.late_interaction.pylate_encoder import PyLateEncoder
from brain.late_interaction import remote_sync


MODEL_REVISION = "59633c2e31717b3502343ff566bee9fda3261943"
TOKEN = "test-shared-secret"
ROOT = Path(__file__).resolve().parents[1]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_gpu_deployment_is_pinned_private_and_default_off():
    dockerfile = (ROOT / "deploy" / "lfm-colbert-gpu" / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "deploy" / "lfm-colbert-gpu" / "docker-compose.yml").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert (
        "pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime"
        "@sha256:417bd75df6365104c283ea4c1651fb3530d9eb5a4c2fafa51943cff2a94e6385" in dockerfile
    )
    assert "pylate==1.4.0" in pyproject
    assert "${LFM_SERVICE_BIND_ADDRESS:-127.0.0.1}" in compose
    assert "LATE_INTERACTION_SERVICE_TOKEN: ${LATE_INTERACTION_SERVICE_TOKEN:?" in compose
    assert "LFM_LICENSE_ACKNOWLEDGED: ${LFM_LICENSE_ACKNOWLEDGED:?" in compose
    assert "gpus: all" in compose
    assert 'io.project-brain.cutover: "false"' in compose


class FakeEncoder:
    model_revision = MODEL_REVISION
    dimension = 2

    async def encode_queries(self, texts: Sequence[str]) -> list[np.ndarray]:
        return [self._matrix(text) for text in texts]

    async def encode_documents(self, texts: Sequence[str]) -> list[np.ndarray]:
        return [self._matrix(text) for text in texts]

    @staticmethod
    def _matrix(text: str) -> np.ndarray:
        if "alpha" in text:
            return np.asarray([[1.0, 0.0], [1.0, 0.0]], dtype=np.float32)
        return np.asarray([[0.0, 1.0]], dtype=np.float32)


def _service(tmp_path, *, max_candidates: int = 500) -> TestClient:
    app = create_app(
        ServiceConfig(
            database_path=str(tmp_path / "late.sqlite3"),
            token=TOKEN,
            environment="test",
            model_revision=MODEL_REVISION,
            dimension=2,
            max_candidates=max_candidates,
        ),
        encoder=FakeEncoder(),
    )
    return TestClient(app)


def _headers(token: str = TOKEN) -> dict[str, str]:
    return {"X-Late-Interaction-Token": token}


@pytest.fixture
def isolated_late_metrics():
    reset_for_tests()
    yield
    reset_for_tests()


def _upsert_body() -> dict:
    return {
        "repository_id": 7,
        "model_revision": MODEL_REVISION,
        "documents": [
            {
                "chunk_id": 10,
                "path": "alpha.py",
                "content_hash": _hash("alpha document"),
                "text": "alpha document",
            },
            {
                "chunk_id": 11,
                "path": "beta.py",
                "content_hash": _hash("beta document"),
                "text": "beta document",
            },
        ],
    }


def test_service_requires_auth_but_keeps_health_public(tmp_path):
    with _service(tmp_path) as client:
        health = client.get("/health")
        ready_missing = client.get("/ready")
        ready = client.get("/ready", headers=_headers())
        missing = client.get(
            "/v1/index/status",
            params={"repository_id": 7, "model_revision": MODEL_REVISION},
        )
        wrong = client.get(
            "/v1/index/status",
            params={"repository_id": 7, "model_revision": MODEL_REVISION},
            headers=_headers("wrong"),
        )

    assert health.status_code == 200
    assert health.json()["index_revision"] == "r0"
    assert ready_missing.status_code == 401
    assert ready.status_code == 200
    assert missing.status_code == 401
    assert wrong.status_code == 401


def test_upsert_is_hash_idempotent_and_inventory_is_revisioned(tmp_path):
    body = _upsert_body()
    with _service(tmp_path) as client:
        first = client.post("/v1/index/upsert", headers=_headers(), json=body)
        second = client.post("/v1/index/upsert", headers=_headers(), json=body)
        client.post(
            "/v1/index/finalize",
            headers=_headers(),
            json={"repository_id": 7, "model_revision": MODEL_REVISION},
        )
        inventory = client.get(
            "/v1/index/status",
            headers=_headers(),
            params={"repository_id": 7, "model_revision": MODEL_REVISION},
        )

    assert first.status_code == 200
    assert first.json()["status"] == "updated"
    assert first.json()["accepted"] == 2
    assert first.json()["index_revision"] == "r1"
    assert second.json()["status"] == "unchanged"
    assert second.json()["unchanged"] == 2
    assert second.json()["index_revision"] == "r1"
    assert inventory.json()["document_count"] == 2
    assert inventory.json()["bytes"] > 0
    assert inventory.json()["index_revision"] == "r1"


def test_upsert_updates_path_when_content_hash_is_unchanged(tmp_path):
    body = _upsert_body()
    renamed = json.loads(json.dumps(body))
    renamed["documents"][0]["path"] = "renamed/alpha.py"

    with _service(tmp_path) as client:
        first = client.post("/v1/index/upsert", headers=_headers(), json=body)
        second = client.post("/v1/index/upsert", headers=_headers(), json=renamed)
        listed = client.get(
            "/v1/index/documents",
            headers=_headers(),
            params={"repository_id": 7, "model_revision": MODEL_REVISION},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "updated"
    assert second.json()["accepted"] == 1
    assert second.json()["unchanged"] == 1
    entries = {item["chunk_id"]: item for item in listed.json()["documents"]}
    assert entries[10]["path"] == "renamed/alpha.py"


def test_rerank_scores_current_hashes_and_fails_open_on_revision_mismatch(tmp_path):
    body = _upsert_body()
    with _service(tmp_path) as client:
        client.post("/v1/index/upsert", headers=_headers(), json=body)
        client.post(
            "/v1/index/finalize",
            headers=_headers(),
            json={"repository_id": 7, "model_revision": MODEL_REVISION},
        )
        reranked = client.post(
            "/v1/rerank",
            headers=_headers(),
            json={
                "query": "alpha",
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "index_revision": "r1",
                "candidates": [
                    {
                        "chunk_id": document["chunk_id"],
                        "path": document["path"],
                        "content_hash": document["content_hash"],
                    }
                    for document in body["documents"]
                ],
            },
        )
        mismatch = client.post(
            "/v1/rerank",
            headers=_headers(),
            json={
                "query": "alpha",
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "index_revision": "r0",
                "candidates": [],
            },
        )

    assert reranked.json()["status"] == "scored"
    assert reranked.json()["coverage"] == 1.0
    assert [score["chunk_id"] for score in reranked.json()["scores"]] == [10, 11]
    assert mismatch.json()["status"] == "revision_mismatch"
    assert mismatch.json()["scores"] == []
    assert mismatch.json()["reason"] == "index_revision_mismatch"


def test_minimum_revision_floor_gates_rerank_but_not_mutation(tmp_path):
    body = _upsert_body()
    with _service(tmp_path) as client:
        client.post("/v1/index/upsert", headers=_headers(), json=body)
        client.post(
            "/v1/index/finalize",
            headers=_headers(),
            json={"repository_id": 7, "model_revision": MODEL_REVISION},
        )
        allowed = client.post(
            "/v1/rerank",
            headers=_headers(),
            json={
                "query": "alpha",
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "minimum_index_revision": "r0",
                "candidates": [],
            },
        )
        mutation = client.post(
            "/v1/index/upsert",
            headers=_headers(),
            json={
                **body,
                "minimum_index_revision": "r2",
            },
        )
        blocked = client.post(
            "/v1/rerank",
            headers=_headers(),
            json={
                "query": "alpha",
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "minimum_index_revision": "r2",
                "candidates": [],
            },
        )

    assert allowed.json()["status"] == "insufficient_coverage"
    assert allowed.json()["index_revision"] == "r1"
    assert mutation.json()["status"] == "unchanged"
    assert mutation.json()["index_revision"] == "r1"
    assert blocked.json()["status"] == "revision_mismatch"
    assert blocked.json()["reason"] == "index_revision_below_approved_floor"


@pytest.mark.asyncio
async def test_live_dual_write_revision_floor_survives_multiple_mutations():
    revision = 1294
    seen_preconditions: list[tuple[object, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal revision
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "status": "ready",
                    "repository_id": 7,
                    "model_revision": MODEL_REVISION,
                    "index_revision": f"r{revision}",
                    "document_count": 1294,
                    "bytes": 1,
                },
            )
        payload = json.loads(request.content)
        seen_preconditions.append(
            (
                payload.get("index_revision"),
                payload.get("minimum_index_revision"),
            )
        )
        if request.url.path == "/v1/index/upsert":
            revision += 1
            return httpx.Response(
                200,
                json={
                    "status": "updated",
                    "accepted": 1,
                    "model_revision": MODEL_REVISION,
                    "index_revision": f"r{revision}",
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "scored",
                "scores": [],
                "coverage": 1.0,
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "index_revision": f"r{revision}",
                "lineage_id": "test-lineage",
                "identity_digest": _hash("inventory"),
                "document_count": revision,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = RemoteLateInteractionClient(
            base_url="http://gpu-service",
            token=TOKEN,
            model_revision=MODEL_REVISION,
            expected_index_revision="r1294",
            allow_index_revision_advance=True,
            approved_repository_id=7,
            client=http_client,
        )
        for chunk_id in (20, 21):
            result = await client.upsert_documents(
                7,
                [
                    LateInteractionDocument(
                        chunk_id=chunk_id,
                        path=f"{chunk_id}.py",
                        content_hash=_hash(str(chunk_id)),
                        text=str(chunk_id),
                    )
                ],
            )
            assert result.status == "updated"
        reranked = await client.rerank("query", 7, [])
        status = await client.status(7)
        rejected_repository = await client.rerank("query", 8, [])

    assert reranked.status == "scored"
    assert status.status == "ready"
    assert status.index_revision == "r1296"
    assert rejected_repository.reason == "repository_not_approved"
    assert seen_preconditions == [(None, "r1294")] * 3


@pytest.mark.asyncio
async def test_live_dual_write_can_rebuild_below_readiness_floor():
    seen_preconditions: list[tuple[object, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen_preconditions.append(
            (
                payload.get("index_revision"),
                payload.get("minimum_index_revision"),
            )
        )
        return httpx.Response(
            200,
            json={
                "status": "updated",
                "accepted": 1,
                "model_revision": MODEL_REVISION,
                # Recovery starts below the approved read floor. The write is
                # still valid; status/rerank remain fail-closed until r1294.
                "index_revision": "r1",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = RemoteLateInteractionClient(
            base_url="http://gpu-service",
            token=TOKEN,
            model_revision=MODEL_REVISION,
            expected_index_revision="r1294",
            allow_index_revision_advance=True,
            approved_repository_id=7,
            client=http_client,
        )
        result = await client.upsert_documents(
            7,
            [
                LateInteractionDocument(
                    chunk_id=20,
                    path="20.py",
                    content_hash=_hash("20"),
                    text="20",
                )
            ],
        )

    assert result.status == "updated"
    assert result.index_revision == "r1"
    assert seen_preconditions == [(None, "r1294")]


@pytest.mark.asyncio
async def test_inventory_recovery_reads_r0_below_serving_floor():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "ready",
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "index_revision": "r0",
                "documents": [],
                "next_after_chunk_id": None,
                "reason": "",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = RemoteLateInteractionClient(
            base_url="http://gpu-service",
            token=TOKEN,
            model_revision=MODEL_REVISION,
            expected_index_revision="r1294",
            allow_index_revision_advance=True,
            approved_repository_id=7,
            client=http_client,
        )
        page = await client.list_documents(7)

    assert page.status == "ready"
    assert page.index_revision == "r0"
    assert page.entries == ()


@pytest.mark.asyncio
async def test_live_revision_floor_rejects_remote_store_rollback():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "ready",
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "index_revision": "r1000",
                "document_count": 1000,
                "bytes": 1,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = RemoteLateInteractionClient(
            base_url="http://gpu-service",
            token=TOKEN,
            model_revision=MODEL_REVISION,
            expected_index_revision="r1294",
            allow_index_revision_advance=True,
            approved_repository_id=7,
            client=http_client,
        )
        status = await client.status(7)

    assert status.status == "revision_mismatch"
    assert status.reason == "index_revision_mismatch"


@pytest.mark.asyncio
async def test_active_release_startup_fails_closed_on_remote_revision_mismatch(
    monkeypatch,
):
    client_module = importlib.import_module("brain.late_interaction.client")
    remote = SimpleNamespace(
        status=AsyncMock(
            return_value=SimpleNamespace(
                status="revision_mismatch",
                reason="index_revision_mismatch",
            )
        )
    )
    monkeypatch.setattr(settings, "LATE_INTERACTION_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_REMOTE_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_APPROVED_REPOSITORY_ID", 7)
    monkeypatch.setattr(client_module, "get_late_interaction_client", lambda: remote)

    with pytest.raises(RuntimeError, match="index_revision_mismatch"):
        await validate_remote_late_interaction_release()
    remote.status.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_active_release_startup_retries_transient_remote_outage(
    monkeypatch,
):
    client_module = importlib.import_module("brain.late_interaction.client")
    ready = SimpleNamespace(
        status="ready",
        repository_id=7,
        index_revision="r12",
        reason="",
    )
    remote = SimpleNamespace(
        status=AsyncMock(
            side_effect=[
                SimpleNamespace(status="failed_open", reason="timeout"),
                SimpleNamespace(status="failed_open", reason="ConnectError"),
                ready,
            ]
        )
    )
    monkeypatch.setattr(settings, "LATE_INTERACTION_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_REMOTE_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_APPROVED_REPOSITORY_ID", 7)
    monkeypatch.setattr(client_module, "get_late_interaction_client", lambda: remote)

    result = await validate_remote_late_interaction_release(
        max_attempts=3,
        initial_backoff_s=0,
    )

    assert result is ready
    assert remote.status.await_count == 3


@pytest.mark.asyncio
async def test_active_release_startup_retries_identity_refresh_pending(
    monkeypatch,
):
    client_module = importlib.import_module("brain.late_interaction.client")
    ready = SimpleNamespace(
        status="ready",
        repository_id=7,
        index_revision="r12",
        reason="",
    )
    remote = SimpleNamespace(
        status=AsyncMock(
            side_effect=[
                SimpleNamespace(
                    status="not_ready",
                    reason="identity_refresh_pending",
                ),
                ready,
            ]
        )
    )
    monkeypatch.setattr(settings, "LATE_INTERACTION_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_REMOTE_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_APPROVED_REPOSITORY_ID", 7)
    monkeypatch.setattr(client_module, "get_late_interaction_client", lambda: remote)

    result = await validate_remote_late_interaction_release(
        max_attempts=2,
        initial_backoff_s=0,
    )

    assert result is ready
    assert remote.status.await_count == 2


@pytest.mark.asyncio
async def test_active_release_exhausted_transient_outage_is_typed_fail_open(
    monkeypatch,
):
    client_module = importlib.import_module("brain.late_interaction.client")
    remote = SimpleNamespace(status=AsyncMock(return_value=SimpleNamespace(status="failed_open", reason="timeout")))
    monkeypatch.setattr(settings, "LATE_INTERACTION_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_REMOTE_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_APPROVED_REPOSITORY_ID", 7)
    monkeypatch.setattr(client_module, "get_late_interaction_client", lambda: remote)

    with pytest.raises(
        LateInteractionReleaseUnavailableError,
        match="after 2 attempts",
    ):
        await validate_remote_late_interaction_release(
            max_attempts=2,
            initial_backoff_s=0,
        )

    api_source = (ROOT / "apps" / "api" / "main.py").read_text(encoding="utf-8")
    worker_source = (ROOT / "brain" / "workers" / "worker.py").read_text(encoding="utf-8")
    assert "except LateInteractionReleaseUnavailableError" in api_source
    assert "except LateInteractionReleaseUnavailableError" in worker_source
    assert "if settings.LATE_INTERACTION_REMOTE_ENABLED:" in worker_source


@pytest.mark.asyncio
async def test_local_provider_mode_does_not_require_remote_startup_validation(
    monkeypatch,
):
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    monkeypatch.setattr(settings, "LATE_INTERACTION_ENABLED", True)
    monkeypatch.setattr(settings, "LATE_INTERACTION_REMOTE_ENABLED", False)

    assert await validate_remote_late_interaction_release() is None


def test_identity_digest_is_finalized_once_and_noop_put_keeps_cache(tmp_path):
    store = SQLiteMatrixStore(str(tmp_path / "identity-cache.sqlite3"))
    first = StoredMatrix(
        chunk_id=1,
        path="one.py",
        content_hash=_hash("one"),
        matrix=np.asarray([[1.0, 0.0]], dtype=np.float32),
    )
    second = StoredMatrix(
        chunk_id=2,
        path="two.py",
        content_hash=_hash("two"),
        matrix=np.asarray([[0.0, 1.0]], dtype=np.float32),
    )
    try:
        assert store.put(7, MODEL_REVISION, [first]) == "r1"
        cache_key = (7, MODEL_REVISION)
        assert cache_key not in store._identity_digest_cache
        finalized = store.release_identity(7, MODEL_REVISION)
        assert finalized.index_revision == "r1"

        assert store.put(7, MODEL_REVISION, []) == "r1"
        assert store._identity_digest_cache[cache_key] is finalized

        assert store.put(7, MODEL_REVISION, [second]) == "r2"
        assert cache_key not in store._identity_digest_cache
        refreshed = store.release_identity(7, MODEL_REVISION)
        assert refreshed.index_revision == "r2"
        assert refreshed.document_count == 2
    finally:
        store.close()


def test_rerank_truncates_candidates_and_excludes_stale_hashes(tmp_path):
    body = _upsert_body()
    with _service(tmp_path, max_candidates=1) as client:
        client.post("/v1/index/upsert", headers=_headers(), json=body)
        client.post(
            "/v1/index/finalize",
            headers=_headers(),
            json={"repository_id": 7, "model_revision": MODEL_REVISION},
        )
        result = client.post(
            "/v1/rerank",
            headers=_headers(),
            json={
                "query": "alpha",
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "candidates": [
                    {
                        "chunk_id": 10,
                        "path": "alpha.py",
                        "content_hash": _hash("alpha document"),
                    },
                    {
                        "chunk_id": 11,
                        "path": "beta.py",
                        "content_hash": _hash("beta document"),
                    },
                ],
            },
        )
        stale = client.post(
            "/v1/rerank",
            headers=_headers(),
            json={
                "query": "alpha",
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "candidates": [
                    {
                        "chunk_id": 10,
                        "path": "alpha.py",
                        "content_hash": _hash("stale"),
                    }
                ],
            },
        )
        wrong_path = client.post(
            "/v1/rerank",
            headers=_headers(),
            json={
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "query": "alpha",
                "candidates": [
                    {
                        "chunk_id": 10,
                        "path": "spoofed.py",
                        "content_hash": _hash("alpha document"),
                    }
                ],
            },
        )

    assert result.status_code == 200
    assert result.json()["status"] == "partial"
    assert result.json()["coverage"] == 0.5
    assert [score["chunk_id"] for score in result.json()["scores"]] == [10]
    assert stale.json()["status"] == "insufficient_coverage"
    assert stale.json()["coverage"] == 0.0
    assert stale.json()["scores"] == []
    assert wrong_path.json()["status"] == "insufficient_coverage"
    assert wrong_path.json()["scores"] == []


def test_delete_mutates_revision_only_when_rows_are_removed(tmp_path):
    body = _upsert_body()
    with _service(tmp_path) as client:
        client.post("/v1/index/upsert", headers=_headers(), json=body)
        deleted = client.post(
            "/v1/index/delete",
            headers=_headers(),
            json={
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "index_revision": "r1",
                "chunk_ids": [10],
            },
        )
        repeated = client.post(
            "/v1/index/delete",
            headers=_headers(),
            json={
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "index_revision": "r2",
                "chunk_ids": [10],
            },
        )

    assert deleted.json()["deleted"] == 1
    assert deleted.json()["index_revision"] == "r2"
    assert repeated.json()["deleted"] == 0
    assert repeated.json()["index_revision"] == "r2"


def test_non_test_service_requires_token_and_license_acknowledgement(tmp_path):
    common = {
        "database_path": str(tmp_path / "late.sqlite3"),
        "environment": "production",
        "model_revision": MODEL_REVISION,
        "dimension": 2,
    }
    with pytest.raises(RuntimeError, match="TOKEN"):
        create_app(ServiceConfig(**common), encoder=FakeEncoder())
    with pytest.raises(RuntimeError, match="LICENSE"):
        create_app(ServiceConfig(**common, token=TOKEN), encoder=FakeEncoder())


def test_service_and_client_contract_cap_candidates_at_500():
    assert ServiceConfig().max_candidates == 500


def test_pylate_adapter_pins_remote_code_revision_and_bf16(monkeypatch):
    calls: dict[str, object] = {}
    fake_torch = types.ModuleType("torch")
    fake_torch.bfloat16 = object()

    class FakeModel:
        def __init__(self):
            self.tokenizer = type("Tokenizer", (), {"eos_token": "<eos>", "pad_token": None})()

    class FakeModels:
        @staticmethod
        def ColBERT(**kwargs):
            calls.update(kwargs)
            return FakeModel()

    fake_pylate = types.ModuleType("pylate")
    fake_pylate.models = FakeModels
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "pylate", fake_pylate)

    model = PyLateEncoder(
        model_name="LiquidAI/LFM2.5-ColBERT-350M",
        model_revision=MODEL_REVISION,
    )._load_model()

    assert calls["revision"] == MODEL_REVISION
    assert calls["trust_remote_code"] is True
    assert calls["query_length"] == 32
    assert calls["document_length"] == 512
    assert calls["model_kwargs"]["torch_dtype"] is fake_torch.bfloat16
    assert model.tokenizer.pad_token == "<eos>"


@pytest.mark.asyncio
async def test_remote_client_sends_auth_and_revisions_without_matrices(
    isolated_late_metrics,
):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("X-Late-Interaction-Token")
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "status": "scored",
                "scores": [{"chunk_id": 10, "path": "alpha.py", "score": 2.0}],
                "coverage": 1.0,
                "latency_ms": 3.0,
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "index_revision": "r3",
                "lineage_id": "test-lineage",
                "identity_digest": _hash("inventory"),
                "document_count": 1,
                "reason": "",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = RemoteLateInteractionClient(
            base_url="http://gpu-service",
            token=TOKEN,
            model_revision=MODEL_REVISION,
            expected_index_revision="r3",
            client=http_client,
        )
        result = await client.rerank(
            "secret query",
            7,
            [LateInteractionCandidate(10, "alpha.py", _hash("alpha document"))],
        )

    assert result.status == "scored"
    assert seen["token"] == TOKEN
    assert seen["payload"]["model_revision"] == MODEL_REVISION
    assert seen["payload"]["index_revision"] == "r3"
    assert "matrix" not in json.dumps(seen["payload"])
    metrics = snapshot()
    assert metrics["provider_requests"] == 1
    assert metrics["provider_failures"] == 0
    assert metrics["provider_latency_ms_total"] >= 0
    assert metrics["rerank_requests"] == 0


@pytest.mark.asyncio
async def test_remote_client_requires_hash_and_does_not_leak_query_on_failure(
    isolated_late_metrics,
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text=request.content.decode("utf-8"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = RemoteLateInteractionClient(
            base_url="http://gpu-service",
            token=TOKEN,
            model_revision=MODEL_REVISION,
            client=http_client,
        )
        missing_hash = await client.rerank(
            "do not expose this",
            7,
            [LateInteractionCandidate(10, "alpha.py")],
        )
        failed = await client.rerank(
            "do not expose this",
            7,
            [LateInteractionCandidate(10, "alpha.py", _hash("alpha"))],
        )

    assert missing_hash.status == "failed_open"
    assert missing_hash.reason == "content_hash_required"
    assert failed.status == "failed_open"
    assert failed.reason == "http_503"
    assert "do not expose this" not in failed.reason
    metrics = snapshot()
    # The locally rejected missing-hash request never reached the provider.
    assert metrics["provider_requests"] == 1
    assert metrics["provider_failures"] == 1
    assert metrics["provider_latency_ms_total"] >= 0
    assert metrics["rerank_requests"] == 0


@pytest.mark.asyncio
async def test_remote_client_uses_separate_timeout_for_mutations():
    observed: list[tuple[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append((request.url.path, request.extensions["timeout"]["read"]))
        if request.url.path == "/v1/rerank":
            return httpx.Response(
                200,
                json={
                    "status": "scored",
                    "scores": [],
                    "coverage": 1.0,
                    "model_revision": MODEL_REVISION,
                    "index_revision": "r1",
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "updated",
                "accepted": 1,
                "model_revision": MODEL_REVISION,
                "index_revision": "r2",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = RemoteLateInteractionClient(
            base_url="http://gpu-service",
            token=TOKEN,
            model_revision=MODEL_REVISION,
            timeout_s=2.0,
            mutation_timeout_s=120.0,
            client=http_client,
        )
        await client.rerank(
            "query",
            7,
            [LateInteractionCandidate(10, "alpha.py", _hash("alpha"))],
        )
        await client.upsert_documents(
            7,
            [
                LateInteractionDocument(
                    chunk_id=10,
                    path="alpha.py",
                    content_hash=_hash("alpha"),
                    text="alpha",
                )
            ],
        )

    assert observed == [
        ("/v1/rerank", 2.0),
        ("/v1/index/upsert", 120.0),
    ]


@pytest.mark.asyncio
async def test_file_indexer_remote_dual_write_batches_upserts_before_cleanup(
    monkeypatch,
):
    upsert = AsyncMock(
        return_value=SimpleNamespace(status="updated", reason=""),
    )
    delete_documents = AsyncMock(
        return_value=SimpleNamespace(status="updated", reason=""),
    )
    client = SimpleNamespace(
        upsert_documents=upsert,
        delete_documents=delete_documents,
    )
    monkeypatch.setattr(
        "brain.late_interaction.client.get_late_interaction_client",
        lambda: client,
    )
    monkeypatch.setattr(settings, "LATE_INTERACTION_REMOTE_MAX_DOCUMENTS", 64)
    indexer = object.__new__(FileIndexer)
    indexer._late_provider = None
    indexer._late_dual_write_failures = 0
    indexer._late_dual_write_circuit_open = False
    documents = [
        {
            "chunk_id": chunk_id,
            "content_hash": f"{chunk_id:064x}",
            "text": f"chunk {chunk_id}",
        }
        for chunk_id in range(1, 71)
    ]

    await indexer._sync_remote_late_documents(
        repository_id=9,
        path="brain/example.py",
        documents=documents,
        obsolete_chunk_ids=list(range(1000, 1700)),
    )

    assert [len(call.args[1]) for call in upsert.await_args_list] == [64, 6]
    assert [len(call.args[1]) for call in delete_documents.await_args_list] == [
        500,
        200,
    ]
    assert upsert.await_args_list[-1].args[1][-1].path == "brain/example.py"
    assert indexer._late_dual_write_failures == 0


@pytest.mark.asyncio
async def test_file_indexer_remote_dual_write_failure_opens_circuit_and_skips_delete(
    monkeypatch,
):
    upsert = AsyncMock(
        return_value=SimpleNamespace(status="failed_open", reason="timeout"),
    )
    delete_documents = AsyncMock()
    monkeypatch.setattr(
        "brain.late_interaction.client.get_late_interaction_client",
        lambda: SimpleNamespace(
            upsert_documents=upsert,
            delete_documents=delete_documents,
        ),
    )
    monkeypatch.setattr(
        settings,
        "LATE_INTERACTION_DUAL_WRITE_MAX_CONSECUTIVE_FAILURES",
        1,
    )
    indexer = object.__new__(FileIndexer)
    indexer._late_provider = None
    indexer._late_dual_write_failures = 0
    indexer._late_dual_write_circuit_open = False

    await indexer._sync_remote_late_documents(
        repository_id=9,
        path="brain/example.py",
        documents=[
            {
                "chunk_id": 1,
                "content_hash": "a" * 64,
                "text": "chunk",
            }
        ],
        obsolete_chunk_ids=[99],
    )

    assert indexer._late_dual_write_circuit_open is True
    delete_documents.assert_not_awaited()


def test_gpu_service_lists_index_documents_with_authenticated_cursor(tmp_path):
    with _service(tmp_path) as client:
        assert (
            client.post(
                "/v1/index/upsert",
                headers=_headers(),
                json=_upsert_body(),
            ).status_code
            == 200
        )
        first = client.get(
            "/v1/index/documents",
            headers=_headers(),
            params={
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "after_chunk_id": 0,
                "limit": 1,
            },
        )
        assert first.status_code == 200
        first_payload = first.json()
        assert [item["chunk_id"] for item in first_payload["documents"]] == [10]
        assert first_payload["next_after_chunk_id"] == 10

        second = client.get(
            "/v1/index/documents",
            headers=_headers(),
            params={
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "after_chunk_id": first_payload["next_after_chunk_id"],
                "limit": 1,
            },
        )
        assert [item["chunk_id"] for item in second.json()["documents"]] == [11]
        assert (
            client.get(
                "/v1/index/documents",
                params={
                    "repository_id": 7,
                    "model_revision": MODEL_REVISION,
                },
            ).status_code
            == 401
        )


@pytest.mark.asyncio
async def test_remote_sync_is_hash_idempotent_and_prunes_only_after_full_scan(
    monkeypatch,
):
    database_batch = AsyncMock(
        side_effect=[
            [(1, "a.py", "alpha"), (2, "b.py", "beta"), (3, "empty.py", "")],
            [],
            [(1, "a.py", "alpha"), (2, "b.py", "beta"), (3, "empty.py", "")],
            [],
        ]
    )
    monkeypatch.setattr(remote_sync, "_database_batch", database_batch)
    upsert = AsyncMock(
        return_value=LateInteractionMutationResult(
            status="updated",
            accepted=1,
            unchanged=0,
            model_revision=MODEL_REVISION,
            index_revision="r2",
        )
    )
    list_documents = AsyncMock(
        side_effect=[
            LateInteractionIndexPage(
                status="ready",
                repository_id=7,
                entries=(
                    LateInteractionIndexEntry(1, "a.py", _hash("alpha")),
                    LateInteractionIndexEntry(99, "stale.py", "f" * 64),
                ),
                model_revision=MODEL_REVISION,
                index_revision="r1",
            ),
            LateInteractionIndexPage(
                status="ready",
                repository_id=7,
                entries=(
                    LateInteractionIndexEntry(1, "a.py", _hash("alpha")),
                    LateInteractionIndexEntry(2, "b.py", _hash("beta")),
                    LateInteractionIndexEntry(99, "stale.py", "f" * 64),
                ),
                model_revision=MODEL_REVISION,
                index_revision="r2",
            ),
            LateInteractionIndexPage(
                status="ready",
                repository_id=7,
                entries=(
                    LateInteractionIndexEntry(1, "a.py", _hash("alpha")),
                    LateInteractionIndexEntry(2, "b.py", _hash("beta")),
                ),
                model_revision=MODEL_REVISION,
                index_revision="r3",
            ),
        ]
    )
    database_documents_by_ids = AsyncMock(return_value=[])
    monkeypatch.setattr(
        remote_sync,
        "_database_documents_by_ids",
        database_documents_by_ids,
    )
    delete_documents = AsyncMock(
        return_value=LateInteractionMutationResult(
            status="updated",
            deleted=1,
            model_revision=MODEL_REVISION,
            index_revision="r3",
        )
    )
    client = SimpleNamespace(
        upsert_documents=upsert,
        list_documents=list_documents,
        delete_documents=delete_documents,
    )

    result = await remote_sync.sync_remote_late_interaction(
        7,
        batch_size=2,
        dry_run=False,
        prune=True,
        client=client,
    )

    assert result.to_dict() == {
        "repository_id": 7,
        "dry_run": False,
        "examined": 3,
        "accepted": 1,
        "unchanged": 1,
        "deleted": 1,
        "skipped_empty": 1,
        "failed_batches": 0,
        "model_revision": MODEL_REVISION,
        "index_revision": "r3",
        "source_identity_digest": remote_sync._identity_digest(
            {
                1: ("a.py", _hash("alpha")),
                2: ("b.py", _hash("beta")),
            }
        ),
        "remote_identity_digest": remote_sync._identity_digest(
            {
                1: ("a.py", _hash("alpha")),
                2: ("b.py", _hash("beta")),
            }
        ),
        "verified_documents": 2,
        "verification_mode": "exact",
        "reason": "",
        "passed": True,
    }
    sent = upsert.await_args.args[1]
    assert len(sent) == 1
    assert sent[0].content_hash == _hash("beta")
    delete_documents.assert_awaited_once_with(7, [99])

    with pytest.raises(ValueError, match="complete unbounded scan"):
        await remote_sync.sync_remote_late_interaction(
            7,
            max_chunks=1,
            dry_run=False,
            prune=True,
            client=client,
        )
