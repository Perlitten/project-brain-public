"""Read-only scheduler state and the post-merge reindex webhook (replaces n8n)."""

from __future__ import annotations

import hmac
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException

from apps.api.auth import require_api_key, require_scope
from apps.api.schemas import GitMergeWebhook, SchedulerJobsResponse
from brain.config.settings import settings
from brain.database.session import redis_client
from brain.workers.queue import queue_for_job
from brain.workers.scheduler import scheduler_status

router = APIRouter(tags=["scheduler"])

# The manual "Run now" endpoint for each scheduled job type (POST /jobs/*).
TRIGGER_PATHS = {
    "nightly_maintenance": "/jobs/nightly-maintenance",
    "health_check": "/jobs/health-check",
    "self_diagnosis": "/jobs/self-diagnosis",
    "benchmark": "/jobs/benchmark",
}


@router.get(
    "/scheduler/jobs",
    response_model=SchedulerJobsResponse,
    dependencies=[Depends(require_api_key), Depends(require_scope("jobs:read"))],
)
async def get_scheduler_jobs() -> SchedulerJobsResponse:
    state = await scheduler_status(redis_client)
    for job in state["jobs"]:
        job["trigger_path"] = TRIGGER_PATHS[job["job_type"]]
    return SchedulerJobsResponse.model_validate(state)


@router.post("/webhooks/git-merge")
async def git_merge_webhook(
    body: GitMergeWebhook,
    x_project_brain_token: Optional[str] = Header(default=None),
):
    """Post-merge reindex trigger for .github/workflows/project-brain-reindex.yml."""
    expected = (settings.PROJECT_BRAIN_WEBHOOK_TOKEN or "").strip()
    supplied = (x_project_brain_token or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="PROJECT_BRAIN_WEBHOOK_TOKEN is not configured")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid webhook token")
    if body.ref != "refs/heads/master":
        return {"status": "ignored", "reason": f"ref {body.ref} is not refs/heads/master"}
    revision = body.sha or body.after or ""
    queue = queue_for_job(
        redis_client, settings.WORKER_REDIS_PREFIX, "reindex", pools_enabled=settings.BRAIN_WORKER_POOLS_V2_ENABLED
    )
    job_id = await queue.enqueue(
        "reindex",
        {
            "clean": False,
            "repo_path": body.repo_path or settings.PROJECT_BRAIN_GIT_REPO_PATH,
            "source_revision": revision,
            "verify_after": True,
            "benchmark_after": True,
        },
        idempotency_key=f"git-reindex:{revision}" if revision else None,
        max_attempts=3,
    )
    return {"status": "queued", "job_id": job_id, "status_url": f"/jobs/{job_id}"}
