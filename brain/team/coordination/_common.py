"""Shared constants and pure path/task helpers for team coordination."""
from __future__ import annotations

import re
import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from brain.database.harness_models import AgentTask
from brain.team.authorization import TeamConflict
from brain.team.models import TeamIntegrationEntry, TeamTaskPlan


MAX_SCOPE_PATHS = 64


MAX_DEPENDENCIES = 32


MAX_SUBTASKS = 200


MAX_RESOLVED = 500


MIN_LEASE_SECONDS = 10


MAX_LEASE_SECONDS = 3600


# Changed files compared for overlap after an execution, and path pairs kept per warning.
MAX_CHANGED_PATHS = 5000


MAX_OVERLAP_PAIRS = 50


MAX_REASON = 500


# Dependency overlap: concrete file paths looked up in the import graph per check, imports kept per file, and the
# time budget for the whole lookup. Past either bound the check is partial and says so (``truncated``).
MAX_DEPENDENCY_PATHS = 32


MAX_DEPENDENCY_NEIGHBORS = 64


DEPENDENCY_TIMEOUT_SECONDS = 5.0


INACTIVE_TASK_STATUSES = ("completed", "cancelled")


PENDING = ("queued", "needs_rebase", "stale_instructions")


FINAL = ("integrated", "rejected")


_GLOB = re.compile(r"[*?\[]")


_DRIVE = re.compile(r"^[A-Za-z]:")


_RESOURCE = re.compile(r"^resource:[a-z0-9][a-z0-9._-]{0,127}$")


def _uuid(value: object, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise TeamConflict(f"Invalid {what}: {value!r}") from exc


def normalize_path(value: str, *, allow_glob: bool = True) -> str:
    """A repository-relative POSIX path; absolute paths, ``..`` and empty paths are refused."""
    raw = str(value).strip().replace("\\", "/")
    if not raw or len(raw) > 512 or raw.startswith("/") or _DRIVE.match(raw) or "\x00" in raw:
        raise TeamConflict(f"Scope path must be repository-relative: {value!r}")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        raise TeamConflict(f"Scope path must stay inside the repository: {value!r}")
    path = "/".join(parts)
    if not allow_glob and _GLOB.search(path):
        raise TeamConflict(f"Lease paths cannot be globs: {value!r}")
    return path


def normalize_scope(values: list[str]) -> list[str]:
    if len(values) > MAX_SCOPE_PATHS:
        raise TeamConflict(f"At most {MAX_SCOPE_PATHS} scope paths are allowed")
    return sorted({normalize_path(value) for value in values})


def normalize_changed_path(value: str) -> str:
    """Validate a literal repository-relative path reported by Git without rewriting its filename."""
    raw = str(value)
    if not raw or len(raw) > 4096 or raw.startswith("/") or _DRIVE.match(raw) or "\x00" in raw:
        raise TeamConflict(f"Changed path must be repository-relative: {value!r}")
    if any(part in ("", ".", "..") for part in raw.split("/")):
        raise TeamConflict(f"Changed path must stay inside the repository: {value!r}")
    return raw


def normalize_resource(value: str) -> str:
    """``resource:<name>`` for a named resource, otherwise a repository-relative path without globs."""
    raw = str(value).strip()
    if raw.startswith("resource:"):
        if not _RESOURCE.match(raw):
            raise TeamConflict("Resource names are lowercase letters, digits, '.', '_' or '-' after 'resource:'")
        return raw
    return normalize_path(raw, allow_glob=False)


def _static_prefix(path: str) -> str:
    match = _GLOB.search(path)
    if match is None:
        return path
    head = path[:match.start()]
    return head.rsplit("/", 1)[0] if "/" in head else ""


def paths_overlap(first: str, second: str) -> bool:
    """Whether two scope entries may touch the same files: equal, nested, or a glob reaching the other."""
    if first.startswith("resource:") or second.startswith("resource:"):
        return first == second
    a, b = _static_prefix(first), _static_prefix(second)
    if not a or not b:
        return True
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def overlapping(mine: list[str], theirs: list[str]) -> list[list[str]]:
    return [[a, b] for a in mine for b in theirs if paths_overlap(a, b)]


async def _done(session: AsyncSession, task_ids: list[uuid.UUID]) -> set[str]:
    """Tasks that are done: status ``completed``.

    Integrating a run sets that status (``integrate``); an integrated queue
    entry also counts so entries integrated before that existed stay done."""
    if not task_ids:
        return set()
    completed = (await session.execute(select(AgentTask.id).where(AgentTask.id.in_(task_ids),
                                                                  AgentTask.status == "completed"))).scalars().all()
    integrated = (await session.execute(select(TeamIntegrationEntry.task_id).where(
        TeamIntegrationEntry.task_id.in_(task_ids), TeamIntegrationEntry.status == "integrated"))).scalars().all()
    return {str(item) for item in (*completed, *integrated)}


def _related(first: TeamTaskPlan, second: TeamTaskPlan) -> bool:
    """Parent and child tasks share scope by construction; siblings and strangers are compared."""
    return first.parent_task_id == second.task_id or second.parent_task_id == first.task_id
