"""Background job handlers — delegate to existing brain modules."""

from __future__ import annotations

import hashlib
import asyncio
import json
import tempfile
from dataclasses import dataclass
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
from brain.indexers.source_mirror import (
    export_commit,
    fetch_into_mirror,
    mirror_path,
    mirror_root,
    resolve_commit,
    resolve_origin_spec,
)
from brain.indexers.source_sync import SourceSyncError, sync_checkout_to_revision
from brain.insights.proactive import generate_proactive_insights
from brain.late_interaction.client import (
    build_maintenance_late_interaction_client,
)
from brain.late_interaction.remote_sync import sync_remote_late_interaction
from brain.database.session import redis_client
from brain.memory.harness_store import HarnessStore
from brain.memory.origin_state_store import get_origin_state, upsert_origin_state
from brain.memory.repo_freshness import assess_repository_freshness
from brain.memory.source_manifest import revision_matches
from brain.workers.origin_poll import run_origin_poll
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


async def run_enrich_summaries(params: Dict[str, Any]) -> Dict[str, Any]:
    from brain.indexers.summary_enrichment import enrich_summaries

    await init_db()
    path = resolve_repo_path(params.get("repo_path"))
    repository = await get_repository_by_path(path)
    if repository is None or repository.id != params.get("repository_id"):
        raise PermanentJobError("Summary enrichment repository scope does not match")
    if settings.INDEX_LLM_SUMMARIES_MODE != "lazy" or not settings.INDEX_LLM_SUMMARIES_BACKGROUND:
        return {"skipped": "summary enrichment is disabled"}
    return await enrich_summaries(repository.id, path, params.get("paths"))


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
        "how the LFM late interaction corpus is synced and reranking applied",
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


# A checkout that cannot be moved for one of these reasons can still follow
# origin through a Brain-owned mirror (BRAIN_SOURCE_MIRROR_DIR).
_MIRROR_FALLBACK_REASONS = frozenset({"read_only", "not_git", "no_origin", "dirty"})


@dataclass
class _SourcePlan:
    """Where a reindex reads its content: the checkout itself, or ``commit`` exported from ``mirror``."""

    sync: Optional[Dict[str, Any]] = None
    mirror: Optional[Path] = None
    commit: Optional[str] = None


async def _mirror_plan(repo_path: Path, expected_revision: str, current: str, exc: SourceSyncError) -> _SourcePlan:
    """Fetch ``expected_revision`` into the repository's mirror after the checkout could not be moved."""
    failure: Dict[str, Any] = {
        "path": repo_path.as_posix(),
        "commit_hash": current,
        "source_revision_verified": False,
    }
    if mirror_root() is None:
        raise PermanentJobError(
            f"Cannot move the source checkout to {expected_revision[:16]}: {exc} "
            "Alternatively set BRAIN_SOURCE_MIRROR_DIR to a writable directory so Brain indexes origin "
            "through its own mirror.",
            result={**failure, "source_sync": {"status": "failed", "reason": exc.reason,
                                               "requested": expected_revision, "head": current}},
        ) from exc
    spec = await asyncio.to_thread(resolve_origin_spec, repo_path)
    if spec is None:
        raise PermanentJobError(
            f"Cannot move the source checkout to {expected_revision[:16]}: {exc} "
            f"No origin URL is known for {repo_path.as_posix()}; name it in BRAIN_ORIGIN_REMOTES "
            "so the source mirror can fetch it.",
            result={**failure, "source_sync": {"status": "failed", "reason": exc.reason, "mode": "mirror",
                                               "mirror": "no_origin", "requested": expected_revision,
                                               "head": current}},
        ) from exc
    try:
        fetched = await asyncio.to_thread(
            fetch_into_mirror,
            repo_path,
            spec,
            expected_revision,
            timeout=float(settings.BRAIN_WEBHOOK_FETCH_TIMEOUT_S),
        )
    except SourceSyncError as mirror_exc:
        raise PermanentJobError(
            f"Cannot fetch {expected_revision[:16]} into the source mirror: {mirror_exc}",
            result={**failure, "source_sync": {"status": "failed", "reason": mirror_exc.reason, "mode": "mirror",
                                               "fallback_reason": exc.reason, "remote": spec.display_url,
                                               "requested": expected_revision, "head": current}},
        ) from mirror_exc
    # SourceSyncTransientError (network, timeout) propagates: the queue retries it.
    logger.info(
        "run_reindex: {} cannot be moved ({}); indexing {} from mirror {}",
        repo_path,
        exc.reason,
        fetched.commit[:12],
        fetched.mirror.name,
    )
    return _SourcePlan(
        sync={
            "status": "mirror",
            "mode": "mirror",
            "fallback_reason": exc.reason,
            "requested": expected_revision,
            "previous_head": current or None,
            "head": fetched.commit,
            "changed": True,
            "remote": spec.display_url,
            "branch": spec.branch,
            **fetched.to_dict(),
        },
        mirror=fetched.mirror,
        commit=fetched.commit,
    )


async def _prepare_source_revision(repo_path: Path, expected_revision: str) -> _SourcePlan:
    """Bring the source to the revision a job asks for, or explain why it cannot.

    The git-merge webhook (and the origin poll) name a commit that is on
    origin, so the checkout HEAD differs from it exactly when there is
    something new to index. A checkout that cannot be moved (read-only, no
    origin, not git at all) is served from a Brain-owned mirror instead when
    ``BRAIN_SOURCE_MIRROR_DIR`` is set.
    """
    current = str(await asyncio.to_thread(get_git_commit_hash, repo_path) or "").strip()
    if revision_matches(expected_revision, current):
        return _SourcePlan(sync={"status": "already_at_revision", "requested": expected_revision,
                                 "previous_head": current, "head": current, "fetched": False, "changed": False})
    if not settings.BRAIN_WEBHOOK_FETCH_SOURCE:
        raise PermanentJobError(
            "Indexed source revision mismatch: "
            f"requested {expected_revision[:16]} but the checkout HEAD is {current[:16] or 'missing'}; "
            "BRAIN_WEBHOOK_FETCH_SOURCE is off, so the checkout was not updated",
            result={
                "path": repo_path.as_posix(),
                "commit_hash": current,
                "source_revision_verified": False,
                "source_sync": {"status": "disabled", "requested": expected_revision, "head": current},
            },
        )
    try:
        outcome = await asyncio.to_thread(
            sync_checkout_to_revision,
            repo_path,
            expected_revision,
            timeout=float(settings.BRAIN_WEBHOOK_FETCH_TIMEOUT_S),
        )
    except SourceSyncError as exc:
        if exc.reason in _MIRROR_FALLBACK_REASONS:
            return await _mirror_plan(repo_path, expected_revision, current, exc)
        # Unreachable commit, failed checkout: retrying cannot help.
        raise PermanentJobError(
            f"Cannot move the source checkout to {expected_revision[:16]}: {exc}",
            result={
                "path": repo_path.as_posix(),
                "commit_hash": current,
                "source_revision_verified": False,
                "source_sync": {"status": "failed", "reason": exc.reason,
                                "requested": expected_revision, "head": current},
            },
        ) from exc
    # SourceSyncTransientError (fetch failure, timeout) propagates: the queue retries it.
    logger.info(
        "run_reindex: moved {} from {} to {} (fetched={})",
        repo_path,
        str(outcome.previous_head)[:12],
        outcome.head[:12],
        outcome.fetched,
    )
    return _SourcePlan(sync=outcome.to_dict())


async def _recorded_mirror_plan(repo_path: Path, repository_id: Optional[int] = None) -> Optional[_SourcePlan]:
    """For a run without a revision: keep indexing the mirror a repository is already served from.

    Without this a nightly or self-heal run would index the stale mounted
    checkout and move the index backwards. Uses only local mirror objects;
    returns None (checkout mode) whenever mirrors are disabled.
    """
    if mirror_root() is None:
        return None
    if repository_id is None:
        record = await get_repository_by_path(repo_path)
        repository_id = record.id if record is not None else None
    if not isinstance(repository_id, int):
        return None
    state = await get_origin_state(repository_id)
    if not state or state.get("source_mode") != "mirror":
        return None
    spec = await asyncio.to_thread(resolve_origin_spec, repo_path)
    mirror = mirror_path(repo_path)
    if spec is None or mirror is None:
        return None
    commit = await asyncio.to_thread(resolve_commit, mirror, spec.tracking_ref)
    if not commit:
        return None
    return _SourcePlan(
        sync={"status": "mirror", "mode": "mirror", "requested": None, "head": commit, "fetched": False,
              "changed": False, "mirror": mirror.name, "mirror_head": commit, "commit": commit,
              "remote": spec.display_url, "branch": spec.branch},
        mirror=mirror,
        commit=commit,
    )


async def _index_with_plan(
    indexer: Any,
    repo_path: Path,
    plan: Optional[_SourcePlan],
    *,
    clean: bool,
    expected_revision: str = "",
) -> Any:
    """Index the checkout, or a snapshot of the planned mirror commit exported to a temporary directory."""
    if plan is None or plan.mirror is None or not plan.commit:
        if expected_revision:
            return await indexer.index_repository(repo_path, clean=clean, expected_revision=expected_revision)
        return await indexer.index_repository(repo_path, clean=clean)
    with tempfile.TemporaryDirectory(prefix="brain-mirror-") as tmp:
        snapshot = Path(tmp)
        await asyncio.to_thread(export_commit, plan.mirror, plan.commit, snapshot)
        return await indexer.index_repository(
            repo_path,
            clean=clean,
            expected_revision=expected_revision,
            source_root=snapshot,
            source_commit=plan.commit,
        )


async def _record_source_mode(repository_id: Any, plan: Optional[_SourcePlan]) -> None:
    """Remember whether the repository is served from its checkout or its mirror (mirrors enabled only)."""
    if mirror_root() is None or not isinstance(repository_id, int):
        return
    if plan is not None and plan.mirror is not None:
        await upsert_origin_state(
            repository_id, source_mode="mirror", mirror_head=(plan.sync or {}).get("mirror_head") or plan.commit
        )
    else:
        await upsert_origin_state(repository_id, source_mode="checkout")


async def _repair_source_fingerprint(
    repo_path: Path, mirror: Optional[Path], commit: Optional[str]
) -> tuple[bool, str]:
    """(source is clean, content digest) of what the reindex read: the checkout or the mirror commit."""
    if mirror is None or not commit:
        files = await asyncio.to_thread(_scan_repo_files, repo_path)
        clean = await asyncio.to_thread(git_source_is_clean, repo_path, files)
        return clean, await asyncio.to_thread(source_content_digest, repo_path, files)
    with tempfile.TemporaryDirectory(prefix="brain-mirror-") as tmp:
        snapshot = Path(tmp)
        await asyncio.to_thread(export_commit, mirror, commit, snapshot)
        files = await asyncio.to_thread(_scan_repo_files, snapshot)
        clean = await asyncio.to_thread(git_source_is_clean, snapshot, files)
        return clean, await asyncio.to_thread(source_content_digest, snapshot, files)


async def run_reindex(params: Dict[str, Any]) -> Dict[str, Any]:
    repo_path = resolve_repo_path(params.get("repo_path"))
    clean = bool(params.get("clean", False))
    await init_db()
    indexer = FileIndexer()
    expected_revision = str(params.get("source_revision") or "").strip()
    plan: Optional[_SourcePlan] = None
    if expected_revision:
        plan = await _prepare_source_revision(repo_path, expected_revision)
    else:
        plan = await _recorded_mirror_plan(repo_path)
    source_sync: Optional[Dict[str, Any]] = plan.sync if plan is not None else None
    if plan is None or plan.mirror is None:
        # Checkout mode keeps the exact call it always made.
        repo = await indexer.index_repository(repo_path, clean=clean, expected_revision=expected_revision)
    else:
        repo = await _index_with_plan(indexer, repo_path, plan, clean=clean, expected_revision=expected_revision)
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
                "source_sync": source_sync,
            },
        )

    result: Dict[str, Any] = {
        "repo_id": repo.id,
        "path": repo.path,
        "commit_hash": actual_revision,
        "source_revision_verified": bool(expected_revision and revision_matches(expected_revision, actual_revision)),
        **indexer.verification,
    }
    if source_sync is not None:
        result["source_sync"] = source_sync
    if result.get("status") == "failed":
        raise PermanentJobError("Repository projection or source verification failed", result=result)
    await _record_source_mode(repo.id, plan)
    counts = result.get("counts") or {}
    if (result.get("status") == "degraded" and not (counts.get("files") or {}).get("failed")
            and not (counts.get("graph") or {}).get("failed")):
        repair = {"repo_path": repo.path, "repository_id": repo.id,
                  "source_revision": actual_revision, "indexing_run_id": indexer.indexing_run_id,
                  "limit": settings.INDEX_REPAIR_CHUNK_LIMIT, "batch_size": settings.INDEX_EMBEDDING_BATCH_SIZE}
        if plan is not None and plan.mirror is not None:
            repair["source_mode"] = "mirror"
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


async def run_diff_review(params: Dict[str, Any]) -> Dict[str, Any]:
    """Worker task: run diff review in background (LLM calls can take minutes)."""
    from brain.analyzers.diff_analyzer import DiffAnalyzer

    repo_path = resolve_repo_path(params.get("repo_path"))
    await init_db()
    analyzer = DiffAnalyzer(repo_path)
    return await analyzer.review_diff(base=params.get("base"), head=params.get("head"))


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
    try:
        from brain.telemetry.tool_calls import prune_tool_calls
        tool_call_retention: Dict[str, Any] = {
            "deleted": await prune_tool_calls(settings.MCP_TOOL_CALLS_RETENTION_DAYS),
            "retention_days": settings.MCP_TOOL_CALLS_RETENTION_DAYS,
        }
    except Exception as exc:  # Housekeeping must not fail the health check.
        tool_call_retention = {"error": f"{type(exc).__name__}: {exc}"[:260]}
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
        "tool_call_retention": tool_call_retention,
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
    # A run indexed from the source mirror is repaired against the same mirror commit.
    mirror: Optional[Path] = None
    mirror_commit: Optional[str] = None
    if params.get("indexing_run_id") and params.get("source_mode") == "mirror":
        mirror = mirror_path(repo_path)
        revision = str(params.get("source_revision") or "")
        mirror_commit = await asyncio.to_thread(resolve_commit, mirror, revision) if mirror and revision else None
        if mirror_commit is None or mirror_commit != revision:
            raise PermanentJobError("Repair source revision mismatch: the source mirror does not hold it")
    elif params.get("indexing_run_id") and get_git_commit_hash(repo_path) != params.get("source_revision"):
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
        source_clean, content_digest = await _repair_source_fingerprint(repo_path, mirror, mirror_commit)
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
                         and (mirror_commit or get_git_commit_hash(repo_path)) == params["source_revision"]
                         and source_clean
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


async def run_memory_consolidation(params: Dict[str, Any]) -> Dict[str, Any]:
    """Run one memory consolidation pass (L1 events → L2 episodes → L3 learnings)."""
    if params.get("scheduled") and not settings.MEMORY_CONSOLIDATION_ENABLED:
        return {
            "status": "skipped",
            "reason": "MEMORY_CONSOLIDATION_ENABLED=false",
            "scheduled": True,
        }
    await init_db()
    from brain.memory.consolidation import run_consolidation

    require_approval = params.get(
        "require_approval", settings.MEMORY_CONSOLIDATION_REQUIRE_APPROVAL
    )
    if require_approval is None:
        require_approval = settings.MEMORY_CONSOLIDATION_REQUIRE_APPROVAL
    report = await run_consolidation(
        require_approval=require_approval,
        dry_run=params.get("dry_run", False),
    )
    report["status"] = "completed"
    report["scheduled"] = bool(params.get("scheduled"))
    return report


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


async def auto_heal_stale_repositories(exclude_repository_id: int | None = None) -> dict[str, Any]:
    """Iterate registered repositories and autonomously reindex/repair any stale or behind ones."""
    healed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    indexer = FileIndexer()
    try:
        repos = await list_all_repositories()
        for repo in repos:
            if repo.id == exclude_repository_id:
                skipped.append({"path": repo.path, "reason": "handled_by_nightly_target"})
                continue
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
            incompatible = inv_dict.get("incompatible_embeddings") or 0

            if status in ("behind", "stale", "unindexed") or missing > 0 or stale > 0 or incompatible > 0:
                logger.info(f"Autonomous Self-Healing: reindexing {repo.path} (status={status}, missing={missing}, stale={stale}, incompatible={incompatible})")
                heal_plan = await _recorded_mirror_plan(repo_p, getattr(repo, "id", None))
                indexed_repo = await _index_with_plan(indexer, repo_p, heal_plan, clean=False)
                backfill_res = await backfill_embeddings(indexed_repo.id)
                healed.append({
                    "path": repo.path,
                    "previous_status": status,
                    "incompatible_embeddings": incompatible,
                    "backfill": backfill_res.to_dict(),
                })
            else:
                skipped.append({"path": repo.path, "status": status})
    except Exception as exc:
        logger.warning(f"Autonomous Self-Healing sweep encountered an issue: {exc}")
    return {"healed": healed, "skipped": skipped}


async def run_nightly_maintenance(params: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the indexed repository healthy: heal stale repos, reindex, verify and
    repair embeddings, consolidate memory.

    The late-interaction (LFM) deep lane is an optional extra on top. Without it
    the standard maintenance still runs, so a default install gets a working
    nightly job instead of a skipped or failing one.
    """
    deep = bool(settings.NIGHTLY_DEEP_MAINTENANCE_ENABLED)

    await init_db()

    repo_path = _deep_repo_path(params)
    if deep:
        record = await _approved_deep_repository(repo_path)
    else:
        record = await get_repository_by_path(repo_path)
        if record is None:
            raise PermanentJobError(
                f"Repository is not indexed yet: {repo_path.as_posix()}. Run `brain index --repo {repo_path.as_posix()}` first."
            )
    # The selected target has its own reindex below; sweep only other repos.
    auto_heal_result = await auto_heal_stale_repositories(exclude_repository_id=record.id)

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

    late_sync: Dict[str, Any] = {"status": "skipped", "reason": "NIGHTLY_DEEP_MAINTENANCE_ENABLED=false"}
    quality_probes: list[dict[str, Any]] = []
    if deep:
        late_sync, exact_client = await _reconcile_exact_lfm(record.id)
        try:
            quality_probes = await _run_deep_quality_probes(
                record.path,
                exact_client,
            )
        finally:
            await exact_client.aclose()
    if deep and (not quality_probes or any(
        not probe.get("passed") for probe in quality_probes
    )):
        raise PermanentJobError(
            "Nightly deep quality probe failed closed",
            result={
                "reindex": reindex,
                "dense_after": dense_after,
                "late_interaction": late_sync,
                "quality_probes": quality_probes,
            },
        )

    # Memory consolidation: best-effort, must not break the nightly run.
    memory_consolidation: Dict[str, Any] = {"status": "skipped"}
    if settings.MEMORY_CONSOLIDATION_ENABLED:
        try:
            memory_consolidation = await run_memory_consolidation({"scheduled": True})
        except Exception as exc:
            logger.warning(f"Nightly memory consolidation failed (best-effort): {exc}")
            memory_consolidation = {"status": "failed", "error": str(exc)[:200]}

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
        "mode": "deep" if deep else "standard",
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
        "memory_consolidation": memory_consolidation,
    }
    await redis_client.set(
        "brain:nightly-maintenance:last-result",
        json.dumps(result, ensure_ascii=False, default=str),
        ex=30 * 24 * 60 * 60,
    )
    return result


async def _standard_deep_context(task_description: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Durable context pack with the default retrieval, for installs without the
    late-interaction model service. The agent still gets a full, persisted pack."""
    from brain.context.context_pack_builder import ContextPackBuilder

    await init_db()
    repo_path = resolve_repo_path(params.get("repo_path") or settings.TARGET_REPO_PATH)
    if await get_repository_by_path(repo_path) is None:
        raise PermanentJobError(
            f"Repository is not indexed yet: {repo_path.as_posix()}. Run `brain index --repo {repo_path.as_posix()}` first."
        )
    context_pack = await ContextPackBuilder().build_context_pack(
        task_description=task_description,
        repo_path=repo_path,
        budget="deep",
        consumer=str(params.get("consumer") or "") or None,
    )
    context_pack.pop("markdown_content", None)
    context_pack = json.loads(json.dumps(context_pack, ensure_ascii=False, default=str))
    return {
        "status": "completed",
        "mode": "standard",
        "note": "Late-interaction reranking is not configured; built with the default retrieval.",
        "context_pack": context_pack,
    }


async def run_deep_context(params: Dict[str, Any]) -> Dict[str, Any]:
    task_description = str(params.get("task_description") or "").strip()
    if not task_description:
        raise PermanentJobError("Deep context task_description is required")

    if not settings.LATE_INTERACTION_DEEP_ENABLED:
        return await _standard_deep_context(task_description, params)

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
            # Supplied by whichever surface queued the job; a job queued before
            # this was recorded simply has no consumer, which is the truth.
            consumer=str(params.get("consumer") or "") or None,
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
        "enrich_summaries": run_enrich_summaries,
        "reindex": run_reindex,
        "diff_review": run_diff_review,
        "health_check": run_health_check,
        "embedding_verify": run_embedding_verify,
        "embedding_backfill": run_embedding_backfill,
        "benchmark": run_benchmark,
        "proactive_insights": run_proactive_insights,
        "memory_consolidation": run_memory_consolidation,
        "self_diagnosis": run_self_diagnosis,
        "origin_poll": run_origin_poll,
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
