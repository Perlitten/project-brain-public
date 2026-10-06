"""Lifecycle and release-readiness endpoints: /health, /ready, /api/version
and the late-interaction operator status."""

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from apps.api.auth import require_api_key, require_scope
from brain import __version__
from brain.config.paths import resolve_repo_path
from brain.config.settings import settings
from brain.database.repository_utils import require_repository_by_path
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
from brain.version import build_info
from brain.workers.queue import worker_pool_status

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
    return {
        "status": status,
        "version": __version__,
        "details": details,
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
async def api_version():
    return build_info()


@router.get("/late-interaction/status", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def late_interaction_status(repo_path: str):
    """Operator evidence for canary coverage, provider health and fail-open use."""
    repository = await require_repository_by_path(resolve_repo_path(repo_path))
    return await collect_late_interaction_operator_status(repository)
