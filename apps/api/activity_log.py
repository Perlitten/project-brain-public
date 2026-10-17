"""Best-effort activity records for API actions that leave no dated database trace.

Queued jobs live in Redis with a TTL, so the team activity feed cannot read
them back (and must not filter a Redis listing after the fact). Each
principal-initiated enqueue appends one ``job.queued`` row to
``team_activity_events`` instead, bound to the job's repository so the feed
can authorize it like any other repository event; a job without a repository
is visible to team administrators only. A failure to record never fails the
enqueue that already happened.
"""
from __future__ import annotations

import logging

from apps.api import team_access
from brain.auth.principals import ResolvedPrincipal
from brain.config.settings import settings
from brain.database.repository_utils import find_repository_id
from brain.team.activity import record_event

logger = logging.getLogger(__name__)


async def record_job_queued(principal: ResolvedPrincipal, job_type: str, job_id: str, *,
                            repo_path: str | None = None, repository_id: int | None = None) -> None:
    if settings.BRAIN_STORAGE_MODE != "full":
        return  # team tables exist only under full storage
    try:
        # The team database the request was authorized against (one session factory for team state).
        async with team_access.async_session_factory() as session, session.begin():
            if repository_id is None and repo_path:
                repository_id = await find_repository_id(session, repo_path)
            author = principal.author_id
            await record_event(
                session, kind="job.queued", audience="repository", summary=f"Queued {job_type} job",
                actor_principal_id=author, actor_label=None if author else principal.actor,
                repository_id=repository_id,
                details={"job_id": job_id, "job_type": job_type, "repo_path": repo_path},
                dedupe_key=f"job:{job_id}",
            )
    except Exception as exc:  # noqa: BLE001 - the job is already queued; the record is best effort
        logger.warning("Could not record job activity for %s: %s: %s", job_id, type(exc).__name__, exc)
