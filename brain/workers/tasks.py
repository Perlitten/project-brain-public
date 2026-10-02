"""Background job handlers — delegate to existing brain modules."""

from __future__ import annotations

import hashlib
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger
from sqlalchemy import select, update, func

from brain.alerts.telegram import (
    deliver_deep_context_alert,
    deliver_diagnosis_alert,
    deliver_maintenance_alert,
)
from brain.config.paths import resolve_repo_path
from brain.config.settings import settings
from brain.context.evaluator import GoldenTaskEvaluator
from brain.database.repository_utils import get_repository_by_path, list_all_repositories
from brain.database.session import check_health, init_db
from brain.database.session import async_session_factory
from brain.database.models import Repository, IndexingRun
from brain.embeddings.backfill import backfill_embeddings
from brain.embeddings.integrity import collect_embedding_inventory, verify_embeddings
from brain.indexers.file_indexer import FileIndexer, _scan_repo_files, source_content_digest
from brain.indexers.repo_indexer import get_git_commit_hash
from brain.indexers.reliability import repository_index_lock, git_source_is_clean
from brain.insights.proactive import generate_proactive_insights
from brain.late_interaction.client import (
    build_maintenance_late_interaction_client,
)
from brain.late_interaction.remote_sync import sync_remote_late_interaction
from brain.database.session import redis_client
from brain.memory.harness_store import HarnessStore
from brain.memory.repo_freshness import assess_repository_freshness
from brain.memory.source_manifest import revision_matches
from brain.workers.runtime import current_job, check_job_lease
from brain.workers.db_fencing import (
    DbFence,
    check_db_fence_open,
    current_job_fence,
    assert_current_db_fence,
)
from brain.workers.errors import PermanentJobError, StaleWorkerFencingError

# StaleWorkerFencingError moved to brain.workers.errors; the import above
# keeps `from brain.workers.tasks import StaleWorkerFencingError` working.


async def validate_task_fencing(redis_cli, prefix: str, job_id: str, fencing_token: Optional[str]) -> None:
    if not fencing_token:
        return
    lease_key = f"{prefix}:job:{job_id}:lease"
    active_token = await redis_cli.hget(lease_key, "fencing_token")
    if isinstance(active_token, bytes):
        active_token = active_token.decode()
    if active_token != fencing_token:
        raise StaleWorkerFencingError(
            f"Fencing mismatch for job {job_id}: active_token={active_token}, worker_token={fencing_token}. "
            "Job lease was claimed by another worker; aborting database mutation."
        )


_NIGHTLY_QUALITY_PROBES = (
    (
        "where the worker enqueues and executes durable background jobs",
        frozenset(
            {
                "brain/workers/queue.py",
                "brain/workers/worker.py",
                "brain/workers/tasks.py",
            }
        ),
    ),
    (
        "how repository indexing verifies and repairs semantic embeddings",
        frozenset(
            {
                "brain/embeddings/backfill.py",
                "brain/embeddings/integrity.py",
                "brain/workers/tasks.py",
            }
        ),
    ),
    (
        "как синхронизируется LFM late interaction корпус и применяется reranking",
        frozenset(
            {
                "brain/late_interaction/application.py",
                "brain/late_interaction/remote_sync.py",
                "brain/workers/tasks.py",
            }
        ),
    ),
)


def _deep_repo_path(params: Dict[str, Any]) -> Path:
    return resolve_repo_path(
        params.get("repo_path")
        or settings.NIGHTLY_DEEP_MAINTENANCE_REPO_PATH
    )


async def _approved_deep_repository(repo_path: Path):
    record = await get_repository_by_path(repo_path)
    if record is None:
        raise PermanentJobError(
            f"Deep repository is not indexed: {repo_path.as_posix()}"
        )
    if record.id != settings.LATE_INTERACTION_APPROVED_REPOSITORY_ID:
        raise PermanentJobError(
            "Deep repository does not match the approved LFM repository",
            result={
                "repository_id": record.id,
                "repository_path": record.path,
            },
        )
    return record


async def _reconcile_exact_lfm(repository_id: int):
    """Synchronize, prove exact equality, then return an exact scoring client."""
    maintenance_client = build_maintenance_late_interaction_client(
        repository_id=repository_id,
    )
    try:
        sync = await sync_remote_late_interaction(
            repository_id,
            dry_run=False,
            prune=True,
            client=maintenance_client,
        )
    finally:
        await maintenance_client.aclose()

    sync_result = sync.to_dict()
    exact = (
        sync.passed
        and sync.verification_mode == "exact"
        and bool(sync.index_revision)
        and bool(sync.source_identity_digest)
        and sync.source_identity_digest == sync.remote_identity_digest
        and sync.verified_documents > 0
    )
    if not exact:
        raise PermanentJobError(
            "LFM exact corpus reconciliation failed",
            result={"late_interaction": sync_result},
        )

    exact_client = build_maintenance_late_interaction_client(
        repository_id=repository_id,
        index_revision=sync.index_revision,
        identity_digest=sync.remote_identity_digest,
        document_count=sync.verified_documents,
    )
    status = await exact_client.status(repository_id)
    if status.status != "ready":
        await exact_client.aclose()
        raise PermanentJobError(
            f"LFM exact release is not ready: {status.reason or status.status}",
            result={
                "late_interaction": sync_result,
                "release_status": status.__dict__,
            },
        )
    return sync_result, exact_client


async def _run_deep_quality_probes(
    repo_path: str,
    exact_client,
) -> list[dict[str, Any]]:
    from brain.search.code_search import search_code

    probes: list[dict[str, Any]] = []
    limit = min(
        settings.NIGHTLY_DEEP_MAINTENANCE_QUALITY_QUERIES,
        len(_NIGHTLY_QUALITY_PROBES),
    )
    for query, expected_paths in _NIGHTLY_QUALITY_PROBES[:limit]:
        result = await search_code(
            query,
            limit=10,
            repo_path=repo_path,
            retrieval_mode="deep",
            late_interaction_client=exact_client,
        )
        late = result.get("late_interaction") or {}
        top_paths = [
            chunk.get("file_path")
            for chunk in result.get("chunks", [])[:10]
            if chunk.get("file_path")
        ]
        matched_expected = sorted(expected_paths.intersection(top_paths))
        passed = bool(
            late.get("status") == "scored"
            and late.get("applied")
            and float(late.get("coverage") or 0.0) >= 1.0
            and matched_expected
        )
        probes.append(
            {
                "query_hash": hashlib.sha256(
                    query.encode("utf-8")
                ).hexdigest(),
                "status": late.get("status"),
                "applied": bool(late.get("applied")),
                "coverage": late.get("coverage"),
                "latency_ms": late.get("latency_ms"),
                "candidate_count": late.get("candidate_count"),
                "expected_paths": sorted(expected_paths),
                "matched_expected": matched_expected,
                "top_paths": top_paths,
                "passed": passed,
            }
        )
    return probes


async def run_reindex(params: Dict[str, Any]) -> Dict[str, Any]:
    repo_path = resolve_repo_path(params.get("repo_path"))
    clean = bool(params.get("clean", False))
    await init_db()
    indexer = FileIndexer()
    expected_revision = str(params.get("source_revision") or "").strip()
    repo = await indexer.index_repository(repo_path, clean=clean, expected_revision=expected_revision)
    actual_revision = str(repo.last_indexed_commit or "").strip()
    if expected_revision and not revision_matches(expected_revision, actual_revision):
        raise PermanentJobError(
            "Indexed source revision mismatch: "
            f"expected={expected_revision[:16]} actual={actual_revision[:16] or 'missing'}",
            result={
                "repo_id": repo.id,
                "path": repo.path,
                "commit_hash": actual_revision,
                "source_revision_verified": False,
            },
        )

    result: Dict[str, Any] = {
        "repo_id": repo.id,
        "path": repo.path,
        "commit_hash": actual_revision,
        "source_revision_verified": bool(expected_revision and revision_matches(expected_revision, actual_revision)),
        **indexer.verification,
    }
    if result.get("status") == "failed":
        raise PermanentJobError("Repository projection or source verification failed", result=result)
    counts = result.get("counts") or {}
    if (result.get("status") == "degraded" and not (counts.get("files") or {}).get("failed")
            and not (counts.get("graph") or {}).get("failed")):
        repair = {"repo_path": repo.path, "repository_id": repo.id,
                  "source_revision": actual_revision, "indexing_run_id": indexer.indexing_run_id,
                  "limit": settings.INDEX_REPAIR_CHUNK_LIMIT, "batch_size": settings.INDEX_EMBEDDING_BATCH_SIZE}
        result["repair_request"] = repair
        runtime = current_job.get()
        if runtime is not None:
            result["repair_job_id"] = await runtime.enqueue_repair(repair)
        async with async_session_factory() as session:
            async with session.begin():
                await assert_current_db_fence(session)
                await session.execute(update(IndexingRun).where(IndexingRun.id == indexer.indexing_run_id)
                                      .values(verification=result))
    if params.get("benchmark_after"):
        result["benchmark"] = await run_benchmark(
            {
                "repo_path": repo.path,
                "golden": params.get("golden", "rules/golden_tasks.yaml"),
                "smoke": True,
            }
        )
    return result


async def run_health_check(params: Dict[str, Any]) -> Dict[str, Any]:
    harness_reconciliation = None
    try:
        await init_db()
        if settings.HARNESS_STALE_REAPER_ENABLED:
            harness_reconciliation = await HarnessStore.reconcile_stale_tasks(
                limit=settings.HARNESS_STALE_REAPER_BATCH_SIZE,
            )
    except Exception as exc:
        harness_reconciliation = {
            "error": f"{type(exc).__name__}: {exc}"[:260],
        }
    services = await check_health()
    all_healthy = all(svc.get("status") == "healthy" for svc in services.values())

    repo_path = resolve_repo_path(params.get("repo_path") or settings.TARGET_REPO_PATH)
    try:
        record = await get_repository_by_path(repo_path)
        inventory = await collect_embedding_inventory(
            record.id if record else None,
            repo_path.as_posix(),
        )
        inventory_dict = inventory.to_dict()
    except Exception as exc:
        inventory_dict = {
            "missing_embeddings": None,
            "stale_embeddings": None,
            "incompatible_embeddings": None,
            "pgvector_coverage_pct": None,
            "collection_error": f"{type(exc).__name__}: {exc}"[:260],
        }
    stale = inventory_dict.get("stale_embeddings", 0)
    missing = inventory_dict.get("missing_embeddings", 0)

    coverage_clean = stale == 0 and missing == 0 and "collection_error" not in inventory_dict
    return {
        "healthy": all_healthy,
        "coverage_clean": coverage_clean,
        "services": services,
        "embeddings": inventory_dict,
        "stale_embeddings": stale,
        "missing_embeddings": missing,
        "harness_reconciliation": harness_reconciliation,
    }


async def run_embedding_verify(params: Dict[str, Any]) -> Dict[str, Any]:
    await init_db()
    repo_path = resolve_repo_path(params.get("repo_path") or settings.TARGET_REPO_PATH)
    record = await get_repository_by_path(repo_path)
    return await verify_embeddings(
        record.id if record else None,
        repo_path.as_posix(),
    )


async def run_embedding_backfill(params: Dict[str, Any]) -> Dict[str, Any]:
    await init_db()
    repo_path = resolve_repo_path(params.get("repo_path") or settings.TARGET_REPO_PATH)
    record = await get_repository_by_path(repo_path)
    if params.get("indexing_run_id"):
        if record is None or record.id != params.get("repository_id"):
            raise PermanentJobError("Repair repository identity mismatch")
    if record is not None:
        async with repository_index_lock(record.path):
            return await _run_embedding_backfill(params, record, repo_path)
    return await _run_embedding_backfill(params, record, repo_path)


async def _run_embedding_backfill(params, record, repo_path) -> Dict[str, Any]:
    repository_id = record.id if record else None
    if params.get("indexing_run_id") and get_git_commit_hash(repo_path) != params.get("source_revision"):
        raise PermanentJobError("Repair source revision mismatch")
    await check_job_lease()
    result = await backfill_embeddings(
        repository_id,
        pgvector_only=bool(params.get("pgvector_only", False)),
        limit=params.get("limit"),
        batch_size=int(params.get("batch_size") or 8),
        provider_name=params.get("provider"),
    )
    inventory = await collect_embedding_inventory(
        repository_id,
        record.path if record else repo_path.as_posix(),
        fast=True,
    )
    response = {
        **result.to_dict(),
        "coverage_after": inventory.to_dict(),
    }
    if params.get("indexing_run_id"):
        verification = await verify_embeddings(repository_id, record.path)
        source_files = await asyncio.to_thread(_scan_repo_files, repo_path)
        source_clean = await asyncio.to_thread(git_source_is_clean, repo_path, source_files)
        content_digest = await asyncio.to_thread(source_content_digest, repo_path, source_files)
        response.update(embedding_verification=verification, causal_parent_job_id=params.get("causal_parent_job_id"),
                        indexing_run_id=params["indexing_run_id"], status="degraded")
        await check_job_lease()
        async with async_session_factory() as session:
            async with session.begin():
                await assert_current_db_fence(session)
                repo = (await session.execute(select(Repository).where(Repository.id == repository_id)
                                               .with_for_update())).scalar_one()
                run = (await session.execute(select(IndexingRun).where(IndexingRun.id == params["indexing_run_id"],
                                                                      IndexingRun.repository_id == repository_id))).scalar_one()
                latest = (await session.execute(select(func.max(IndexingRun.id))
                                                .where(IndexingRun.repository_id == repository_id))).scalar()
                exact = (repo.last_indexed_commit == params["source_revision"] == run.commit_hash
                         and get_git_commit_hash(repo_path) == params["source_revision"] and source_clean
                         and (run.verification or {}).get("source_content_digest", content_digest) == content_digest)
                counts = (run.verification or {}).get("counts") or {}
                projection_ok = not (counts.get("files", {}).get("failed") or counts.get("graph", {}).get("failed"))
                if latest == run.id and run.status in {"degraded", "completed"} and exact and projection_ok and verification.get("pass"):
                    runtime = current_job.get()
                    run.status = repo.indexing_status = "completed"
                    run.verification = {**(run.verification or {}), "status": "completed",
                                        "embedding_verification": verification, "repair": response}
                    run.progress = {**(run.progress or {}), "phase": "completed", "repair_job_id":
                                    runtime.job_id if runtime else None}
                    run.updated_at = datetime.now(timezone.utc)
                    run.completed_at = run.updated_at
                    response["status"] = "completed"
                else:
                    response["promotion_skipped"] = True
    return response


async def run_benchmark(params: Dict[str, Any]) -> Dict[str, Any]:
    repo_path = resolve_repo_path(params.get("repo_path"))
    golden = params.get("golden", "rules/golden_tasks.yaml")
    smoke = bool(params.get("smoke", False))

    golden_path = Path(golden)
    if not golden_path.is_absolute():
        candidate = repo_path / golden
        golden_path = candidate if candidate.exists() else Path.cwd() / golden

    evaluator = GoldenTaskEvaluator(golden_path=golden_path if golden_path.exists() else None)
    if smoke and evaluator.golden_tasks:
        evaluator.golden_tasks = evaluator.golden_tasks[:1]
        logger.info("Smoke benchmark: running first golden task only")

    return await evaluator.run_evaluation(repo_path)


async def run_proactive_insights(params: Dict[str, Any]) -> Dict[str, Any]:
    if params.get("scheduled") and not settings.PROACTIVE_INSIGHTS_ENABLED:
        return {
            "status": "skipped",
            "reason": "PROACTIVE_INSIGHTS_ENABLED=false",
            "scheduled": True,
        }
    try:
        await init_db()
    except Exception as exc:
        logger.warning(f"Proactive diagnostic schema init unavailable: {type(exc).__name__}")
    return await generate_proactive_insights(
        repo_path=params.get("repo_path"),
        use_llm=params.get("use_llm"),
        persist=True,
    )


async def run_self_diagnosis(params: Dict[str, Any]) -> Dict[str, Any]:
    if params.get("scheduled") and not settings.SELF_DIAGNOSIS_ENABLED:
        return {
            "status": "skipped",
            "reason": "SELF_DIAGNOSIS_ENABLED=false",
            "scheduled": True,
        }
    try:
        await init_db()
    except Exception as exc:
        logger.warning(f"Self-diagnosis schema init unavailable: {type(exc).__name__}")
    use_llm = params.get("use_llm")
    if use_llm is None:
        use_llm = settings.SELF_DIAGNOSIS_USE_LLM
    diagnosis = await generate_proactive_insights(
        repo_path=params.get("repo_path"),
        use_llm=bool(use_llm),
        persist=True,
        trigger_context=params.get("trigger_context"),
    )
    insights = list((diagnosis.get("persisted") or {}).get("insights") or [])
    delivery = await deliver_diagnosis_alert(
        insights,
        generated_at=str(diagnosis.get("generated_at") or ""),
        llm=diagnosis.get("llm") or {},
    )
    result = {
        **diagnosis,
        "telegram": delivery,
        "status": (
            "degraded"
            if delivery.get("status") in {"failed", "disabled"}
            or diagnosis.get("llm", {}).get("status") == "failed"
            else "completed"
        ),
    }
    await redis_client.set(
        "brain:self-diagnosis:last-result",
        json.dumps(result, ensure_ascii=False, default=str),
        ex=max(settings.SELF_DIAGNOSIS_RESULT_TTL_SECONDS, 60),
    )
    if delivery.get("status") == "failed":
        raise RuntimeError(f"Self-diagnosis completed but Telegram delivery is unavailable: {delivery.get('status')}")
    return result


async def auto_heal_stale_repositories() -> dict[str, Any]:
    """Iterate registered repositories and autonomously reindex/repair any stale or behind ones."""
    healed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    indexer = FileIndexer()
    try:
        repos = await list_all_repositories()
        for repo in repos:
            repo_p = Path(repo.path)
            if not repo_p.exists():
                skipped.append({"path": repo.path, "reason": "source_missing"})
                continue
            freshness = await assess_repository_freshness(repo)
            status = freshness.get("status")
            inventory = await collect_embedding_inventory(repo.id, repo_p.as_posix(), fast=True)
            inv_dict = inventory.to_dict()
            missing = inv_dict.get("missing_embeddings") or 0
            stale = inv_dict.get("stale_embeddings") or 0

            if status in ("behind", "stale", "unindexed") or missing > 0 or stale > 0:
                logger.info(f"Autonomous Self-Healing: reindexing {repo.path} (status={status}, missing={missing}, stale={stale})")
                indexed_repo = await indexer.index_repository(repo_p, clean=False)
                backfill_res = await backfill_embeddings(indexed_repo.id)
                healed.append({
                    "path": repo.path,
                    "previous_status": status,
                    "backfill": backfill_res.to_dict(),
                })
            else:
                skipped.append({"path": repo.path, "status": status})
    except Exception as exc:
        logger.warning(f"Autonomous Self-Healing sweep encountered an issue: {exc}")
    return {"healed": healed, "skipped": skipped}


async def run_nightly_maintenance(params: Dict[str, Any]) -> Dict[str, Any]:
    if (
        params.get("scheduled")
        and not settings.NIGHTLY_DEEP_MAINTENANCE_ENABLED
    ):
        return {
            "status": "skipped",
            "reason": "NIGHTLY_DEEP_MAINTENANCE_ENABLED=false",
            "scheduled": True,
        }

    await init_db()

    # Autonomous self-healing sweep across all registered repositories
    auto_heal_result = await auto_heal_stale_repositories()

    repo_path = _deep_repo_path(params)
    record = await _approved_deep_repository(repo_path)

    reindex = await run_reindex(
        {
            "repo_path": record.path,
            "clean": False,
            "verify_after": False,
        }
    )
    dense_before = await verify_embeddings(record.id, record.path)
    embedding_repair: dict[str, Any] = {
        "pgvector_synced": 0,
        "regenerated": 0,
        "failed": 0,
        "skipped": 0,
    }
    if not dense_before.get("pass"):
        embedding_repair = await run_embedding_backfill(
            {
                "repo_path": record.path,
                "batch_size": 8,
            }
        )
    dense_after = await verify_embeddings(record.id, record.path)
    if not dense_after.get("pass"):
        raise PermanentJobError(
            "Dense embedding verification failed after nightly repair",
            result={
                "reindex": reindex,
                "dense_before": dense_before,
                "embedding_repair": embedding_repair,
                "dense_after": dense_after,
            },
        )

    late_sync, exact_client = await _reconcile_exact_lfm(record.id)
    try:
        quality_probes = await _run_deep_quality_probes(
            record.path,
            exact_client,
        )
    finally:
        await exact_client.aclose()
    if not quality_probes or any(
        not probe.get("passed") for probe in quality_probes
    ):
        raise PermanentJobError(
            "Nightly deep quality probe failed closed",
            result={
                "reindex": reindex,
                "dense_after": dense_after,
                "late_interaction": late_sync,
                "quality_probes": quality_probes,
            },
        )

    notify = bool(
        params.get(
            "notify",
            settings.NIGHTLY_DEEP_MAINTENANCE_NOTIFY,
        )
    )
    telegram = (
        await deliver_maintenance_alert(
            repo_path=record.path,
            dense_verification=dense_after,
            late_sync=late_sync,
            quality_probes=quality_probes,
            repaired_embeddings=int(
                embedding_repair.get("regenerated") or 0
            ),
        )
        if notify
        else {"status": "disabled", "reason": "notify=false"}
    )
    result = {
        "status": "completed",
        "repository": {
            "id": record.id,
            "path": record.path,
            "commit_hash": reindex.get("commit_hash"),
        },
        "reindex": reindex,
        "dense_before": dense_before,
        "embedding_repair": embedding_repair,
        "dense_after": dense_after,
        "late_interaction": late_sync,
        "quality_probes": quality_probes,
        "telegram": telegram,
        "auto_heal": auto_heal_result,
    }
    await redis_client.set(
        "brain:nightly-maintenance:last-result",
        json.dumps(result, ensure_ascii=False, default=str),
        ex=30 * 24 * 60 * 60,
    )
    return result


async def run_deep_context(params: Dict[str, Any]) -> Dict[str, Any]:
    if not settings.LATE_INTERACTION_DEEP_ENABLED:
        raise PermanentJobError("LFM deep lane is disabled")

    task_description = str(params.get("task_description") or "").strip()
    if not task_description:
        raise PermanentJobError("Deep context task_description is required")

    await init_db()
    repo_path = _deep_repo_path(params)
    record = await _approved_deep_repository(repo_path)
    freshness = await assess_repository_freshness(record)
    dense = await verify_embeddings(record.id, record.path)
    if freshness.get("status") != "current" or not dense.get("pass"):
        await run_nightly_maintenance(
            {
                "repo_path": record.path,
                "scheduled": False,
                "notify": False,
            }
        )
        record = await _approved_deep_repository(repo_path)
        freshness = await assess_repository_freshness(record)
        dense = await verify_embeddings(record.id, record.path)
    if freshness.get("status") != "current" or not dense.get("pass"):
        raise PermanentJobError(
            "Deep context prerequisites are not current",
            result={
                "freshness": freshness,
                "dense": dense,
            },
        )

    late_sync, exact_client = await _reconcile_exact_lfm(record.id)
    try:
        from brain.context.context_pack_builder import ContextPackBuilder

        context_pack = await ContextPackBuilder().build_context_pack(
            task_description=task_description,
            repo_path=repo_path,
            budget="deep",
            retrieval_mode="deep",
            late_interaction_client=exact_client,
        )
    finally:
        await exact_client.aclose()

    context_pack.pop("markdown_content", None)
    # Context packs contain ORM timestamps.  JobQueue persists handler results
    # as JSON, so normalize the public payload here instead of letting an
    # otherwise successful Deep job fail during the final status update.
    context_pack = json.loads(
        json.dumps(context_pack, ensure_ascii=False, default=str)
    )
    late_debug = (
        (context_pack.get("retrieval_debug") or {}).get(
            "late_interaction"
        )
        or {}
    )
    if late_debug.get("status") != "scored" or not late_debug.get("applied"):
        raise PermanentJobError(
            "Deep context completed without an applied LFM ranking",
            result={
                "context_pack_id": context_pack.get("id"),
                "late_interaction": late_debug,
            },
        )

    notify = bool(
        params.get("notify", settings.LATE_INTERACTION_DEEP_NOTIFY)
    )
    telegram = (
        await deliver_deep_context_alert(
            repo_path=record.path,
            context_pack_id=context_pack.get("id"),
            critic_status=str(context_pack.get("critic_status") or ""),
            late_status=str(late_debug.get("status") or ""),
            retrieved_files=len(context_pack.get("retrieved_files") or []),
        )
        if notify
        else {"status": "disabled", "reason": "notify=false"}
    )
    result = {
        "status": "completed",
        "repository": {
            "id": record.id,
            "path": record.path,
            "freshness": freshness,
        },
        "dense": dense,
        "late_interaction": late_sync,
        "context_pack": context_pack,
        "telegram": telegram,
    }
    return json.loads(
        json.dumps(result, ensure_ascii=False, default=str)
    )


async def execute_job(
    job_type: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    job_id: Optional[str] = None,
    fencing_token: Optional[str] = None,
    lease_prefix: Optional[str] = None,
) -> Dict[str, Any]:
    """Dispatch a job to its handler, enforcing lease ownership.

    When the worker passes ``job_id`` + ``fencing_token``, ownership is
    verified before dispatch (Redis fast path) and again — durably, in
    Postgres via ``check_db_fence_open`` — before the result is returned, so
    a worker whose lease was claimed by another cannot commit its outcome.
    ``current_job_fence`` also carries the fence to any handler-internal
    transaction that opts into ``assert_current_db_fence``.
    """
    fence_token_ctx = None
    if fencing_token and job_id:
        await validate_task_fencing(
            redis_client, lease_prefix or settings.WORKER_REDIS_PREFIX, job_id, fencing_token
        )
        fence_token_ctx = current_job_fence.set(
            DbFence(job_id=job_id, fencing_token=fencing_token)
        )
    params = params or {}
    handlers = {
        "reindex": run_reindex,
        "health_check": run_health_check,
        "embedding_verify": run_embedding_verify,
        "embedding_backfill": run_embedding_backfill,
        "benchmark": run_benchmark,
        "proactive_insights": run_proactive_insights,
        "self_diagnosis": run_self_diagnosis,
        "nightly_maintenance": run_nightly_maintenance,
        "deep_context": run_deep_context,
    }
    handler = handlers.get(job_type)
    if handler is None:
        if fence_token_ctx is not None:
            current_job_fence.reset(fence_token_ctx)
        raise ValueError(f"Unknown job type: {job_type}")
    try:
        result = await handler(params)
    finally:
        if fence_token_ctx is not None:
            current_job_fence.reset(fence_token_ctx)
    if fencing_token and job_id:
        # Durable check in Postgres — a Redis read alone cannot protect the
        # result/status commit that follows this return.
        await check_db_fence_open(job_id=job_id, fencing_token=fencing_token)
    return result
