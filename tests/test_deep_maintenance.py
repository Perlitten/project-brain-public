from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain.config.settings import settings
from brain.workers.errors import PermanentJobError
from brain.workers.tasks import run_deep_context, run_nightly_maintenance
import brain.workers.tasks as worker_tasks


def _repository():
    return SimpleNamespace(id=3, path="/app", name="project-brain")


def test_auto_heal_repairs_incompatible_embeddings(tmp_path):
    record = SimpleNamespace(id=9, path=str(tmp_path))
    indexer = SimpleNamespace(index_repository=AsyncMock(return_value=record))
    inventory = SimpleNamespace(to_dict=lambda: {"missing_embeddings": 0, "stale_embeddings": 0, "incompatible_embeddings": 1})
    repair = SimpleNamespace(to_dict=lambda: {"regenerated": 1})
    with (
        patch.object(worker_tasks, "FileIndexer", return_value=indexer),
        patch.object(worker_tasks, "list_all_repositories", AsyncMock(return_value=[record])),
        patch.object(worker_tasks, "assess_repository_freshness", AsyncMock(return_value={"status": "current"})),
        patch.object(worker_tasks, "collect_embedding_inventory", AsyncMock(return_value=inventory)),
        patch.object(worker_tasks, "backfill_embeddings", AsyncMock(return_value=repair)) as backfill,
    ):
        result = asyncio.run(worker_tasks.auto_heal_stale_repositories())
    indexer.index_repository.assert_awaited_once_with(tmp_path, clean=False)
    backfill.assert_awaited_once_with(record.id)
    assert result["healed"][0]["incompatible_embeddings"] == 1


def test_auto_heal_excludes_nightly_target_before_reading_inventory(tmp_path):
    record = SimpleNamespace(id=9, path=str(tmp_path))
    indexer = SimpleNamespace(index_repository=AsyncMock())
    with (
        patch.object(worker_tasks, "FileIndexer", return_value=indexer),
        patch.object(worker_tasks, "list_all_repositories", AsyncMock(return_value=[record])),
        patch.object(worker_tasks, "assess_repository_freshness", AsyncMock()) as freshness,
        patch.object(worker_tasks, "collect_embedding_inventory", AsyncMock()) as inventory,
    ):
        result = asyncio.run(worker_tasks.auto_heal_stale_repositories(exclude_repository_id=record.id))
    freshness.assert_not_awaited()
    inventory.assert_not_awaited()
    indexer.index_repository.assert_not_awaited()
    assert result["skipped"] == [{"path": record.path, "reason": "handled_by_nightly_target"}]


def _dense_result(*, passed: bool = True):
    return {
        "pass": passed,
        "chunks_with_current_embeddings": 1735 if passed else 1733,
        "total_eligible_chunks": 1735,
        "pgvector_coverage_pct": 100.0 if passed else 99.9,
    }


def _quality_probe(*, passed: bool = True):
    return {
        "query_hash": "a" * 64,
        "status": "scored",
        "applied": True,
        "coverage": 1.0,
        "expected_paths": ["brain/workers/tasks.py"],
        "matched_expected": (
            ["brain/workers/tasks.py"] if passed else []
        ),
        "top_paths": ["brain/workers/tasks.py"],
        "passed": passed,
    }


@pytest.mark.asyncio
async def test_scheduled_nightly_job_respects_feature_gate(monkeypatch):
    monkeypatch.setattr(
        settings,
        "NIGHTLY_DEEP_MAINTENANCE_ENABLED",
        False,
    )

    result = await run_nightly_maintenance({"scheduled": True})

    assert result == {
        "status": "skipped",
        "reason": "NIGHTLY_DEEP_MAINTENANCE_ENABLED=false",
        "scheduled": True,
    }


@pytest.mark.asyncio
async def test_nightly_job_repairs_dense_corpus_and_proves_lfm_quality():
    record = _repository()
    exact_client = SimpleNamespace(aclose=AsyncMock())
    redis_set = AsyncMock()
    probes = [_quality_probe() for _ in range(3)]

    with (
        patch("brain.workers.tasks.init_db", new=AsyncMock()),
        patch("brain.workers.tasks.auto_heal_stale_repositories", new=AsyncMock(return_value={"healed": [], "skipped": []})) as sweep,
        patch(
            "brain.workers.tasks._deep_repo_path",
            return_value=Path("/app"),
        ),
        patch(
            "brain.workers.tasks._approved_deep_repository",
            new=AsyncMock(return_value=record),
        ),
        patch(
            "brain.workers.tasks.run_reindex",
            new=AsyncMock(
                return_value={
                    "status": "success",
                    "commit_hash": "build-sha",
                }
            ),
        ) as reindex,
        patch(
            "brain.workers.tasks.verify_embeddings",
            new=AsyncMock(
                side_effect=[
                    _dense_result(passed=False),
                    _dense_result(),
                ]
            ),
        ),
        patch(
            "brain.workers.tasks.run_embedding_backfill",
            new=AsyncMock(
                return_value={
                    "regenerated": 2,
                    "failed": 0,
                }
            ),
        ) as backfill,
        patch(
            "brain.workers.tasks._reconcile_exact_lfm",
            new=AsyncMock(
                return_value=(
                    {
                        "passed": True,
                        "verification_mode": "exact",
                        "verified_documents": 1735,
                        "index_revision": "r3000",
                    },
                    exact_client,
                )
            ),
        ),
        patch(
            "brain.workers.tasks._run_deep_quality_probes",
            new=AsyncMock(return_value=probes),
        ),
        patch(
            "brain.workers.tasks.deliver_maintenance_alert",
            new=AsyncMock(return_value={"status": "sent"}),
        ),
        patch(
            "brain.workers.tasks.redis_client.set",
            new=redis_set,
        ),
    ):
        result = await run_nightly_maintenance(
            {
                "repo_path": "/app",
                "notify": True,
            }
        )

    assert result["status"] == "completed"
    sweep.assert_awaited_once_with(exclude_repository_id=record.id)
    reindex.assert_awaited_once()
    assert result["dense_after"]["pass"] is True
    assert result["late_interaction"]["verification_mode"] == "exact"
    assert all(probe["passed"] for probe in result["quality_probes"])
    backfill.assert_awaited_once()
    exact_client.aclose.assert_awaited_once()
    redis_set.assert_awaited_once()


@pytest.mark.asyncio
async def test_nightly_job_fails_closed_on_relevance_probe_miss():
    record = _repository()
    exact_client = SimpleNamespace(aclose=AsyncMock())

    with (
        patch("brain.workers.tasks.init_db", new=AsyncMock()),
        patch(
            "brain.workers.tasks._deep_repo_path",
            return_value=Path("/app"),
        ),
        patch(
            "brain.workers.tasks._approved_deep_repository",
            new=AsyncMock(return_value=record),
        ),
        patch(
            "brain.workers.tasks.run_reindex",
            new=AsyncMock(return_value={"commit_hash": "build-sha"}),
        ),
        patch(
            "brain.workers.tasks.verify_embeddings",
            new=AsyncMock(return_value=_dense_result()),
        ),
        patch(
            "brain.workers.tasks._reconcile_exact_lfm",
            new=AsyncMock(return_value=({"passed": True}, exact_client)),
        ),
        patch(
            "brain.workers.tasks._run_deep_quality_probes",
            new=AsyncMock(return_value=[_quality_probe(passed=False)]),
        ),
        patch(
            "brain.workers.tasks.redis_client.set",
            new=AsyncMock(),
        ) as redis_set,
    ):
        with pytest.raises(
            PermanentJobError,
            match="quality probe failed closed",
        ):
            await run_nightly_maintenance({"repo_path": "/app"})

    exact_client.aclose.assert_awaited_once()
    redis_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_deep_context_returns_only_after_applied_exact_lfm(monkeypatch):
    monkeypatch.setattr(settings, "LATE_INTERACTION_DEEP_ENABLED", True)
    record = _repository()
    exact_client = SimpleNamespace(aclose=AsyncMock())
    builder = MagicMock()
    builder.build_context_pack = AsyncMock(
        return_value={
            "id": 91,
            "critic_status": "COMPLETE",
            "created_at": datetime(
                2026,
                7,
                30,
                tzinfo=timezone.utc,
            ),
            "markdown_content": "large private payload",
            "retrieved_files": [{"path": "brain/workers/tasks.py"}],
            "retrieval_debug": {
                "late_interaction": {
                    "status": "scored",
                    "applied": True,
                    "coverage": 1.0,
                }
            },
        }
    )

    with (
        patch("brain.workers.tasks.init_db", new=AsyncMock()),
        patch(
            "brain.workers.tasks._deep_repo_path",
            return_value=Path("/app"),
        ),
        patch(
            "brain.workers.tasks._approved_deep_repository",
            new=AsyncMock(return_value=record),
        ),
        patch(
            "brain.workers.tasks.assess_repository_freshness",
            new=AsyncMock(
                return_value={
                    "status": "current",
                    "checked_at": datetime(
                        2026,
                        7,
                        30,
                        1,
                        tzinfo=timezone.utc,
                    ),
                }
            ),
        ),
        patch(
            "brain.workers.tasks.verify_embeddings",
            new=AsyncMock(return_value=_dense_result()),
        ),
        patch(
            "brain.workers.tasks._reconcile_exact_lfm",
            new=AsyncMock(
                return_value=(
                    {
                        "passed": True,
                        "verification_mode": "exact",
                    },
                    exact_client,
                )
            ),
        ),
        patch(
            "brain.context.context_pack_builder.ContextPackBuilder",
            return_value=builder,
        ),
        patch(
            "brain.workers.tasks.deliver_deep_context_alert",
            new=AsyncMock(return_value={"status": "sent"}),
        ),
    ):
        result = await run_deep_context(
            {
                "task_description": "inspect exact LFM maintenance",
                "repo_path": "/app",
                "notify": True,
            }
        )

    assert result["status"] == "completed"
    assert "markdown_content" not in result["context_pack"]
    assert result["context_pack"]["retrieval_debug"][
        "late_interaction"
    ]["applied"]
    assert result["context_pack"]["created_at"] == "2026-07-30 00:00:00+00:00"
    assert (
        result["repository"]["freshness"]["checked_at"]
        == "2026-07-30 01:00:00+00:00"
    )
    json.dumps(result)
    exact_client.aclose.assert_awaited_once()
    builder.build_context_pack.assert_awaited_once_with(
        task_description="inspect exact LFM maintenance",
        repo_path=Path("/app"),
        budget="deep",
        retrieval_mode="deep",
        late_interaction_client=exact_client,
    )
