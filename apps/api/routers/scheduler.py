"""Read-only scheduler state and the post-merge reindex webhook (replaces the retired external orchestrator)."""

from __future__ import annotations

import hashlib
import hmac

from fastapi import APIRouter, Depends, HTTPException, Request

from apps.api.auth import require_api_key, require_principal, require_scope
from apps.api.schemas import GitMergeWebhook, SchedulerJobsResponse
from apps.api.team_access import repository_authority, team_administrator_access
from brain.auth.principals import ResolvedPrincipal
from brain.config.settings import settings
from brain.database.session import redis_client
from brain.team.authorization import Permission
from brain.workers.queue import queue_for_job
from brain.workers.scheduler import scheduler_status

router = APIRouter(tags=["scheduler"])

# The manual "Run now" endpoint for each scheduled job type (POST /jobs/*).
TRIGGER_PATHS = {
    "nightly_maintenance": "/jobs/nightly-maintenance",
    "health_check": "/jobs/health-check",
    "self_diagnosis": "/jobs/self-diagnosis",
    "benchmark": "/jobs/benchmark",
    "memory_consolidation": "/jobs/memory-consolidation",
}


@router.get(
    "/scheduler/jobs",
    response_model=SchedulerJobsResponse,
    # Every schedule and its admin-only "Run now" trigger: a team administrator once members exist.
    dependencies=[Depends(require_api_key), Depends(require_scope("jobs:read")), Depends(team_administrator_access())],
)
async def get_scheduler_jobs() -> SchedulerJobsResponse:
    state = await scheduler_status(redis_client)
    for job in state["jobs"]:
        job["trigger_path"] = TRIGGER_PATHS[job["job_type"]]
    return SchedulerJobsResponse.model_validate(state)


def _github_signature_valid(raw_body: bytes, signature: str) -> bool:
    """GitHub-style ``X-Hub-Signature-256: sha256=<hmac>`` over the raw body.

    The shared secret is ``PROJECT_BRAIN_WEBHOOK_TOKEN``, the same env value the
    caller (GitHub Actions or a native GitHub webhook) signs with.
    """
    secret = (settings.PROJECT_BRAIN_WEBHOOK_TOKEN or "").strip()
    if not secret or not signature.strip().lower().startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


async def _api_key_principal(request: Request) -> ResolvedPrincipal | None:
    """The principal of a presented ``X-API-Key`` resolved like on any other route, else ``None``."""
    api_key = request.headers.get("x-api-key")
    if not api_key:
        return None
    try:
        return await require_principal(request, api_key)
    except Exception:
        # A bad key (401/503) or an unreachable credential store fails closed.
        return None


@router.post("/webhooks/git-merge")
async def git_merge_webhook(request: Request, body: GitMergeWebhook):
    """Post-merge reindex trigger for docs/examples/reindex-on-push.yml.

    Accepts a GitHub ``X-Hub-Signature-256`` HMAC over the raw body (secret
    ``PROJECT_BRAIN_WEBHOOK_TOKEN``) or the standard ``X-API-Key``; everything
    else is rejected. The signed path is the operator's own webhook secret; an
    ``X-API-Key`` caller requests a reindex like ``POST /jobs/reindex`` does.
    """
    raw_body = await request.body()
    signature = request.headers.get("x-hub-signature-256", "")
    repo_path = body.repo_path or settings.PROJECT_BRAIN_GIT_REPO_PATH
    if not _github_signature_valid(raw_body, signature):
        principal = await _api_key_principal(request)
        if principal is None:
            if not (settings.PROJECT_BRAIN_WEBHOOK_TOKEN or "").strip() and not (
                settings.PROJECT_BRAIN_API_KEY or ""
            ).strip():
                raise HTTPException(status_code=503, detail="No webhook credential is configured")
            raise HTTPException(status_code=401, detail="Invalid webhook credentials")
        await repository_authority(principal, Permission.REQUEST_RUN, repo_path=repo_path or None)
    expected_ref = settings.PROJECT_BRAIN_WEBHOOK_REF
    if body.ref != expected_ref:
        return {"status": "ignored", "reason": f"ref {body.ref} is not {expected_ref}"}
    revision = body.sha or body.after or ""
    queue = queue_for_job(
        redis_client, settings.WORKER_REDIS_PREFIX, "reindex", pools_enabled=settings.BRAIN_WORKER_POOLS_V2_ENABLED
    )
    job_id = await queue.enqueue(
        "reindex",
        {
            "clean": False,
            "repo_path": repo_path,
            "source_revision": revision,
            "verify_after": True,
            "benchmark_after": True,
        },
        idempotency_key=f"git-reindex:{revision}" if revision else None,
        max_attempts=3,
    )
    return {"status": "queued", "job_id": job_id, "status_url": f"/jobs/{job_id}"}
