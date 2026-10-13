"""Evidence-bound proactive insight generation.

The core rule: deterministic code gathers facts first. The LLM may synthesize
or add bounded recommendations, but every saved insight must carry evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, cast

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult

from brain.config.paths import resolve_repo_path
from brain.config.settings import settings
from brain.database.harness_models import AgentTask, TERMINAL_STATUSES, WorkerLease
from brain.database.models import BrainInsight, ContextPack, IndexingRun, Repository
from brain.database.repository_utils import get_repository_by_path
from brain.database.session import async_session_factory, check_health, redis_client
from brain.workers.db_fencing import assert_current_db_fence
from brain.embeddings.integrity import collect_embedding_inventory
from brain.indexers.repo_indexer import get_git_commit_hash
from brain.llm.router import TaskKind, get_model_router
from brain.version import build_info
from brain.memory.repo_freshness import assess_repository_freshness
from brain.insights.candidates import (
    INSIGHT_ENGINE_VERSION as INSIGHT_ENGINE_VERSION,
    InsightCandidate as InsightCandidate,
    _clean_text as _clean_text,
    _safe_token as _safe_token,
    _clean_evidence as _clean_evidence,
    _dedupe_key as _dedupe_key,
    _safe_count as _safe_count,
)
from brain.insights.detectors import (
    operational_insights,
    repository_insights,
    worker_insights,
    retrieval_insights,
    indexing_insights,
    recent_job_insights,
    memory_insights,
)
from brain.insights.drift_analyzer import scan_repository_drift
from brain.insights.drift_baseline import DriftBaselineManager

_ACTIVE_STATUSES = ("new", "accepted")
_MAX_LLM_INSIGHTS = 4

# The complete status vocabulary this engine writes. persist_insights() creates
# every row as "new", stales anything still active whose dedupe key stops
# recurring, and revives a "stale" or "actioned" row back to "new" when the same
# finding returns. Those four values are therefore the whole lifecycle — an
# operator transition may not introduce a fifth.
INSIGHT_STATUSES: tuple[str, ...] = ("new", "accepted", "actioned", "stale")

# Legal operator moves, read straight off the engine's own semantics:
#   new       a fresh finding; it can be acknowledged, closed out, or dismissed.
#   accepted  acknowledged and still active (_ACTIVE_STATUSES) — from here the
#             operator either finishes the remediation or dismisses it.
#   actioned  the remediation was performed. Not an active status, so the scan
#             never auto-stales it; only a manual dismissal moves it on.
#   stale     the finding is no longer observed. Nothing leads out of it by hand:
#             persist_insights() alone revives it, and only by seeing it again.
INSIGHT_STATUS_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "new": ("accepted", "actioned", "stale"),
    "accepted": ("actioned", "stale"),
    "actioned": ("stale",),
    "stale": (),
}


class InsightNotFoundError(LookupError):
    """No insights row carries the requested id."""


class InsightTransitionError(ValueError):
    """The requested status move is outside the engine's lifecycle."""


def allowed_status_transitions(current: str) -> tuple[str, ...]:
    """Statuses `current` may legally move to, excluding the idempotent re-assert."""
    return INSIGHT_STATUS_TRANSITIONS.get(str(current or "").strip().lower(), ())


def is_legal_status_transition(current: str, target: str) -> bool:
    """Re-asserting the status a row already has is legal and does nothing —
    that is what makes the mutation endpoint idempotent under a double click."""
    current = str(current or "").strip().lower()
    target = str(target or "").strip().lower()
    if target not in INSIGHT_STATUSES:
        return False
    if current == target:
        return True
    return target in INSIGHT_STATUS_TRANSITIONS.get(current, ())


async def set_insight_status(insight_id: int, status: str) -> dict[str, Any]:
    """Move one insight through the lifecycle above.

    Raises InsightTransitionError for a status outside INSIGHT_STATUSES or a move
    the lifecycle does not allow, and InsightNotFoundError for an id that has no
    row — neither is a server fault, so neither may surface as a 500.
    """
    target = _safe_token(status, fallback="")
    if target not in INSIGHT_STATUSES:
        raise InsightTransitionError(
            f"Unknown insight status '{_clean_text(status, limit=50)}'. Known statuses: {', '.join(INSIGHT_STATUSES)}."
        )

    now = _utc_now()
    async with async_session_factory() as session:
        async with session.begin():
            await assert_current_db_fence(session)
            row = (
                await session.execute(select(BrainInsight).where(BrainInsight.id == insight_id))
            ).scalar_one_or_none()
            if row is None:
                raise InsightNotFoundError(f"No insight with id {insight_id}")

            previous = str(row.status or "new")
            if not is_legal_status_transition(previous, target):
                allowed = allowed_status_transitions(previous)
                raise InsightTransitionError(
                    f"Insight {insight_id} cannot move from '{previous}' to '{target}'. "
                    + (
                        f"Allowed from '{previous}': {', '.join(allowed)}."
                        if allowed
                        else f"'{previous}' is terminal until the finding recurs."
                    )
                )

            changed = previous != target
            if changed:
                row.status = target
                row.updated_at = now
            # Built inside the transaction: after the commit the instance may be
            # expired, and refreshing it would cost a second round trip.
            payload = _row_to_public_dict(row)

    return {"insight": payload, "previous_status": previous, "changed": changed}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _exception_summary(exc: BaseException, *, limit: int = 400) -> str:
    """Return an operator-useful error even for exceptions with empty messages."""
    name = type(exc).__name__
    detail = _clean_text(exc, limit=limit)
    if not detail:
        return name
    return _clean_text(f"{name}: {detail}", limit=limit)


def _compact_json(data: Any, *, max_chars: int) -> str:
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 160] + "\n...TRUNCATED_SNAPSHOT..."


def _dt(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


async def collect_proactive_snapshot(repo_path: str | Path | None = None) -> dict[str, Any]:
    target_repo = resolve_repo_path(repo_path or settings.TARGET_REPO_PATH)
    snapshot: dict[str, Any] = {
        "collected_at": _utc_now().isoformat(),
        "project": build_info(),
        "repo": {
            "path": target_repo.as_posix(),
            "commit": get_git_commit_hash(target_repo),
        },
    }

    services = await check_health()
    snapshot["services"] = _redact_service_errors(services)
    try:
        from brain.database.session import redis_client
        from brain.workers.scheduler import scheduler_status

        snapshot["scheduler"] = {"stale": (await scheduler_status(redis_client)).get("stale", [])}
    except Exception as exc:
        snapshot["scheduler"] = {"stale": [], "error": type(exc).__name__}

    try:
        record = await get_repository_by_path(target_repo)
        inventory = await collect_embedding_inventory(
            record.id if record else None,
            record.path if record else target_repo.as_posix(),
            fast=True,
        )
        snapshot["embeddings"] = inventory.to_dict()
    except Exception as exc:
        record = None
        snapshot["embeddings"] = {
            "missing_embeddings": None,
            "stale_embeddings": None,
            "incompatible_embeddings": None,
            "pgvector_coverage_pct": None,
            "collection_error": _clean_text(exc, limit=260),
        }

    try:
        async with async_session_factory() as session:
            from brain.dashboard.helpers import get_db_counts

            counts = await get_db_counts(session)
            snapshot["counts"] = counts

            latest_runs = (
                (await session.execute(select(IndexingRun).order_by(IndexingRun.id.desc()).limit(5))).scalars().all()
            )
            snapshot["recent_indexing_runs"] = [
                {
                    "id": run.id,
                    "status": run.status,
                    "commit_hash": run.commit_hash,
                    "started_at": _dt(run.started_at),
                    "completed_at": _dt(run.completed_at),
                }
                for run in latest_runs
            ]

            latest_packs = (
                (await session.execute(select(ContextPack).order_by(ContextPack.id.desc()).limit(3))).scalars().all()
            )
            snapshot["recent_context_packs"] = [
                {
                    "id": pack.id,
                    "task_description": _clean_text(pack.task_description, limit=240),
                    "path": Path(pack.path).name,
                    "created_at": _dt(pack.created_at),
                }
                for pack in latest_packs
            ]
    except Exception as exc:
        snapshot["counts"] = {}
        snapshot["recent_indexing_runs"] = []
        snapshot["recent_context_packs"] = []
        snapshot["database_collection_warning"] = _clean_text(exc, limit=260)

    try:
        from brain.dashboard.helpers import get_recent_brain_jobs, get_reports_list

        job_data = await get_recent_brain_jobs(limit=100)
        snapshot["recent_jobs"] = [
            {
                "id": job.get("id"),
                "type": job.get("type"),
                "status": job.get("status"),
                "created_at": job.get("created_at"),
                "updated_at": job.get("updated_at"),
                "attempt": job.get("attempt"),
                "max_attempts": job.get("max_attempts"),
                "error": _clean_text(job.get("error"), limit=260) if job.get("error") else "",
            }
            for job in job_data.get("jobs", [])
        ]
        snapshot["reports"] = get_reports_list()[:5]
        from brain.workers.queue import JobQueue

        queue = JobQueue(redis_client, prefix=settings.WORKER_REDIS_PREFIX)
        queue_stats = await queue.get_stats()
        now = _utc_now()
        stale_worker_jobs = []
        for job in snapshot["recent_jobs"]:
            if str(job.get("status")).lower() not in {
                "queued",
                "running",
                "retrying",
            }:
                continue
            updated = _parse_snapshot_datetime(job.get("updated_at"))
            if updated is None:
                continue
            max_attempts = max(_safe_count(job.get("max_attempts")), 1)
            stale_after = max(settings.WORKER_JOB_TIMEOUT_SECONDS, 60) * max_attempts + 300
            age_seconds = max(0, int((now - updated).total_seconds()))
            if age_seconds > stale_after:
                stale_worker_jobs.append(
                    {
                        "id": job.get("id"),
                        "type": job.get("type"),
                        "status": job.get("status"),
                        "age_seconds": age_seconds,
                        "stale_after_seconds": stale_after,
                    }
                )
        snapshot["worker_queue"] = {
            **queue_stats,
            "stale_job_count": len(stale_worker_jobs),
            "stale_jobs": stale_worker_jobs[:10],
        }
    except Exception as exc:
        snapshot["recent_jobs"] = []
        snapshot["reports"] = []
        snapshot["collection_warning"] = f"jobs_or_reports_unavailable: {exc}"
        snapshot["worker_queue"] = {
            "queued": None,
            "processing": None,
            "retrying": None,
            "stale_job_count": None,
            "stale_jobs": [],
            "collection_error": _clean_text(exc, limit=260),
        }

    try:
        async with async_session_factory() as session:
            repositories = list(
                (await session.execute(select(Repository).order_by(Repository.id.asc()))).scalars().all()
            )
        snapshot["repositories"] = [
            {
                "id": repository.id,
                "name": repository.name,
                "path": repository.path,
                "indexing_status": repository.indexing_status,
                "freshness": await assess_repository_freshness(repository),
            }
            for repository in repositories
        ]
    except Exception as exc:
        snapshot["repositories"] = []
        snapshot["repository_collection_warning"] = _clean_text(exc, limit=260)

    try:
        now = _utc_now()
        async with async_session_factory() as session:
            tasks = list(
                (await session.execute(select(AgentTask).order_by(AgentTask.updated_at.desc()).limit(500)))
                .scalars()
                .all()
            )
            leases = list((await session.execute(select(WorkerLease))).scalars().all())
        status_counts: dict[str, int] = {}
        stale_tasks: list[dict[str, Any]] = []
        for task in tasks:
            status_counts[task.status] = status_counts.get(task.status, 0) + 1
            if task.status in TERMINAL_STATUSES or task.status == "acceptance_pending":
                continue
            touched = task.updated_at or task.created_at
            if touched.tzinfo is None:
                touched = touched.replace(tzinfo=timezone.utc)
            age_seconds = max(0, int((now - touched).total_seconds()))
            timeout_seconds = max(int(task.timeout_seconds or 900), 60)
            if age_seconds > timeout_seconds:
                stale_tasks.append(
                    {
                        "id": str(task.id),
                        "title": _clean_text(task.title, limit=160),
                        "status": task.status,
                        "age_seconds": age_seconds,
                        "timeout_seconds": timeout_seconds,
                        "repo_path": task.repo_path,
                    }
                )
        snapshot["harness"] = {
            "task_count": len(tasks),
            "status_counts": status_counts,
            "stale_task_count": len(stale_tasks),
            "stale_tasks": stale_tasks[:10],
            "expired_lease_count": sum(
                1
                for lease in leases
                if (
                    lease.lease_expires_at.replace(tzinfo=timezone.utc)
                    if lease.lease_expires_at.tzinfo is None
                    else lease.lease_expires_at
                )
                <= now
            ),
        }
    except Exception as exc:
        snapshot["harness"] = {
            "task_count": None,
            "status_counts": {},
            "stale_task_count": None,
            "stale_tasks": [],
            "collection_error": _clean_text(exc, limit=260),
        }

    try:
        from brain.dashboard.helpers import get_latest_eval_metrics

        snapshot["evaluation"] = get_latest_eval_metrics(target_repo)
    except Exception as exc:
        snapshot["evaluation"] = {"error": _clean_text(exc, limit=260)}

    snapshot["alerting"] = {
        "self_diagnosis_enabled": settings.SELF_DIAGNOSIS_ENABLED,
        "telegram_enabled": settings.TELEGRAM_ALERTS_ENABLED,
        "telegram_credentials_present": bool(settings.TELEGRAM_ALERT_BOT_TOKEN and settings.TELEGRAM_ALERT_CHAT_ID),
    }
    try:
        from brain.database.session import neo4j_driver

        async with neo4j_driver.session() as session:
            count_record = await (
                await session.run("MATCH (n) WHERE n.repository_id IS NULL RETURN count(n) AS count")
            ).single()
        snapshot["graph_identity"] = {"legacy_unscoped_nodes": int(count_record["count"] if count_record else 0)}
    except Exception as exc:
        snapshot["graph_identity"] = {
            "legacy_unscoped_nodes": None,
            "collection_error": _clean_text(exc, limit=260),
        }

    return snapshot


def _parse_snapshot_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _redact_service_errors(services: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for name, details in services.items():
        if not isinstance(details, dict):
            redacted[name] = {"status": "unknown"}
            continue
        clean = {key: value for key, value in details.items() if key not in {"password", "token", "api_key"}}
        if "error" in clean:
            clean["error"] = _clean_text(clean["error"], limit=260)
        redacted[name] = clean
    return redacted


def _drift_insights(snapshot: dict[str, Any]) -> list[InsightCandidate]:
    """Collect drift deltas without mixing filesystem I/O into snapshot checks."""
    insights: list[InsightCandidate] = []
    # Architectural Drift Intelligence v2 Scan & Delta Classification
    repo_info = snapshot.get("repo") or {}
    repo_info_path = repo_info.get("path")
    target_repo = Path(repo_info_path) if isinstance(repo_info_path, str) and repo_info_path else resolve_repo_path(None)
    drift_findings = scan_repository_drift(target_repo)
    baseline_mgr = DriftBaselineManager(target_repo / ".brain" / "drift_baseline.json")
    previous_baseline = baseline_mgr.load_baseline()
    evaluated_deltas = baseline_mgr.compute_deltas(drift_findings, previous_baseline)

    for item in evaluated_deltas:
        finding = item.finding
        delta = item.delta_state

        if delta == "resolved":
            insights.append(
                InsightCandidate(
                    insight_type="architectural_drift",
                    severity="info",
                    title=f"Resolved Architectural Drift: {finding.rule_name}",
                    summary=f"Previous architectural drift violation in {finding.file_path} is now resolved.",
                    evidence=[
                        {"label": "file_path", "value": finding.file_path},
                        {"label": "fingerprint", "value": finding.fingerprint},
                        {"label": "delta_state", "value": "resolved"},
                    ],
                    recommended_action="Verification passed. Keep component boundaries clean.",
                    confidence="high",
                    dedupe_key=f"architectural_drift:resolved:{finding.fingerprint}",
                )
            )
        else:
            insights.append(
                InsightCandidate(
                    insight_type="architectural_drift",
                    severity=finding.severity,
                    title=f"Architectural Boundary Drift ({delta.upper()}): {finding.rule_name}",
                    summary=finding.description,
                    evidence=[
                        {"label": "file_path", "value": finding.file_path},
                        {"label": "line_number", "value": finding.line_number},
                        {"label": "containing_symbol", "value": finding.containing_symbol},
                        {"label": "imported_module", "value": finding.imported_module},
                        {"label": "delta_state", "value": delta},
                        {"label": "fingerprint", "value": finding.fingerprint},
                    ],
                    recommended_action=finding.remediation_guidance,
                    confidence="high",
                    dedupe_key=f"architectural_drift:{finding.fingerprint}",
                )
            )

    return insights


def deterministic_insights(snapshot: dict[str, Any]) -> list[InsightCandidate]:
    """Run snapshot detectors in contract order, then inspect on-disk drift."""
    insights = operational_insights(snapshot, environment=settings.ENVIRONMENT)
    insights.extend(repository_insights(snapshot))
    insights.extend(worker_insights(snapshot))
    insights.extend(retrieval_insights(snapshot, minimum_recall=settings.SELF_DIAGNOSIS_MIN_AVG_RECALL))
    insights.extend(indexing_insights(snapshot))
    insights.extend(recent_job_insights(snapshot))
    insights.extend(memory_insights(snapshot))
    insights.extend(_drift_insights(snapshot))
    counts = snapshot.get("counts") or {}
    coverage = (snapshot.get("embeddings") or {}).get("pgvector_coverage_pct")

    if not insights:
        insights.append(
            InsightCandidate(
                insight_type="operational_readiness",
                severity="ready",
                title="Project Brain is ready for operator review",
                summary="Core services, embeddings, source inventory, and recent job signals do not show an active issue.",
                evidence=[
                    {"label": "services", "value": "healthy"},
                    {"label": "files", "value": counts.get("files")},
                    {"label": "pgvector_coverage_pct", "value": coverage},
                ],
                recommended_action="Review recent context packs and automation before a deploy or handoff.",
                confidence="medium",
                dedupe_key="operational_readiness:ready_review",
            )
        )

    return insights


async def _llm_insights(snapshot: dict[str, Any]) -> tuple[list[InsightCandidate], dict[str, Any]]:
    router = get_model_router()
    model = router.task_model(TaskKind.INSIGHT)
    snapshot_json = _compact_json(snapshot, max_chars=settings.PROACTIVE_INSIGHTS_MAX_SNAPSHOT_CHARS)
    prompt = (
        "Analyze this Project Brain operational snapshot and return proactive insights.\n"
        "Respond only with JSON in this exact shape:\n"
        '{"insights":[{"insight_type":"...","severity":"critical|warning|info|ready",'
        '"title":"...","summary":"...","evidence":[{"label":"...","value":"..."}],'
        '"recommended_action":"...","confidence":"low|medium|high","dedupe_key":"optional"}]}\n'
        f"Return at most {_MAX_LLM_INSIGHTS} concise insights; keep each summary and recommended_action under 240 "
        "characters and use at "
        "most 4 evidence entries. If there is no additional observation, return an empty insights list. "
        "Rules: every insight must cite evidence from the snapshot; do not invent files, secrets, hosts, users, or "
        "actions; do not recommend destructive operations; prefer operator-visible next steps.\n\n"
        f"SNAPSHOT_JSON:\n{snapshot_json}"
    )
    system_instruction = (
        "You are Project Brain's proactive systems architect. You produce bounded, evidence-first operational insights. "
        "You never expose secrets and never claim production readiness without evidence."
    )
    raw = await asyncio.wait_for(
        router.llm(TaskKind.INSIGHT).generate(
            prompt=prompt,
            system_instruction=system_instruction,
            temperature=0.1,
            max_tokens=1600,
        ),
        timeout=settings.PROACTIVE_INSIGHTS_TIMEOUT_S,
    )
    parsed, parse_recovered = _parse_json_payload_with_status(raw)
    raw_items = parsed.get("insights") if isinstance(parsed, dict) else parsed
    if not isinstance(raw_items, list):
        raise ValueError("LLM insight payload did not contain an insights list")
    insights = []
    for item in raw_items[:_MAX_LLM_INSIGHTS]:
        candidate = _candidate_from_llm_item(item, model=model)
        if candidate and _candidate_evidence_is_grounded(candidate, snapshot, snapshot_json):
            insights.append(candidate)
    return insights, {
        "status": "used",
        "model": model,
        "count": len(insights),
        "input_chars": len(snapshot_json),
        "parse_recovered": parse_recovered,
    }


def _evidence_index(snapshot: dict[str, Any]) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}

    def normalized(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return "null"
        return _clean_text(value, limit=800).lower()

    def walk(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, (*path, str(key)))
            return
        if isinstance(value, list):
            for child in value:
                walk(child, path)
            return
        if not path:
            return
        labels = {
            re.sub(r"[^a-z0-9]+", "_", path[-1].lower()).strip("_"),
            re.sub(r"[^a-z0-9]+", "_", ".".join(path).lower()).strip("_"),
        }
        for label in labels:
            if label:
                index.setdefault(label, set()).add(normalized(value))

    walk(snapshot, ())
    return index


def _candidate_evidence_is_grounded(
    candidate: InsightCandidate,
    snapshot: dict[str, Any],
    snapshot_json: str,
) -> bool:
    """Require every LLM citation to match a snapshot field and its exact value."""
    evidence_index = _evidence_index(snapshot)
    for item in candidate.evidence:
        label = re.sub(
            r"[^a-z0-9]+",
            "_",
            _clean_text(item.get("label"), limit=160).lower(),
        ).strip("_")
        value = _clean_text(item.get("value"), limit=800).lower()
        if not label or not value or value not in evidence_index.get(label, set()):
            return False

    # Absolute/path-like claims are high-risk hallucinations. If the LLM adds
    # one to prose, it must already be present in the bounded snapshot.
    haystack = snapshot_json.lower()
    free_text = " ".join(
        [
            text
            for text in (
                candidate.title,
                candidate.summary,
                candidate.recommended_action,
            )
            if text is not None
        ]
    )
    path_tokens = re.findall(r"(?:[a-zA-Z]:[\\/]|/)[a-zA-Z0-9_.\\/-]+", free_text)
    if any(token.lower().rstrip(".,;:") not in haystack for token in path_tokens):
        return False
    return True


def _llm_cache_material(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Keep diagnostic state, discard volatile timestamps and successful noise."""
    stable_services: dict[str, Any] = {}
    for name, state in (snapshot.get("services") or {}).items():
        if isinstance(state, dict):
            # Latency samples (for example Redis ping_ms) are useful in the
            # snapshot but must not spend another LLM call on every run.
            stable_services[name] = {
                key: state.get(key)
                for key in ("status", "error")
                if state.get(key) not in (None, "")
            }
        else:
            stable_services[name] = state
    repositories = []
    for repository in snapshot.get("repositories") or []:
        freshness = repository.get("freshness") or {}
        repositories.append(
            {
                "id": repository.get("id"),
                "path": repository.get("path"),
                "indexing_status": repository.get("indexing_status"),
                "freshness": {
                    key: freshness.get(key)
                    for key in (
                        "status",
                        "source_present",
                        "source_head_commit",
                        "indexed_commit",
                        "source_manifest_digest",
                    )
                },
            }
        )
    failed_jobs = [
        {
            "type": job.get("type"),
            "status": job.get("status"),
            "error": job.get("error"),
        }
        for job in snapshot.get("recent_jobs") or []
        if str(job.get("status")).lower() == "failed"
    ]
    harness = snapshot.get("harness") or {}
    stable_harness = {
        "task_count": harness.get("task_count"),
        "status_counts": harness.get("status_counts"),
        "stale_task_count": harness.get("stale_task_count"),
        "expired_lease_count": harness.get("expired_lease_count"),
        "stale_tasks": [
            {
                "id": task.get("id"),
                "status": task.get("status"),
                "timeout_seconds": task.get("timeout_seconds"),
                "repo_path": task.get("repo_path"),
            }
            for task in harness.get("stale_tasks") or []
        ],
        "collection_error": harness.get("collection_error"),
    }
    worker_queue = snapshot.get("worker_queue") or {}
    stable_worker_queue = {
        "queued": worker_queue.get("queued"),
        "processing": worker_queue.get("processing"),
        "retrying": worker_queue.get("retrying"),
        "stale_job_count": worker_queue.get("stale_job_count"),
        "stale_jobs": [
            {
                "id": job.get("id"),
                "type": job.get("type"),
                "status": job.get("status"),
            }
            for job in worker_queue.get("stale_jobs") or []
        ],
        "collection_error": worker_queue.get("collection_error"),
    }
    return {
        "engine_version": INSIGHT_ENGINE_VERSION,
        "llm_route": snapshot.get("llm_route"),
        "project": snapshot.get("project"),
        "repo": snapshot.get("repo"),
        "services": stable_services,
        "counts": snapshot.get("counts"),
        "embeddings": snapshot.get("embeddings"),
        "repositories": repositories,
        "harness": stable_harness,
        "worker_queue": stable_worker_queue,
        "evaluation": snapshot.get("evaluation"),
        "alerting": snapshot.get("alerting"),
        "graph_identity": snapshot.get("graph_identity"),
        "failed_jobs": failed_jobs,
        "trigger_source": (snapshot.get("trigger") or {}).get("source"),
        "trigger_summary": (snapshot.get("trigger") or {}).get("summary"),
    }


def _llm_cache_fingerprint(snapshot: dict[str, Any]) -> str:
    material = json.dumps(
        _llm_cache_material(snapshot),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def _load_llm_cache(
    fingerprint: str,
) -> tuple[list[InsightCandidate], dict[str, Any]] | None:
    ttl = settings.PROACTIVE_INSIGHTS_LLM_CACHE_TTL_SECONDS
    if ttl <= 0:
        return None
    try:
        raw = await redis_client.get(f"brain:proactive:llm-cache:{fingerprint}")
        payload = json.loads(raw) if raw else None
        if not isinstance(payload, dict):
            return None
        candidates = [InsightCandidate(**item) for item in payload.get("candidates") or [] if isinstance(item, dict)]
        status = dict(payload.get("status") or {})
        status.update(
            {
                "status": "cache_hit",
                "snapshot_fingerprint": fingerprint,
                "saved_llm_call": True,
            }
        )
        return candidates, status
    except Exception as exc:
        logger.warning(f"LLM diagnostic cache read skipped: {type(exc).__name__}")
        return None


async def _store_llm_cache(
    fingerprint: str,
    candidates: list[InsightCandidate],
    status: dict[str, Any],
) -> None:
    ttl = settings.PROACTIVE_INSIGHTS_LLM_CACHE_TTL_SECONDS
    if ttl <= 0:
        return
    payload = {
        "candidates": [candidate.to_record() for candidate in candidates],
        "status": status,
    }
    try:
        await redis_client.set(
            f"brain:proactive:llm-cache:{fingerprint}",
            json.dumps(payload, ensure_ascii=False),
            ex=ttl,
        )
    except Exception as exc:
        logger.warning(f"LLM diagnostic cache write skipped: {type(exc).__name__}")


def _parse_json_payload(raw: str) -> Any:
    payload, _ = _parse_json_payload_with_status(raw)
    return payload


def _parse_json_payload_with_status(raw: str) -> tuple[Any, bool]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text), False
    except json.JSONDecodeError as original_error:
        start_candidates = [idx for idx in (text.find("{"), text.find("[")) if idx >= 0]
        if start_candidates:
            start = min(start_candidates)
            end = max(text.rfind("}"), text.rfind("]"))
            if end > start:
                try:
                    return json.loads(text[start : end + 1]), False
                except json.JSONDecodeError:
                    pass

        recovered_items = _recover_complete_insight_items(text)
        if recovered_items:
            logger.warning(
                "Recovered {} complete LLM insight item(s) from malformed structured output",
                len(recovered_items),
            )
            return {"insights": recovered_items}, True
        raise original_error


def _recover_complete_insight_items(text: str) -> list[dict[str, Any]]:
    """Salvage only fully decoded list items from a truncated insight payload."""
    match = re.search(r'"insights"[ \t\r\n]*:[ \t\r\n]*\[', text)
    if match:
        cursor = match.end()
    else:
        # _llm_insights also accepts a bare top-level list. Recover it only
        # from the first array opening; fully decoded objects still pass the
        # normal evidence-grounding gate before becoming insights.
        cursor = text.find("[") + 1
        if cursor == 0:
            return []

    decoder = json.JSONDecoder()
    recovered: list[dict[str, Any]] = []
    limit = min(len(text), 64_000)
    while cursor < limit and len(recovered) < _MAX_LLM_INSIGHTS:
        while cursor < limit and text[cursor] in " \t\r\n,":
            cursor += 1
        if cursor >= limit or text[cursor] == "]":
            break
        try:
            item, end = decoder.raw_decode(text, cursor)
        except json.JSONDecodeError:
            break
        if isinstance(item, dict):
            recovered.append(item)
        cursor = end
    return recovered


def _candidate_from_llm_item(item: Any, *, model: str) -> InsightCandidate | None:
    if not isinstance(item, dict):
        return None
    evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
    if not evidence:
        return None
    try:
        return InsightCandidate(
            insight_type=item.get("insight_type") or item.get("type") or "llm_observation",
            severity=item.get("severity") or "info",
            title=item.get("title") or "LLM insight",
            summary=item.get("summary") or item.get("description") or item.get("title") or "LLM insight",
            evidence=evidence,
            recommended_action=item.get("recommended_action") or item.get("action"),
            confidence=item.get("confidence") or "medium",
            dedupe_key=item.get("dedupe_key"),
            source="llm",
            source_model=model,
        )
    except Exception as exc:
        logger.warning(f"Skipping invalid LLM insight item: {exc}")
        return None


def _merge_insights(items: Iterable[InsightCandidate]) -> list[InsightCandidate]:
    by_key: dict[str, InsightCandidate] = {}
    for item in items:
        if not item.evidence:
            continue
        key = item.dedupe_key
        if key and key not in by_key:
            by_key[key] = item
    severity_rank = {"critical": 0, "warning": 1, "info": 2, "ready": 3}
    return sorted(by_key.values(), key=lambda i: (severity_rank.get(i.severity, 2), i.title.lower()))[:12]


async def persist_insights(candidates: list[InsightCandidate]) -> dict[str, Any]:
    now = _utc_now()
    saved: list[dict[str, Any]] = []
    current_keys = [candidate.dedupe_key for candidate in candidates if candidate.dedupe_key]
    created = updated = staled = 0

    async with async_session_factory() as session:
        async with session.begin():
            await assert_current_db_fence(session)
            for candidate in candidates:
                row = (
                    await session.execute(select(BrainInsight).where(BrainInsight.dedupe_key == candidate.dedupe_key))
                ).scalar_one_or_none()
                if row is None:
                    row = BrainInsight(
                        insight_type=candidate.insight_type,
                        severity=candidate.severity,
                        title=candidate.title,
                        summary=candidate.summary,
                        evidence=candidate.evidence,
                        recommended_action=candidate.recommended_action,
                        confidence=candidate.confidence,
                        dedupe_key=candidate.dedupe_key,
                        status="new",
                        source=candidate.source,
                        source_model=candidate.source_model,
                        engine_version=candidate.engine_version,
                        occurrence_count=1,
                        last_seen_at=now,
                    )
                    session.add(row)
                    await session.flush()
                    created += 1
                else:
                    row.insight_type = candidate.insight_type
                    row.severity = candidate.severity
                    row.title = candidate.title
                    row.summary = candidate.summary
                    row.evidence = candidate.evidence
                    row.recommended_action = candidate.recommended_action
                    row.confidence = candidate.confidence
                    row.source = candidate.source
                    row.source_model = candidate.source_model
                    row.engine_version = candidate.engine_version
                    row.occurrence_count = (row.occurrence_count or 0) + 1
                    row.last_seen_at = now
                    row.updated_at = now
                    if row.status in {"stale", "actioned"}:
                        row.status = "new"
                    updated += 1
                saved.append(_row_to_public_dict(row))

            stale_stmt = update(BrainInsight).where(BrainInsight.status.in_(_ACTIVE_STATUSES))
            if current_keys:
                stale_stmt = stale_stmt.where(BrainInsight.dedupe_key.not_in(current_keys))
            result = await session.execute(stale_stmt.values(status="stale", updated_at=now))
            staled = cast(CursorResult[Any], result).rowcount or 0

    return {
        "created": created,
        "updated": updated,
        "staled": staled,
        "insights": saved,
    }


def _row_to_public_dict(row: BrainInsight) -> dict[str, Any]:
    return {
        "id": row.id,
        "insight_type": row.insight_type,
        "severity": row.severity,
        "title": row.title,
        "summary": row.summary,
        "evidence": row.evidence or [],
        "recommended_action": row.recommended_action,
        "confidence": row.confidence,
        "dedupe_key": row.dedupe_key,
        "status": row.status,
        "source": row.source,
        "source_model": row.source_model,
        "engine_version": row.engine_version,
        "occurrence_count": row.occurrence_count,
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
        "last_seen_at": _dt(row.last_seen_at),
    }


async def list_recent_insights(
    *,
    limit: int = 5,
    offset: int = 0,
    statuses: tuple[str, ...] | None = _ACTIVE_STATUSES,
    session: Any | None = None,
) -> list[dict[str, Any]]:
    """Newest findings first.

    `session` exists so a caller that already holds one can page and count in
    the same transaction. Opening a second session inside an open one is what
    broke the Command screen: SQLAlchemy refuses the nested execute with
    "Can't use AsyncSession.execute() with a server-side cursor", and the
    failure surfaced on an unrelated route.
    """

    def _stmt():
        stmt = select(BrainInsight).order_by(
            BrainInsight.last_seen_at.desc(), BrainInsight.id.desc()
        )
        if statuses:
            stmt = stmt.where(BrainInsight.status.in_(statuses))
        return stmt.limit(limit).offset(max(0, int(offset or 0)))

    if session is not None:
        rows = (await session.execute(_stmt())).scalars().all()
        return [_row_to_public_dict(row) for row in rows]
    async with async_session_factory() as own:
        own_rows: Iterable[BrainInsight] = (await own.execute(_stmt())).scalars().all()
        return [_row_to_public_dict(row) for row in own_rows]


async def generate_proactive_insights(
    *,
    repo_path: str | Path | None = None,
    use_llm: bool | None = None,
    persist: bool = True,
    trigger_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = await collect_proactive_snapshot(repo_path)
    snapshot["llm_route"] = {
        "provider": settings.DEFAULT_LLM_PROVIDER,
        "model": get_model_router().task_model(TaskKind.INSIGHT),
    }
    if trigger_context:
        snapshot["trigger"] = {
            "source": _clean_text(trigger_context.get("source"), limit=64),
            "summary": _clean_text(trigger_context.get("summary"), limit=1000),
            "reference": _clean_text(trigger_context.get("reference"), limit=255),
        }
    deterministic = deterministic_insights(snapshot)

    llm_status: dict[str, Any] = {
        "status": "skipped",
        "reason": "PROACTIVE_INSIGHTS_LLM_ENABLED=false",
        "model": get_model_router().task_model(TaskKind.INSIGHT),
    }
    llm_candidates: list[InsightCandidate] = []
    should_use_llm = settings.PROACTIVE_INSIGHTS_LLM_ENABLED if use_llm is None else bool(use_llm)
    if should_use_llm:
        fingerprint = _llm_cache_fingerprint(snapshot)
        cacheable = (snapshot.get("trigger") or {}).get("source") in {
            None,
            "",
            "scheduler",
            "job_failure",
        }
        cached = await _load_llm_cache(fingerprint) if cacheable else None
        if cached:
            llm_candidates, llm_status = cached
        else:
            try:
                llm_candidates, llm_status = await _llm_insights(snapshot)
                llm_status["snapshot_fingerprint"] = fingerprint
                if cacheable and not llm_status.get("parse_recovered"):
                    await _store_llm_cache(fingerprint, llm_candidates, llm_status)
            except Exception as exc:
                error = _exception_summary(exc)
                logger.warning(f"Proactive insight LLM stage failed: {error}")
                llm_status = {
                    "status": "failed",
                    "error": error,
                    "model": get_model_router().task_model(TaskKind.INSIGHT),
                    "snapshot_fingerprint": fingerprint,
                }

    if should_use_llm and llm_status.get("status") not in {
        "used",
        "completed",
        "cache_hit",
    }:
        llm_candidates.append(
            InsightCandidate(
                insight_type="llm_diagnostics",
                severity="warning",
                title="LLM self-diagnosis stage is unavailable",
                summary="Deterministic probes completed, but the configured LLM could not synthesize the diagnostic snapshot.",
                evidence=[
                    {"label": "llm_status", "value": llm_status.get("status")},
                    {"label": "model", "value": llm_status.get("model")},
                    {"label": "error", "value": llm_status.get("error") or llm_status.get("reason")},
                ],
                recommended_action="Check the configured LLM provider and retry the self-diagnosis job; deterministic findings remain valid.",
                confidence="high",
                dedupe_key="llm_diagnostics:stage_unavailable",
                source="deterministic",
            )
        )
    elif should_use_llm and llm_status.get("parse_recovered"):
        llm_candidates.append(
            InsightCandidate(
                insight_type="llm_diagnostics",
                severity="warning",
                title="LLM self-diagnosis output was truncated",
                summary=(
                    "Only fully decoded and evidence-grounded insights were retained; "
                    "the incomplete remainder was discarded and was not cached."
                ),
                evidence=[
                    {"label": "parse_recovered", "value": True},
                    {"label": "model", "value": llm_status.get("model")},
                    {"label": "retained_insights", "value": llm_status.get("count")},
                ],
                recommended_action=(
                    "Check the provider output limit and retry self-diagnosis; deterministic findings remain valid."
                ),
                confidence="high",
                dedupe_key="llm_diagnostics:truncated_output",
                source="deterministic",
            )
        )

    candidates = _merge_insights([*deterministic, *llm_candidates])
    if persist:
        try:
            persistence = await persist_insights(candidates)
        except Exception as exc:
            logger.warning(f"Insight persistence unavailable; continuing with alert payload: {type(exc).__name__}")
            persistence = {
                "created": 0,
                "updated": 0,
                "staled": 0,
                "insights": [candidate.to_public_dict() for candidate in candidates],
                "error": _clean_text(exc, limit=260),
            }
    else:
        persistence = {
            "created": 0,
            "updated": 0,
            "staled": 0,
            "insights": [candidate.to_public_dict() for candidate in candidates],
        }

    return {
        "engine_version": INSIGHT_ENGINE_VERSION,
        "generated_at": _utc_now().isoformat(),
        "proactive_enabled": settings.PROACTIVE_INSIGHTS_ENABLED,
        "llm": llm_status,
        "snapshot_summary": {
            "repo": snapshot.get("repo"),
            "services": {key: value.get("status") for key, value in (snapshot.get("services") or {}).items()},
            "counts": snapshot.get("counts"),
            "embeddings": {
                "missing_embeddings": (snapshot.get("embeddings") or {}).get("missing_embeddings"),
                "stale_embeddings": (snapshot.get("embeddings") or {}).get("stale_embeddings"),
                "pgvector_coverage_pct": (snapshot.get("embeddings") or {}).get("pgvector_coverage_pct"),
            },
            "trigger": snapshot.get("trigger"),
        },
        "deterministic_count": len(deterministic),
        "llm_count": len(llm_candidates),
        "persisted": persistence,
    }
