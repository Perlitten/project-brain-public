from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Sequence

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from brain.late_interaction.client import RemoteLateInteractionClient
from brain.late_interaction.gpu_service import (
    AsyncRevisionLock,
    SQLiteMatrixStore,
    ServiceConfig,
    StoredMatrix,
    create_app,
)


MODEL_REVISION = "59633c2e31717b3502343ff566bee9fda3261943"
TOKEN = "test-shared-secret"


class FakeEncoder:
    model_revision = MODEL_REVISION
    dimension = 2

    async def encode_queries(self, texts: Sequence[str]) -> list[np.ndarray]:
        return [self._matrix(text) for text in texts]

    async def encode_documents(self, texts: Sequence[str]) -> list[np.ndarray]:
        return [self._matrix(text) for text in texts]

    @staticmethod
    def _matrix(_text: str) -> np.ndarray:
        return np.asarray([[1.0, 0.0]], dtype=np.float32)


class BlockingFirstDocumentEncoder(FakeEncoder):
    def __init__(self) -> None:
        self.first_document_started = asyncio.Event()
        self.release_first_document = asyncio.Event()
        self.document_calls = 0

    async def encode_documents(self, texts: Sequence[str]) -> list[np.ndarray]:
        self.document_calls += 1
        if self.document_calls == 1:
            self.first_document_started.set()
            await self.release_first_document.wait()
        return await super().encode_documents(texts)


class BlockingSnapshotEncoder(FakeEncoder):
    def __init__(self) -> None:
        self.query_started = asyncio.Event()
        self.release_query = asyncio.Event()
        self.document_started = asyncio.Event()

    async def encode_queries(self, texts: Sequence[str]) -> list[np.ndarray]:
        self.query_started.set()
        await self.release_query.wait()
        return await super().encode_queries(texts)

    async def encode_documents(self, texts: Sequence[str]) -> list[np.ndarray]:
        self.document_started.set()
        return await super().encode_documents(texts)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _identity_digest(identities: list[tuple[int, str, str]]) -> str:
    digest = hashlib.sha256()
    for chunk_id, path, content_hash in sorted(identities):
        digest.update(
            json.dumps(
                [chunk_id, path, content_hash.lower()],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _config(path: Path, **overrides) -> ServiceConfig:
    values = {
        "database_path": str(path),
        "token": TOKEN,
        "environment": "test",
        "model_revision": MODEL_REVISION,
        "dimension": 2,
    }
    values.update(overrides)
    return ServiceConfig(**values)


def _headers() -> dict[str, str]:
    return {"X-Late-Interaction-Token": TOKEN}


def _document(chunk_id: int, text: str) -> dict[str, object]:
    return {
        "chunk_id": chunk_id,
        "path": f"{chunk_id}.py",
        "content_hash": _hash(text),
        "text": text,
    }


def test_legacy_manifest_read_does_not_mint_lineage_until_mutation(
    tmp_path: Path,
):
    database_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE late_interaction_documents (
            repository_id INTEGER NOT NULL,
            chunk_id INTEGER NOT NULL,
            model_revision TEXT NOT NULL,
            path TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            token_count INTEGER NOT NULL,
            dimension INTEGER NOT NULL,
            vector_dtype TEXT NOT NULL,
            matrix BLOB NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (repository_id, chunk_id, model_revision)
        );
        CREATE TABLE late_interaction_manifests (
            repository_id INTEGER NOT NULL,
            model_revision TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (repository_id, model_revision)
        );
        CREATE TABLE late_interaction_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT INTO late_interaction_metadata(key, value)
        VALUES ('global_revision', '1');
        """
    )
    connection.execute(
        """
        INSERT INTO late_interaction_documents (
            repository_id, chunk_id, model_revision, path, content_hash,
            token_count, dimension, vector_dtype, matrix, updated_at
        ) VALUES (?, ?, ?, ?, ?, 1, 2, 'float16', ?, 'now')
        """,
        (7, 10, MODEL_REVISION, "alpha.py", _hash("alpha"), b"\0\0\0\0"),
    )
    connection.execute(
        """
        INSERT INTO late_interaction_manifests (
            repository_id, model_revision, revision, updated_at
        ) VALUES (?, ?, 1, 'now')
        """,
        (7, MODEL_REVISION),
    )
    connection.commit()
    connection.close()

    store = SQLiteMatrixStore(str(database_path))
    before = store.inventory(7, MODEL_REVISION)
    unchanged_revision = store.put(7, MODEL_REVISION, [])
    after = store.inventory(7, MODEL_REVISION)
    store.close()
    reopened = SQLiteMatrixStore(str(database_path))
    persisted = reopened.inventory(7, MODEL_REVISION)
    reopened.close()

    assert before.lineage_id == ""
    assert unchanged_revision == "r1"
    assert str(uuid.UUID(after.lineage_id)) == after.lineage_id
    assert persisted.lineage_id == after.lineage_id
    assert before.identity_digest == _identity_digest([(10, "alpha.py", _hash("alpha"))])
    assert persisted.identity_digest == before.identity_digest
    assert persisted.index_revision == "r1"
    assert persisted.document_count == 1


def test_status_read_does_not_create_manifest_or_lineage(tmp_path: Path):
    database_path = tmp_path / "read-only-status.sqlite3"
    store = SQLiteMatrixStore(str(database_path))
    app = create_app(
        _config(database_path),
        encoder=FakeEncoder(),
        store=store,
    )

    with TestClient(app) as client:
        status = client.get(
            "/v1/index/status",
            headers=_headers(),
            params={"repository_id": 7, "model_revision": MODEL_REVISION},
        )

    connection = sqlite3.connect(database_path)
    manifest_count = connection.execute("SELECT COUNT(*) FROM late_interaction_manifests").fetchone()[0]
    connection.close()
    store.close()

    assert status.json()["index_revision"] == "r0"
    assert status.json()["lineage_id"] == ""
    assert manifest_count == 0


def test_finalize_mints_legacy_lineage_without_advancing_revision(
    tmp_path: Path,
):
    database_path = tmp_path / "legacy-finalize.sqlite3"
    store = SQLiteMatrixStore(str(database_path))
    store.put(
        7,
        MODEL_REVISION,
        [
            StoredMatrix(
                chunk_id=1,
                path="1.py",
                content_hash=_hash("one"),
                matrix=np.asarray([[1.0, 0.0]], dtype=np.float32),
            )
        ],
    )
    with store._lock, store._connection:
        store._connection.execute(
            """
            UPDATE late_interaction_manifests
            SET lineage_id = NULL
            WHERE repository_id = ? AND model_revision = ?
            """,
            (7, MODEL_REVISION),
        )
        store._identity_digest_cache.clear()

    app = create_app(
        _config(database_path),
        encoder=FakeEncoder(),
        store=store,
    )
    with TestClient(app) as client:
        finalized = client.post(
            "/v1/index/finalize",
            headers=_headers(),
            json={"repository_id": 7, "model_revision": MODEL_REVISION},
        )

    payload = finalized.json()
    store.close()

    assert payload["status"] == "ready"
    assert payload["index_revision"] == "r1"
    assert str(uuid.UUID(payload["lineage_id"])) == payload["lineage_id"]
    assert payload["identity_digest"] == _identity_digest([(1, "1.py", _hash("one"))])


def test_service_policy_enforces_repository_floor_and_minimum_count(
    tmp_path: Path,
):
    database_path = tmp_path / "policy.sqlite3"
    store = SQLiteMatrixStore(str(database_path))
    store.put(
        7,
        MODEL_REVISION,
        [
            StoredMatrix(
                chunk_id=1,
                path="1.py",
                content_hash=_hash("one"),
                matrix=np.asarray([[1.0, 0.0]], dtype=np.float32),
            )
        ],
    )
    initial = store.inventory(7, MODEL_REVISION)
    app = create_app(
        _config(
            database_path,
            approved_repository_id=7,
            minimum_index_revision="r1",
            minimum_document_count=2,
        ),
        encoder=FakeEncoder(),
        store=store,
    )

    with TestClient(app) as client:
        wrong_repository = client.get(
            "/v1/index/status",
            headers=_headers(),
            params={"repository_id": 8, "model_revision": MODEL_REVISION},
        )
        incomplete = client.get(
            "/v1/index/status",
            headers=_headers(),
            params={"repository_id": 7, "model_revision": MODEL_REVISION},
        )
        not_ready = client.get("/ready", headers=_headers())
        added = client.post(
            "/v1/index/upsert",
            headers=_headers(),
            json={
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "documents": [_document(2, "two")],
            },
        )
        finalized = client.post(
            "/v1/index/finalize",
            headers=_headers(),
            json={"repository_id": 7, "model_revision": MODEL_REVISION},
        )
        ready = client.get("/ready", headers=_headers())
        complete = client.get(
            "/v1/index/status",
            headers=_headers(),
            params={"repository_id": 7, "model_revision": MODEL_REVISION},
        )

    assert wrong_repository.status_code == 403
    assert incomplete.json()["status"] == "not_ready"
    assert incomplete.json()["reason"] == "document_count_below_service_minimum"
    assert incomplete.json()["minimum_document_count"] == 2
    assert incomplete.json()["lineage_id"] == initial.lineage_id
    assert not_ready.status_code == 503
    assert added.json()["status"] == "updated"
    assert finalized.json()["status"] == "ready"
    assert ready.status_code == 200
    assert ready.json()["document_count"] == 2
    assert ready.json()["minimum_document_count"] == 2
    assert ready.json()["lineage_id"] == initial.lineage_id
    assert complete.json()["status"] == "ready"
    assert complete.json()["identity_digest"] == _identity_digest(
        [(1, "1.py", _hash("one")), (2, "2.py", _hash("two"))]
    )

    stricter_app = create_app(
        _config(
            database_path,
            approved_repository_id=7,
            minimum_index_revision="r3",
            minimum_document_count=2,
        ),
        encoder=FakeEncoder(),
        store=store,
    )
    with TestClient(stricter_app) as client:
        below_floor = client.get(
            "/v1/index/status",
            headers=_headers(),
            params={"repository_id": 7, "model_revision": MODEL_REVISION},
        )
        advanced = client.post(
            "/v1/index/upsert",
            headers=_headers(),
            json={
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "documents": [_document(3, "three")],
            },
        )
        client.post(
            "/v1/index/finalize",
            headers=_headers(),
            json={"repository_id": 7, "model_revision": MODEL_REVISION},
        )
        at_floor = client.get(
            "/v1/index/status",
            headers=_headers(),
            params={"repository_id": 7, "model_revision": MODEL_REVISION},
        )

    assert below_floor.json()["status"] == "revision_mismatch"
    assert below_floor.json()["reason"] == "index_revision_below_approved_floor"
    assert advanced.json()["status"] == "updated"
    assert advanced.json()["index_revision"] == "r3"
    assert at_floor.json()["status"] == "ready"
    assert store.inventory(7, MODEL_REVISION).document_count == 3
    store.close()


def test_service_floor_keeps_r0_rebuild_mutable_but_reads_not_ready(
    tmp_path: Path,
):
    database_path = tmp_path / "floor-rebuild.sqlite3"
    store = SQLiteMatrixStore(str(database_path))
    app = create_app(
        _config(
            database_path,
            approved_repository_id=7,
            minimum_index_revision="r2",
        ),
        encoder=FakeEncoder(),
        store=store,
    )

    with TestClient(app) as client:
        first = client.post(
            "/v1/index/upsert",
            headers=_headers(),
            json={
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "index_revision": "r0",
                "minimum_index_revision": "r2",
                "documents": [_document(1, "one")],
            },
        )
        recovery_inventory = client.get(
            "/v1/index/documents",
            headers=_headers(),
            params={"repository_id": 7, "model_revision": MODEL_REVISION},
        )
        client.post(
            "/v1/index/finalize",
            headers=_headers(),
            json={"repository_id": 7, "model_revision": MODEL_REVISION},
        )
        below_floor = client.get(
            "/v1/index/status",
            headers=_headers(),
            params={"repository_id": 7, "model_revision": MODEL_REVISION},
        )
        second = client.post(
            "/v1/index/upsert",
            headers=_headers(),
            json={
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "index_revision": "r1",
                "minimum_index_revision": "r2",
                "documents": [_document(2, "two")],
            },
        )
        client.post(
            "/v1/index/finalize",
            headers=_headers(),
            json={"repository_id": 7, "model_revision": MODEL_REVISION},
        )
        ready = client.get(
            "/v1/index/status",
            headers=_headers(),
            params={"repository_id": 7, "model_revision": MODEL_REVISION},
        )

    store.close()

    assert first.json()["status"] == "updated"
    assert first.json()["index_revision"] == "r1"
    assert recovery_inventory.json()["status"] == "ready"
    assert [item["chunk_id"] for item in recovery_inventory.json()["documents"]] == [1]
    assert below_floor.json()["status"] == "revision_mismatch"
    assert below_floor.json()["reason"] == "index_revision_below_approved_floor"
    assert second.json()["status"] == "updated"
    assert second.json()["index_revision"] == "r2"
    assert ready.json()["status"] == "ready"


def test_release_identity_digest_is_cached_per_revision(
    tmp_path: Path,
    monkeypatch,
):
    database_path = tmp_path / "identity-cache.sqlite3"
    store = SQLiteMatrixStore(str(database_path))
    store.put(
        7,
        MODEL_REVISION,
        [
            StoredMatrix(
                chunk_id=1,
                path="1.py",
                content_hash=_hash("one"),
                matrix=np.asarray([[1.0, 0.0]], dtype=np.float32),
            )
        ],
    )
    calls = 0
    original = store._identity_digest_unlocked

    def counted_digest(repository_id: int, model_revision: str) -> str:
        nonlocal calls
        calls += 1
        return original(repository_id, model_revision)

    monkeypatch.setattr(store, "_identity_digest_unlocked", counted_digest)

    first = store.release_identity(7, MODEL_REVISION)
    second = store.release_identity(7, MODEL_REVISION)
    inventory = store.inventory(7, MODEL_REVISION)
    store.put(
        7,
        MODEL_REVISION,
        [
            StoredMatrix(
                chunk_id=2,
                path="2.py",
                content_hash=_hash("two"),
                matrix=np.asarray([[1.0, 0.0]], dtype=np.float32),
            )
        ],
    )
    advanced = store.release_identity(7, MODEL_REVISION)
    store.close()

    assert first == second
    assert inventory.identity_digest == first.identity_digest
    # Each revision pays the canonical O(N) scan only when explicitly
    # finalized. Mutations themselves remain O(batch), avoiding O(N²)
    # backfills.
    assert calls == 2
    assert first.index_revision == "r1"
    assert advanced.index_revision == "r2"
    assert advanced.document_count == 2


def test_service_policy_env_and_validation(monkeypatch):
    monkeypatch.setenv("LATE_INTERACTION_SERVICE_TOKEN", TOKEN)
    monkeypatch.setenv("LATE_INTERACTION_SERVICE_ENV", "test")
    monkeypatch.setenv("LATE_INTERACTION_SERVICE_APPROVED_REPOSITORY_ID", "7")
    monkeypatch.setenv("LATE_INTERACTION_SERVICE_MINIMUM_INDEX_REVISION", "r12")
    monkeypatch.setenv("LATE_INTERACTION_SERVICE_MINIMUM_DOCUMENT_COUNT", "42")

    config = ServiceConfig.from_env()

    assert config.approved_repository_id == 7
    assert config.minimum_index_revision == "r12"
    assert config.minimum_document_count == 42
    config.validate_startup()
    with pytest.raises(RuntimeError, match="rN format"):
        _config(
            Path("ignored.sqlite3"),
            approved_repository_id=7,
            minimum_index_revision="12",
        ).validate_startup()
    with pytest.raises(RuntimeError, match="APPROVED_REPOSITORY_ID"):
        _config(
            Path("ignored.sqlite3"),
            minimum_document_count=1,
        ).validate_startup()


@pytest.mark.asyncio
async def test_exact_client_validates_lineage_digest_and_document_count():
    identity_digest = _identity_digest([(1, "1.py", _hash("one"))])
    payload = {
        "status": "ready",
        "repository_id": 7,
        "model_revision": MODEL_REVISION,
        "index_revision": "r1",
        "lineage_id": "4e4de008-74d8-4cff-a6b4-e949d4de5a12",
        "identity_digest": identity_digest,
        "document_count": 1,
        "minimum_document_count": 1,
        "bytes": 4,
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        exact = RemoteLateInteractionClient(
            base_url="http://gpu-service",
            token=TOKEN,
            model_revision=MODEL_REVISION,
            expected_index_revision="r1",
            approved_repository_id=7,
            expected_lineage_id=payload["lineage_id"],
            expected_identity_digest=identity_digest,
            expected_document_count=1,
            minimum_document_count=1,
            client=http_client,
        )
        valid = await exact.status(7)

        wrong_lineage = RemoteLateInteractionClient(
            base_url="http://gpu-service",
            token=TOKEN,
            model_revision=MODEL_REVISION,
            expected_index_revision="r1",
            approved_repository_id=7,
            expected_lineage_id="different-lineage",
            client=http_client,
        )
        lineage_mismatch = await wrong_lineage.status(7)

        wrong_digest = RemoteLateInteractionClient(
            base_url="http://gpu-service",
            token=TOKEN,
            model_revision=MODEL_REVISION,
            expected_index_revision="r1",
            approved_repository_id=7,
            expected_identity_digest="f" * 64,
            client=http_client,
        )
        digest_mismatch = await wrong_digest.status(7)

        wrong_count = RemoteLateInteractionClient(
            base_url="http://gpu-service",
            token=TOKEN,
            model_revision=MODEL_REVISION,
            expected_index_revision="r1",
            approved_repository_id=7,
            expected_document_count=2,
            client=http_client,
        )
        count_mismatch = await wrong_count.status(7)

    assert valid.status == "ready"
    assert valid.lineage_id == payload["lineage_id"]
    assert valid.identity_digest == identity_digest
    assert lineage_mismatch.reason == "lineage_id_mismatch"
    assert digest_mismatch.reason == "identity_digest_mismatch"
    assert count_mismatch.reason == "document_count_mismatch"


@pytest.mark.asyncio
async def test_cancelled_waiting_writer_wakes_queued_reader():
    lock = AsyncRevisionLock()
    first_read = lock.read()
    await first_read.__aenter__()
    writer_entered = asyncio.Event()
    second_reader_entered = asyncio.Event()

    async def writer() -> None:
        async with lock.write():
            writer_entered.set()

    async def second_reader() -> None:
        async with lock.read():
            second_reader_entered.set()

    writer_task = asyncio.create_task(writer())
    for _ in range(100):
        if lock._waiting_writers:
            break
        await asyncio.sleep(0)
    reader_task = asyncio.create_task(second_reader())
    await asyncio.sleep(0)
    writer_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writer_task

    await asyncio.wait_for(second_reader_entered.wait(), timeout=1)
    await reader_task
    assert not writer_entered.is_set()
    await first_read.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_rerank_client_rejects_mid_process_corpus_identity_change():
    expected_digest = _identity_digest([(1, "1.py", _hash("one"))])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "scored",
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "index_revision": "r1",
                "lineage_id": "replacement-lineage",
                "identity_digest": expected_digest,
                "document_count": 1,
                "coverage": 1.0,
                "scores": [{"chunk_id": 1, "path": "1.py", "score": 1.0}],
                "reason": "",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = RemoteLateInteractionClient(
            base_url="http://gpu-service",
            token=TOKEN,
            model_revision=MODEL_REVISION,
            expected_index_revision="r1",
            approved_repository_id=7,
            expected_lineage_id="approved-lineage",
            expected_identity_digest=expected_digest,
            expected_document_count=1,
            client=http_client,
        )
        result = await client.rerank(
            "one",
            7,
            [],
        )

    assert result.status == "inventory_mismatch"
    assert result.index_revision == "r1"
    assert result.reason == "lineage_id_mismatch"
    assert result.scores == ()


@pytest.mark.asyncio
async def test_concurrent_exact_upserts_allow_only_one_r0_mutation(
    tmp_path: Path,
):
    database_path = tmp_path / "concurrent-exact-upserts.sqlite3"
    store = SQLiteMatrixStore(str(database_path))
    encoder = BlockingFirstDocumentEncoder()
    app = create_app(
        _config(database_path, max_concurrency=2),
        encoder=encoder,
        store=store,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://service",
    ) as client:
        first = asyncio.create_task(
            client.post(
                "/v1/index/upsert",
                headers=_headers(),
                json={
                    "repository_id": 7,
                    "model_revision": MODEL_REVISION,
                    "index_revision": "r0",
                    "documents": [_document(1, "one")],
                },
            )
        )
        await asyncio.wait_for(encoder.first_document_started.wait(), timeout=1)
        second = asyncio.create_task(
            client.post(
                "/v1/index/upsert",
                headers=_headers(),
                json={
                    "repository_id": 7,
                    "model_revision": MODEL_REVISION,
                    "index_revision": "r0",
                    "documents": [_document(2, "two")],
                },
            )
        )
        second_response = await asyncio.wait_for(second, timeout=1)
        assert second_response.json()["status"] == "updated"
        encoder.release_first_document.set()
        first_response = await first

    payloads = [first_response.json(), second_response.json()]
    updated = [payload for payload in payloads if payload["status"] == "updated"]
    mismatched = [payload for payload in payloads if payload["status"] == "revision_mismatch"]
    inventory = store.inventory(7, MODEL_REVISION)
    store.close()

    assert len(updated) == 1
    assert updated[0]["index_revision"] == "r1"
    assert len(mismatched) == 1
    assert mismatched[0]["index_revision"] == "r1"
    assert mismatched[0]["reason"] == "index_revision_mismatch"
    assert encoder.document_calls == 2
    assert inventory.index_revision == "r1"
    assert inventory.document_count == 1


@pytest.mark.asyncio
async def test_exact_rerank_snapshot_blocks_mutation_and_returns_its_revision(
    tmp_path: Path,
):
    database_path = tmp_path / "rerank-snapshot.sqlite3"
    store = SQLiteMatrixStore(str(database_path))
    store.put(
        7,
        MODEL_REVISION,
        [
            StoredMatrix(
                chunk_id=1,
                path="1.py",
                content_hash=_hash("one"),
                matrix=np.asarray([[1.0, 0.0]], dtype=np.float32),
            )
        ],
    )
    store.release_identity(7, MODEL_REVISION)
    encoder = BlockingSnapshotEncoder()
    app = create_app(
        _config(database_path, max_concurrency=2),
        encoder=encoder,
        store=store,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://service",
    ) as client:
        rerank = asyncio.create_task(
            client.post(
                "/v1/rerank",
                headers=_headers(),
                json={
                    "query": "one",
                    "repository_id": 7,
                    "model_revision": MODEL_REVISION,
                    "index_revision": "r1",
                    "candidates": [
                        {
                            "chunk_id": 1,
                            "path": "1.py",
                            "content_hash": _hash("one"),
                        }
                    ],
                },
            )
        )
        await asyncio.wait_for(encoder.query_started.wait(), timeout=1)
        mutation = asyncio.create_task(
            client.post(
                "/v1/index/upsert",
                headers=_headers(),
                json={
                    "repository_id": 7,
                    "model_revision": MODEL_REVISION,
                    "index_revision": "r1",
                    "documents": [_document(2, "two")],
                },
            )
        )
        await asyncio.wait_for(encoder.document_started.wait(), timeout=1)
        assert not mutation.done()
        assert store.index_revision(7, MODEL_REVISION) == "r1"

        encoder.release_query.set()
        rerank_response = await rerank
        mutation_response = await mutation

    inventory = store.inventory(7, MODEL_REVISION)
    store.close()

    assert rerank_response.json()["status"] == "scored"
    assert rerank_response.json()["index_revision"] == "r1"
    assert rerank_response.json()["repository_id"] == 7
    assert rerank_response.json()["lineage_id"]
    assert rerank_response.json()["identity_digest"] == _identity_digest([(1, "1.py", _hash("one"))])
    assert rerank_response.json()["document_count"] == 1
    assert mutation_response.json()["status"] == "updated"
    assert mutation_response.json()["index_revision"] == "r2"
    assert inventory.index_revision == "r2"
    assert inventory.document_count == 2


@pytest.mark.asyncio
async def test_upsert_rejects_changed_content_for_existing_chunk_identity(
    tmp_path: Path,
):
    database_path = tmp_path / "immutable-identity.sqlite3"
    store = SQLiteMatrixStore(str(database_path))
    app = create_app(
        _config(database_path),
        encoder=FakeEncoder(),
        store=store,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://service",
    ) as client:
        initial_response = await client.post(
            "/v1/index/upsert",
            headers=_headers(),
            json={
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "index_revision": "r0",
                "documents": [_document(1, "original")],
            },
        )
        before_inventory = store.inventory(7, MODEL_REVISION)
        before_identity = store.document_identities(
            7,
            MODEL_REVISION,
            [1],
        )
        conflict_response = await client.post(
            "/v1/index/upsert",
            headers=_headers(),
            json={
                "repository_id": 7,
                "model_revision": MODEL_REVISION,
                "index_revision": "r1",
                "documents": [_document(1, "changed")],
            },
        )

    after_inventory = store.inventory(7, MODEL_REVISION)
    after_identity = store.document_identities(7, MODEL_REVISION, [1])
    store.close()

    assert initial_response.json()["status"] == "updated"
    assert initial_response.json()["index_revision"] == "r1"
    assert conflict_response.json()["status"] == "identity_conflict"
    assert conflict_response.json()["reason"] == ("immutable_chunk_content_hash_conflict")
    assert conflict_response.json()["accepted"] == 0
    assert conflict_response.json()["index_revision"] == "r1"
    assert after_inventory.index_revision == before_inventory.index_revision == "r1"
    assert after_inventory.identity_digest == before_inventory.identity_digest
    assert (
        after_identity
        == before_identity
        == {
            1: ("1.py", _hash("original")),
        }
    )
