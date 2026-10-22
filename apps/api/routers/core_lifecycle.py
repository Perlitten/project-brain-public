"""Lifecycle and release-readiness endpoints: /health, /ready, /api/version
and the late-interaction operator status."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from apps.api.auth import require_api_key, require_scope
from apps.api.repo_paths import repo_path_errors_as_400
from apps.api.team_access import current_team_state, repository_access
from brain import __version__
from brain.config.paths import resolve_repo_path
from brain.config.settings import settings
from brain.database.repository_utils import list_all_repositories, require_repository_by_path
from brain.database.session import check_health, redis_client
from brain.late_interaction.client import (
    get_late_interaction_client,
    late_interaction_release_active,
)
from brain.late_interaction.operator_status import (
    collect_late_interaction_operator_status,
)
from brain.late_interaction.provider import LfmColbertProvider
from brain.llm.presets import llm_provider_ready
from brain.memory.freshness_envelope import repository_age_report
from brain.team.authorization import Permission
from brain.team.restore_fence import current_team_access
from brain.version import build_info
from brain.workers.queue import worker_liveness, worker_pool_status

router = APIRouter()


@router.get("/health")
async def health_check():
    from brain.workers.scheduler import scheduler_status

    details = await check_health()
    is_healthy = all(svc.get("status") == "healthy" for svc in details.values())
    try:
        scheduler: dict[str, Any] = await scheduler_status(redis_client)
    except Exception as exc:  # noqa: BLE001 — Redis down is already reported in details
        scheduler = {"error": f"{type(exc).__name__}: {exc}", "stale": [], "jobs": []}
    status = "error" if not is_healthy else "degraded" if scheduler.get("stale") else "ok"
    try:
        # Index age per repository against the SLO; an old index degrades the
        # service without making it unhealthy (answers still carry freshness).
        freshness = repository_age_report(await list_all_repositories(), settings.BRAIN_FRESHNESS_MAX_AGE_S)
    except Exception as exc:  # noqa: BLE001 — the database outage is already reported in details
        freshness = {
            "max_age_s": settings.BRAIN_FRESHNESS_MAX_AGE_S, "exceeded": [], "repositories": [],
            "error": type(exc).__name__,
        }
    if status == "ok" and freshness.get("exceeded"):
        status = "degraded"
    try:
        worker: dict[str, Any] = await worker_liveness(
            redis_client, settings.WORKER_REDIS_PREFIX, pools_enabled=settings.BRAIN_WORKER_POOLS_V2_ENABLED
        )
    except Exception as exc:  # noqa: BLE001 — Redis down is already reported in details
        worker = {"running": None, "pools": None, "hint": "", "error": type(exc).__name__}
    try:
        # "solo" until the first team member joins, then "team" (unified team model).
        mode = await current_team_state()
    except Exception as exc:  # noqa: BLE001 — the database outage is already reported in details
        mode = f"unknown ({type(exc).__name__})"
    try:
        # "pending_reconciliation" after a team restore until `brain team reconcile` (no credential works).
        team_access = await current_team_access()
    except Exception as exc:  # noqa: BLE001 — the database outage is already reported in details
        team_access = f"unknown ({type(exc).__name__})"
    return {
        "status": status,
        "version": __version__,
        "mode": mode,
        "team_access": team_access,
        "details": details,
        "worker": worker,
        "freshness": freshness,
        "scheduler": {
            "enabled": scheduler.get("enabled"),
            "stale": scheduler.get("stale", []),
            "jobs": {j["job_type"]: j["status"] for j in scheduler.get("jobs", [])},
            **({"error": scheduler["error"]} if "error" in scheduler else {}),
        },
    }


@router.get("/ready")
async def readiness_check():
    """Strict production readiness: dependencies, release identity, worker, alerts."""
    details = await check_health()
    build = build_info()
    checks = {
        "datastores": all(svc.get("status") == "healthy" for svc in details.values()),
        "release_identity": (
            settings.ENVIRONMENT.lower() != "production"
            or (
                build.get("build_sha") not in {None, "", "unknown"}
                and build.get("source_digest") not in {None, "", "unknown"}
            )
        ),
        "self_diagnosis": (
            not settings.SELF_DIAGNOSIS_ENABLED
            or (
                settings.TELEGRAM_ALERTS_ENABLED
                and bool(settings.TELEGRAM_ALERT_BOT_TOKEN and settings.TELEGRAM_ALERT_CHAT_ID)
            )
        ),
        # Any OpenAI-compatible provider must have its base URL, model and
        # (unless it is a keyless local endpoint) API key configured.
        "llm_provider": (settings.ENVIRONMENT.lower() != "production" or llm_provider_ready(settings)[0]),
        "worker_heartbeat": True,
    }
    worker_pools = {}
    if settings.ENVIRONMENT.lower() == "production":
        try:
            if settings.BRAIN_WORKER_POOLS_V2_ENABLED:
                worker_pools = await worker_pool_status(
                    redis_client,
                    settings.WORKER_REDIS_PREFIX,
                    pools_enabled=True,
                )
                checks["worker_heartbeat"] = all(pool["heartbeat"] for pool in worker_pools.values())
                checks["worker_capacity"] = all(pool["ready"] for pool in worker_pools.values())
            else:
                # The legacy single worker publishes only a heartbeat, not
                # queue capacity. Do not call the v2 statistics path before
                # its flag is enabled: that would turn a healthy existing
                # deployment into a false negative during the rollout.
                heartbeat = await redis_client.get(f"{settings.WORKER_REDIS_PREFIX}:heartbeat")
                checks["worker_heartbeat"] = bool(heartbeat)
                worker_pools = {"maintenance": {"heartbeat": bool(heartbeat), "legacy": True}}
        except Exception:
            checks["worker_heartbeat"] = False
            if settings.BRAIN_WORKER_POOLS_V2_ENABLED:
                checks["worker_capacity"] = False
    optional_dependencies = {"late_interaction_provider": "disabled"}
    if late_interaction_release_active():
        strict_release_readiness = settings.ENVIRONMENT.lower() == "production"
        if settings.LATE_INTERACTION_REMOTE_ENABLED:
            repository_id = settings.LATE_INTERACTION_APPROVED_REPOSITORY_ID
            if repository_id is None:
                late_health = await get_late_interaction_client().ready()
                healthy = late_health.status == "ready"
            else:
                late_health = await get_late_interaction_client().status(repository_id)
                healthy = late_health.status == "ready"
            if strict_release_readiness:
                checks["late_interaction_release"] = healthy
            optional_dependencies["late_interaction_provider"] = "healthy" if healthy else "unhealthy"
        else:
            probe = LfmColbertProvider(timeout_s=2.0)
            try:
                late_health = await probe.health()
                optional_dependencies["late_interaction_provider"] = late_health["status"]
                if strict_release_readiness:
                    checks["late_interaction_release"] = late_health["status"] in {
                        "healthy",
                        "ok",
                        "ready",
                    }
            except Exception:
                optional_dependencies["late_interaction_provider"] = "unhealthy"
                if strict_release_readiness:
                    checks["late_interaction_release"] = False
            finally:
                await probe.aclose()

    ready = all(checks.values())
    payload = {
        "status": "ready" if ready else "not_ready",
        "checks": checks,
        "optional_dependencies": optional_dependencies,
        "worker_pools": worker_pools,
        "build": build,
    }
    return JSONResponse(status_code=200 if ready else 503, content=payload)


@router.get("/api/version")
async def api_version() -> dict[str, Any]:
    return {
        **build_info(),
        # Supported server contracts, not the caller's authorization. Every
        # operation still checks its scopes, membership and project role.
        "capabilities": {
            "team": True, "team_bootstrap": True, "team_activity": True,
            "team_board": True, "knowledge_review": True,
            "repository_registration": True,
        },
    }


@router.get("/late-interaction/status", dependencies=[Depends(require_api_key), Depends(require_scope("core:read")),
                                                      Depends(repository_access(Permission.READ))])
async def late_interaction_status(repo_path: str):
    """Operator evidence for canary coverage, provider health and fail-open use."""
    with repo_path_errors_as_400():
        target = resolve_repo_path(repo_path)
    try:
        repository = await require_repository_by_path(target)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return await collect_late_interaction_operator_status(repository)
