"""Shared router, access dependencies and shaping helpers for the web JSON API."""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from apps.api.auth import require_api_key, require_scope
from apps.api.team_access import repository_access, team_administrator_access
from brain.config.paths import context_packs_dir, get_repo_root
from brain.config.settings import settings
from brain.database.models import Repository
from brain.database.session import async_session_factory
from brain.team.authorization import Permission


WEB_READ_SCOPE = "core:read"


router = APIRouter(
    prefix="/api/web",
    tags=["web"],
    dependencies=[Depends(require_api_key), Depends(require_scope(WEB_READ_SCOPE))],
)


# Most screens are repository-scoped; without ``repository_id`` they span the
# whole Brain and therefore require a team administrator. Decisions and rules
# are the exception: their handlers filter an unscoped listing to the caller's
# readable repositories plus global memory.
REPOSITORY_READ = Depends(repository_access(Permission.READ))


# Reports and orchestration health span the whole Brain whatever repository the query names.
TEAM_ADMIN = Depends(team_administrator_access())

ROOT_MODULE = "(root)"


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _iso(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        raw = value.strip().replace("Z", "+00:00").replace(" UTC", "")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _pct(part: int | None, whole: int | None) -> float | None:
    """Coverage that never rounds an incomplete corpus up to 100."""
    if part is None or whole is None or whole <= 0:
        return None
    if part >= whole:
        return 100.0
    return min(int(part / whole * 1000) / 10.0, 99.9)


def module_of(path: str) -> str:
    """Module = first two directory segments of a repo-relative path."""
    parts = [part for part in str(path).replace("\\", "/").split("/") if part]
    directories = parts[:-1]
    if not directories:
        return ROOT_MODULE
    return "/".join(directories[:2])


def _repository_dict(record: Repository | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {"id": record.id, "name": record.name, "path": record.path}


async def _resolve_repository(repository_id: int | None) -> Repository | None:
    """The requested repository, else TARGET_REPO_PATH's row, else the first indexed one.

    An explicit id that does not exist is a 404, never a silent fallback.
    """
    from brain.database.repository_utils import get_repository_by_path

    async with async_session_factory() as session:
        if repository_id is not None:
            record = await session.get(Repository, repository_id)
            if record is None:
                raise HTTPException(status_code=404, detail="Repository not found")
            return record
    target = getattr(settings, "TARGET_REPO_PATH", None)
    if isinstance(target, str) and target.strip():
        record = await get_repository_by_path(target)
        if record is not None:
            return record
    async with async_session_factory() as session:
        return (await session.execute(select(Repository).order_by(Repository.id.asc()).limit(1))).scalar_one_or_none()


def _context_pack_roots() -> list[Path]:
    roots = [context_packs_dir(), get_repo_root() / "context_packs"]
    target_repo = getattr(settings, "TARGET_REPO_PATH", None)
    if isinstance(target_repo, str) and target_repo:
        roots.append(Path(target_repo).resolve() / "context_packs")
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = root.resolve().as_posix()
        if key not in seen:
            seen.add(key)
            deduped.append(root)
    return deduped


def _resolve_inside(candidate: Path, roots: list[Path]) -> Path | None:
    """Return a regular file inside an allowed root, rejecting symlink hops.

    Containment is proven twice on canonical strings with a trailing separator
    (before and after symlink resolution); the filesystem is only consulted for
    a candidate that already passed the lexical check.
    """
    for root in roots:
        try:
            root_absolute = os.path.abspath(os.fspath(root))
            root_prefix = os.path.join(root_absolute, "")
            candidate_absolute = os.path.abspath(os.fspath(candidate))
            if not candidate_absolute.startswith(root_prefix):
                continue
            if os.path.islink(root_absolute):
                continue
            current = Path(root_absolute)
            hopped = False
            for part in Path(candidate_absolute[len(root_prefix):]).parts:
                current = current / part
                if current.is_symlink():
                    hopped = True
                    break
            if hopped:
                continue
            resolved = os.path.join(os.path.realpath(candidate_absolute), "")
            if not resolved.startswith(os.path.join(os.path.realpath(root_absolute), "")):
                continue
            final = Path(resolved)
            if final.is_file():
                return final
        except (OSError, RuntimeError, ValueError):
            continue
    return None


def _paged_records(rows: list[Any], page: int, size: int, query: str | None) -> dict[str, Any]:
    if query:
        needle = query.casefold()
        rows = [
            r
            for r in rows
            if needle in str(getattr(r, "title", getattr(r, "name", ""))).casefold()
            or needle in str(getattr(r, "description", "")).casefold()
        ]
    total = len(rows)
    statuses: dict[str, int] = defaultdict(int)
    for row in rows:
        status = getattr(row, "status", None) or "unknown"
        statuses[str(status)] += 1
    start = (page - 1) * size
    return {
        "items": rows[start : start + size],
        "total": total,
        "page": page,
        "page_size": size,
        "total_pages": max(1, (total + size - 1) // size),
        "facets": {"status": dict(statuses)},
    }
