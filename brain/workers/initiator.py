"""Who asked for a queued job, and whether they still may (ADR docs/adr/team-collaboration.md).

Every enqueue records an origin on the job:

- ``principal`` — an API caller: principal id, credential id and the permission
  the API checked before admitting the job.
- ``system`` — work Brain starts on its own (scheduler, git webhook, failure
  routing, automatic reindex, the local stdio MCP server); ``source`` names it.

Jobs a running job enqueues (repairs, summary enrichment) inherit that job's
initiator, so revoking the initiator also stops the follow-ups. A job without
an origin was queued before this field existed: legacy.

Before every attempt the worker calls ``initiator_refusal``. In solo state
nothing changes. In team state a principal job is re-authorized through
``authorize_repository`` (live credential, membership, project role and
policy, revocation); a legacy job is refused because nobody is left to
re-authorize; a system job runs as before.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from brain.auth.principals import ResolvedPrincipal
from brain.config.settings import settings
from brain.team.authorization import Permission, TeamDenied, TeamState, authorize_repository, team_state

Origin = Literal["principal", "system"]
LEGACY_REFUSAL = ("Refused in team state: the job was queued before initiators were recorded, "
                  "so there is no principal to re-authorize")


@dataclass(frozen=True)
class JobInitiator:
    origin: Origin
    principal_id: int | None = None
    credential_id: int | None = None
    permission: str = ""
    source: str = ""

    @classmethod
    def for_principal(cls, caller: ResolvedPrincipal, permission: Permission) -> JobInitiator:
        """The caller the API authorized; a run principal is re-walked through its delegation chain."""
        return cls("principal", caller.id, caller.credential_id, permission.value)

    @classmethod
    def system(cls, source: str) -> JobInitiator:
        return cls("system", source=source)

    @classmethod
    def inherited(cls, source: str) -> JobInitiator | None:
        """The initiator of the job running in this context, else ``system(source)``."""
        running = current_initiator.get()
        return running if running is not _OUTSIDE_JOB else cls.system(source)

    @classmethod
    def from_job(cls, job: Mapping[str, Any]) -> JobInitiator | None:
        """``None`` for a legacy job. Malformed ids read as missing and fail re-authorization."""
        origin = str(job.get("initiator_origin") or "")
        if origin not in ("principal", "system"):
            return None
        return cls(origin, _int_or_none(job.get("initiator_principal_id")),
                   _int_or_none(job.get("initiator_credential_id")),
                   str(job.get("initiator_permission") or ""), str(job.get("initiator_source") or ""))

    def fields(self) -> dict[str, str]:
        """Flat string fields stored on the job record next to its other metadata."""
        return {
            "initiator_origin": self.origin,
            "initiator_principal_id": "" if self.principal_id is None else str(self.principal_id),
            "initiator_credential_id": "" if self.credential_id is None else str(self.credential_id),
            "initiator_permission": self.permission,
            "initiator_source": self.source,
        }


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


_OUTSIDE_JOB: Any = object()
# The worker sets this while a job executes; ``None`` there means a legacy job.
current_initiator: ContextVar[JobInitiator | None] = ContextVar("current_job_initiator", default=_OUTSIDE_JOB)


async def current_team_state() -> TeamState:
    """``"solo"`` or ``"team"`` right now; Lite storage is always solo and opens no session."""
    if settings.BRAIN_STORAGE_MODE != "full":
        return "solo"
    from brain.database.session import async_session_factory

    async with async_session_factory() as session:
        return await team_state(session)


async def initiator_refusal(job: Mapping[str, Any]) -> str | None:
    """Why this job must not run now, or ``None``. Database errors propagate: the caller retries later."""
    initiator = JobInitiator.from_job(job)
    if initiator is not None and initiator.origin == "system":
        return None
    if await current_team_state() == "solo":
        return None
    if initiator is None:
        return LEGACY_REFUSAL
    try:
        permission = Permission(initiator.permission)
    except ValueError:
        return "Refused in team state: the job records no valid permission to re-authorize"
    if initiator.principal_id is None:
        return "Initiator no longer authorized: A named live credential is required"
    caller = ResolvedPrincipal(initiator.principal_id, "", "", scopes=frozenset(),
                               credential_id=initiator.credential_id)
    params = job.get("params") or {}
    repo_path = params.get("repo_path") if isinstance(params, Mapping) else None
    from brain.database.repository_utils import find_repository_id
    from brain.database.session import async_session_factory

    async with async_session_factory() as session:
        # Same scope the API authorized: an unindexed or absent path is the unscoped surface.
        repository_id = await find_repository_id(session, repo_path) if repo_path else None
        try:
            await authorize_repository(session, caller, repository_id, permission)
        except TeamDenied as exc:
            return f"Initiator no longer authorized: {exc}"
    return None
