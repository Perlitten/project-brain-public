"""Context-pack listing, detail and file resolution endpoints."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from fastapi import Depends, HTTPException, Query
from sqlalchemy import String, case, cast, func, select
from apps.api import team_access
from apps.api.auth import require_principal
from apps.api.team_access import filters_unreadable, repository_authority
from brain.auth.principals import ResolvedPrincipal
from brain.context.budget import estimated_tokens
from brain.database.models import ContextPack, IndexingRun, Repository
from brain.database.session import async_session_factory
from brain.team.authorization import Permission
from ._common import REPOSITORY_READ, _context_pack_roots, _iso, _parse_dt, _resolve_inside, router


def resolve_context_pack_file(stored_path: str | None) -> Path | None:
    if not stored_path:
        return None
    roots = _context_pack_roots()
    candidates = [Path(stored_path)] + [root / Path(stored_path).name for root in roots]
    for candidate in candidates:
        resolved = _resolve_inside(candidate, roots)
        if resolved is not None:
            return resolved
    return None


_BUDGET_RE = re.compile(r"^\s*-\s*\*\*Token Budget Mode\*\*:\s*(\S+)", re.MULTILINE)


def parse_context_pack_markdown(text: str) -> dict[str, Any]:
    """Budget mode and file count as written by ContextPackBuilder."""
    budget_match = _BUDGET_RE.search(text)
    files = 0
    in_files = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("###"):
            in_files = stripped == "### Files"
            continue
        if stripped.startswith("## "):
            in_files = False
            continue
        if in_files and line.startswith("- [") and not line.startswith("- [ ]"):
            files += 1
    return {"budget": budget_match.group(1) if budget_match else None, "files": files}


def shape_context_pack(pack: Any, resolved: Path | None, index_commit: str | None) -> dict[str, Any]:
    # Prefer what the builder recorded: it is what the agent was actually given,
    # it survives the artifact being moved or pruned, and it is the same number
    # the aggregate reports. Re-reading the file is the fallback for packs built
    # before that accounting existed.
    budget: str | None = getattr(pack, "budget", None)
    files: int | None = getattr(pack, "file_count", None)
    tokens: int | None = getattr(pack, "token_estimate", None)
    if resolved is not None and (budget is None or files is None or tokens is None):
        try:
            raw = resolved.read_bytes()
            tokens = tokens if tokens is not None else estimated_tokens(len(raw))
            parsed = parse_context_pack_markdown(raw.decode("utf-8", errors="replace"))
            budget = budget if budget is not None else parsed["budget"]
            files = files if files is not None else parsed["files"]
        except OSError:
            pass
    created = _parse_dt(pack.created_at)
    stale = None
    pack_commit = getattr(pack, "repo_commit", None)
    if pack_commit and index_commit:
        stale = pack_commit != index_commit
    return {
        "id": pack.id,
        "task": pack.task_description,
        "budget": budget,
        "tokens": tokens,
        "files": files,
        "created_at": _iso(created),
        "stale": stale,
        # Lets the dashboard correlate a pack with its project without
        # exposing any repository beyond the rows this caller may already read.
        "repository_id": getattr(pack, "repository_id", None),
        # Null when the transport that asked did not identify itself, or when
        # the pack predates attribution — in neither case is it "nobody".
        "consumer": getattr(pack, "consumer", None),
        "memory": {
            "rules": getattr(pack, "rule_count", None),
            "decisions": getattr(pack, "decision_count", None),
            "learnings": getattr(pack, "learning_count", None),
        },
        "symbols": getattr(pack, "symbol_count", None),
        "path": pack.path,
        "available": resolved is not None,
    }


@router.get("/context-packs", dependencies=[REPOSITORY_READ])
@filters_unreadable
async def web_context_packs(
    limit: int = Query(50, ge=1, le=500),
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1, le=500),
    q: str | None = Query(None, max_length=500),
    repository_id: int | None = None,
    status: str | None = Query(None, max_length=50),
    principal: ResolvedPrincipal = Depends(require_principal),
) -> dict[str, Any]:
    # Direct callers in the unit suite invoke the coroutine without FastAPI's
    # dependency conversion, so Query defaults may still be present.
    page_value = page if isinstance(page, int) else 1
    size = (page_size if isinstance(page_size, int) else None) or limit
    if not isinstance(size, int):
        size = 50
    query = q.strip() if isinstance(q, str) else ""
    async with async_session_factory() as session:
        base = select(ContextPack)
        if repository_id is not None:
            base = base.where(ContextPack.repository_id == repository_id)
        elif isinstance(principal, ResolvedPrincipal):
            # Every repository: a team member sees the packs of repositories they can read.
            all_ids = list((await session.execute(select(Repository.id))).scalars())
            readable = await team_access.readable_repository_ids(principal, all_ids)
            if readable is not None:
                base = base.where(ContextPack.repository_id.in_(readable))
        if query:
            needle = f"%{query}%"
            base = base.where(
                (ContextPack.task_description.ilike(needle)) | (cast(ContextPack.id, String).ilike(needle))
            )
        latest_commit = (
            select(IndexingRun.commit_hash)
            .where(
                IndexingRun.repository_id == ContextPack.repository_id, func.lower(IndexingRun.status) == "completed"
            )
            .order_by(IndexingRun.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        freshness = case(
            (ContextPack.repository_id.is_(None), "unknown"),
            (ContextPack.repo_commit.is_(None), "unknown"),
            (ContextPack.repo_commit == "", "unknown"),
            (latest_commit.is_(None), "unknown"),
            (ContextPack.repo_commit == latest_commit, "fresh"),
            else_="outdated",
        ).label("freshness")
        facet_base = base
        if isinstance(status, str) and status:
            normalized_status = status.lower()
            if normalized_status not in {"fresh", "outdated", "unknown"}:
                raise HTTPException(status_code=400, detail="Unknown context pack freshness status")
            base = base.where(freshness == normalized_status)
        count_base = base.subquery()
        total = int((await session.execute(select(func.count()).select_from(count_base))).scalar() or 0)
        facet_stmt = select(freshness, func.count()).select_from(ContextPack)
        if facet_base.whereclause is not None:
            facet_stmt = facet_stmt.where(facet_base.whereclause)
        facet_rows = (await session.execute(facet_stmt.group_by(freshness))).all()
        packs = (
            (
                await session.execute(
                    base.order_by(ContextPack.created_at.desc()).offset((page_value - 1) * size).limit(size)
                )
            )
            .scalars()
            .all()
        )
        repo_ids = {pack.repository_id for pack in packs if pack.repository_id is not None}
        latest_commits: dict[int | None, str | None] = {}
        if repo_ids:
            ranked = (
                select(
                    IndexingRun.repository_id,
                    IndexingRun.commit_hash,
                    func.row_number()
                    .over(partition_by=IndexingRun.repository_id, order_by=IndexingRun.id.desc())
                    .label("rank"),
                )
                .where(IndexingRun.repository_id.in_(repo_ids), func.lower(IndexingRun.status) == "completed")
                .subquery()
            )
            rows = (
                await session.execute(select(ranked.c.repository_id, ranked.c.commit_hash).where(ranked.c.rank == 1))
            ).all()
            latest_commits = {repo_id: commit for repo_id, commit in rows}
    shaped = [
        shape_context_pack(
            pack,
            resolve_context_pack_file(pack.path),
            latest_commits.get(pack.repository_id) if pack.repository_id is not None else None,
        )
        for pack in packs
    ]
    facets = {"freshness": {str(key): int(value) for key, value in facet_rows}}
    for key in ("fresh", "outdated", "unknown"):
        facets["freshness"].setdefault(key, 0)
    return {
        "packs": shaped,
        "total": total,
        "page": page_value,
        "page_size": size,
        "total_pages": max(1, (total + size - 1) // size),
        "facets": facets,
    }


@router.get("/context-packs/{pack_id}", dependencies=[REPOSITORY_READ])
async def web_context_pack_detail(pack_id: int,
                                  principal: ResolvedPrincipal = Depends(require_principal)) -> dict[str, Any]:
    async with async_session_factory() as session:
        pack = await session.get(ContextPack, pack_id)
        if pack is None:
            raise HTTPException(status_code=404, detail="Context pack not found")
        # The router-level check covers the ``repository_id`` query parameter, not
        # the pack's own repository: authorize that too, and answer a pack the
        # caller may not read exactly like a missing one.
        try:
            await repository_authority(principal, Permission.READ, repository_id=pack.repository_id)
        except HTTPException as exc:
            raise HTTPException(status_code=404, detail="Context pack not found") from exc
        resolved = resolve_context_pack_file(pack.path)
        if resolved is None:
            raise HTTPException(status_code=404, detail="Context pack content is unavailable")
        latest = None
        if pack.repository_id is not None:
            latest = (
                await session.execute(
                    select(IndexingRun.commit_hash)
                    .where(
                        IndexingRun.repository_id == pack.repository_id,
                        func.lower(IndexingRun.status) == "completed",
                    )
                    .order_by(IndexingRun.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Context pack content could not be read") from exc
    return {"pack": shape_context_pack(pack, resolved, latest), "content": content}
