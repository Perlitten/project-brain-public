"""Read-only JSON API for the web UI (apps/web).

Every endpoint here replaces a screen of the retired server-rendered dashboard.
Values are read from the same stores the dashboard read; anything the system
does not record is returned as ``null`` rather than invented.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import String, case, cast, func, literal, select

from apps.api.auth import require_api_key, require_scope
from apps.api.helpers import (
    get_db_counts,
    get_embedding_retrieval_health,
    get_recent_brain_jobs,
    get_reports_list,
    get_self_diagnosis_status,
)
from brain.config.paths import context_packs_dir, get_repo_root, reports_dir
from brain.config.settings import settings
from brain.context.budget import estimated_tokens
from brain.database.models import ContextPack, Decision, Embedding, File, FileChunk, IndexingRun, Repository, Rule
from brain.database.session import async_session_factory, check_health
from brain.memory.repo_scope import repository_scope_clause

WEB_READ_SCOPE = "core:read"

router = APIRouter(
    prefix="/api/web",
    tags=["web"],
    dependencies=[Depends(require_api_key), Depends(require_scope(WEB_READ_SCOPE))],
)


@router.get("/dashboard")
async def web_dashboard(period: str = Query("24h"), repository_id: int | None = Query(None, ge=1)) -> dict[str, Any]:
    from apps.api.telemetry import dashboard_metrics
    days = {"24h": 1, "7d": 7, "30d": 30}.get(period)
    if days is None:
        raise HTTPException(status_code=400, detail="period must be one of 24h, 7d, 30d")
    if repository_id is not None:
        await _resolve_repository(repository_id)
    try:
        result = await dashboard_metrics(period_days=days, repository_id=repository_id)
    except Exception:
        # Dashboard observability must distinguish unavailable telemetry from an empty measured period.
        result = {
            "collection": {"collection_started_at": None, "until": None, "sample_count": None, "status": "unavailable"},
            "requests": {"total": None, "successes": None, "failures": None, "success_rate": None, "latency_ms": {"p50": None, "p95": None}, "clients": None, "by_operation": {}},
            "search": {"total": None, "successes": None, "failures": None, "sample_count": None, "latency_ms": {"p50": None, "p95": None}},
            "context": {"total": None, "successes": None, "failures": None, "sample_count": None, "latency_ms": {"p50": None, "p95": None}},
            "series": [],
        }
        unavailable = True
    else:
        unavailable = False
    result["scope"] = {"repository_id": repository_id, "mode": "repository" if repository_id is not None else "all"}
    result["collection_started_at"] = result["collection"]["collection_started_at"]
    result["observed_at"] = result["collection"]["until"]
    result["period"] = period
    result["partial"] = bool(result["collection"].get("partial", True))
    result["unavailable"] = unavailable
    result["total"] = result["requests"]["total"]
    result["failed"] = result["requests"]["failures"]
    result["clients"] = result["requests"]["clients"]
    result["context_total"] = result["context"]["total"]
    result["context_success"] = result["context"]["successes"]
    result["latency"] = {op: {"samples": data["sample_count"], "p50_ms": data["latency_ms"]["p50"], "p95_ms": data["latency_ms"]["p95"]} for op, data in (("search", result["search"]), ("context", result["context"]))}
    result["buckets"] = [{"at": item["bucket"], "total": item["requests"], "failed": item["failures"], "p95_ms": item.get("p95_ms"), "partial": item["partial"]} for item in result["series"]]
    result["recent_requests"] = result.get("recent_requests", [])
    return result

MODULE_GRAPH_EDGE_LIMIT = 50_000
ROOT_MODULE = "(root)"


# --------------------------------------------------------------------------- utils


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


# --------------------------------------------------------------------------- context packs


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
    """Return a regular file inside an allowed root, rejecting symlink hops."""
    candidate_absolute = Path(os.path.abspath(os.fspath(candidate)))
    for root in roots:
        try:
            root_absolute = Path(os.path.abspath(os.fspath(root)))
            relative = candidate_absolute.relative_to(root_absolute)
            current = root_absolute
            if current.is_symlink():
                continue
            hopped = False
            for part in relative.parts:
                current = current / part
                if current.is_symlink():
                    hopped = True
                    break
            if hopped:
                continue
            resolved = candidate_absolute.resolve(strict=True)
            resolved.relative_to(root_absolute.resolve(strict=True))
            if resolved.is_file():
                return resolved
        except (OSError, RuntimeError, ValueError):
            continue
    return None


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
    budget: str | None = None
    files: int | None = None
    tokens: int | None = None
    if resolved is not None:
        try:
            raw = resolved.read_bytes()
            tokens = estimated_tokens(len(raw))
            parsed = parse_context_pack_markdown(raw.decode("utf-8", errors="replace"))
            budget = parsed["budget"]
            files = parsed["files"]
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
        # Not recorded: context_packs has no consumer/agent column.
        "consumer": None,
        "path": pack.path,
        "available": resolved is not None,
    }


@router.get("/context-packs")
async def web_context_packs(
    limit: int = Query(50, ge=1, le=500),
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1, le=500),
    q: str | None = Query(None, max_length=500),
    repository_id: int | None = None,
    status: str | None = Query(None, max_length=50),
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


@router.get("/context-packs/{pack_id}")
async def web_context_pack_detail(pack_id: int) -> dict[str, Any]:
    async with async_session_factory() as session:
        pack = await session.get(ContextPack, pack_id)
        if pack is None:
            raise HTTPException(status_code=404, detail="Context pack not found")
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


# --------------------------------------------------------------------------- reports


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


def _decision_status(value: Any) -> str:
    raw = str(value or "").lower()
    if raw in {"active", "accepted"}:
        return "accepted"
    if raw in {"proposed", "draft", "pending"}:
        return "proposed"
    return (
        "superseded"
        if raw in {"deprecated", "superseded", "historical", "replaced", "inactive", "rejected"}
        else (raw or "unknown")
    )


def _rule_severity(value: Any) -> str:
    raw = str(value or "").lower()
    if raw in {"high", "error", "critical", "block", "blocker"}:
        return "block"
    if raw in {"medium", "warn", "warning"}:
        return "warn"
    return "advise"


@router.get("/decisions")
async def web_decisions(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
                        q: str | None = Query(None, max_length=500), status: str | None = Query(None, max_length=50),
                        repository_id: int | None = None) -> dict[str, Any]:
    async with async_session_factory() as session:
        base = select(Decision)
        if repository_id is not None:
            repository = await session.get(Repository, repository_id)
            if repository is None:
                raise HTTPException(status_code=404, detail="Repository not found")
            base = base.where(repository_scope_clause(Decision, repository.path))
        if q:
            needle = f"%{q.strip()}%"
            text = cast(Decision.id, String) + literal(" ") + func.coalesce(Decision.title, "") + literal(" ") + func.coalesce(Decision.description, "")
            base = base.where(text.ilike(needle))
        status_expr = case((func.lower(Decision.status).in_(("active", "accepted")), "accepted"),
                           (func.lower(Decision.status).in_(("proposed", "draft", "pending")), "proposed"),
                           (func.lower(Decision.status).in_(("deprecated", "superseded", "historical", "replaced", "inactive", "rejected")), "superseded"),
                           else_=func.coalesce(func.nullif(func.lower(Decision.status), ""), "unknown")).label("status")
        facet_stmt = select(status_expr, func.count()).select_from(Decision)
        if base.whereclause is not None:
            facet_stmt = facet_stmt.where(base.whereclause)
        facet_rows = (await session.execute(facet_stmt.group_by(status_expr))).all()
        filtered = base
        if status:
            filtered = filtered.where(status_expr == _decision_status(status))
        total = int((await session.execute(select(func.count()).select_from(filtered.subquery()))).scalar() or 0)
        rows = (await session.execute(filtered.order_by(Decision.id).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"total": total, "page": page, "page_size": page_size, "total_pages": max(1, (total + page_size - 1) // page_size),
            "facets": {"status": {str(k): int(v) for k, v in facet_rows}}, "scope": "repository" if repository_id is not None else "global",
            "decisions": [{"id": r.id, "title": r.title, "description": r.description, "status": _decision_status(r.status), "raw_status": r.status,
                           "repo_path": r.repo_path, "date": r.date.isoformat() if r.date else None, "reason": r.reason,
                           "consequences": r.consequences, "affected_features": r.affected_features, "affected_modules": r.affected_modules,
                           "affected_files": r.affected_files} for r in rows]}


@router.get("/rules")
async def web_rules(page: int = Query(1, ge=1, le=1000000), page_size: int = Query(50, ge=1, le=500),
                    q: str | None = Query(None, max_length=500), status: str | None = Query(None, max_length=50),
                    severity: str | None = Query(None, max_length=50), repository_id: int | None = None) -> dict[str, Any]:
    async with async_session_factory() as session:
        base = select(Rule).where(func.coalesce(func.lower(Rule.status), "").not_in(("inactive", "disabled", "deprecated")))
        if repository_id is not None:
            repository = await session.get(Repository, repository_id)
            if repository is None:
                raise HTTPException(status_code=404, detail="Repository not found")
            base = base.where(repository_scope_clause(Rule, repository.path))
        if q:
            needle = f"%{q.strip()}%"
            text = cast(Rule.id, String) + literal(" ") + func.coalesce(Rule.name, "") + literal(" ") + func.coalesce(Rule.description, "")
            base = base.where(text.ilike(needle))
        if status:
            base = base.where(func.lower(Rule.status) == status.lower())
        severity_expr = case((func.lower(Rule.severity).in_(("high", "error", "critical", "block", "blocker")), "block"),
                             (func.lower(Rule.severity).in_(("medium", "warn", "warning")), "warn"), else_="advise").label("severity")
        facet_stmt = select(severity_expr, func.count()).select_from(Rule)
        if base.whereclause is not None:
            facet_stmt = facet_stmt.where(base.whereclause)
        facet_rows = (await session.execute(facet_stmt.group_by(severity_expr))).all()
        filtered = base.where(severity_expr == _rule_severity(severity)) if severity else base
        total = int((await session.execute(select(func.count()).select_from(filtered.subquery()))).scalar() or 0)
        rows = (await session.execute(filtered.order_by(Rule.id).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"total": total, "page": page, "page_size": page_size, "total_pages": max(1, (total + page_size - 1) // page_size),
            "facets": {"severity": {str(k): int(v) for k, v in facet_rows}}, "scope": "repository" if repository_id is not None else "global",
            "rules": [{"id": r.id, "name": r.name, "description": r.description, "status": r.status, "severity": _rule_severity(r.severity),
                       "raw_severity": r.severity, "type": r.type, "applies_to": r.applies_to, "repo_path": r.repo_path} for r in rows]}

def report_kind(name: str) -> str:
    lowered = name.lower()
    if "audit" in lowered:
        return "audit"
    if "impact" in lowered:
        return "impact"
    if "diff" in lowered:
        return "diff review"
    return "report"


def shape_report(raw: dict[str, Any]) -> dict[str, Any]:
    name = str(raw.get("name") or "")
    created = raw.get("created_at")
    created_iso = None
    if isinstance(created, str) and created:
        try:
            # get_reports_list() formats the local mtime; keep it naive-local → ISO.
            created_iso = datetime.strptime(created, "%Y-%m-%d %H:%M:%S").isoformat()
        except ValueError:
            created_iso = created
    return {
        "id": name,
        "title": Path(name).stem if name else name,
        "kind": report_kind(name),
        "created_at": created_iso,
        "size_bytes": _as_int(raw.get("size")),
    }


@router.get("/reports")
async def web_reports(
    limit: int = Query(50, ge=1, le=500),
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1, le=500),
    q: str | None = Query(None, max_length=500),
) -> dict[str, Any]:
    try:
        raw_reports = get_reports_list()
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Reports folder could not be read") from exc
    size = page_size or limit
    if q and q.strip():
        needle = q.strip().casefold()
        raw_reports = [raw for raw in raw_reports if needle in str(raw.get("name", "")).casefold()]
    total = len(raw_reports)
    start = (page - 1) * size
    return {
        "reports": [shape_report(raw) for raw in raw_reports[start : start + size]],
        "total": total,
        "page": page,
        "page_size": size,
        "total_pages": max(1, (total + size - 1) // size),
        "scope": "global",
    }


@router.get("/reports/{report_name}")
async def web_report_detail(report_name: str) -> dict[str, Any]:
    if Path(report_name).name != report_name or report_name in {"", ".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid report name")
    allowed = reports_dir().resolve()
    candidate = _resolve_inside(allowed / report_name, [allowed])
    if candidate is None or candidate.suffix.lower() != ".md":
        raise HTTPException(status_code=404, detail="Report not found")
    raw = {
        "name": candidate.name,
        "created_at": datetime.fromtimestamp(candidate.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "size": candidate.stat().st_size,
    }
    try:
        content = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Report could not be read") from exc
    return {"report": shape_report(raw), "content": content}


# --------------------------------------------------------------------------- index runs


def shape_index_run(run: Any) -> dict[str, Any]:
    progress = _as_dict(run.progress)
    verification = _as_dict(run.verification)
    file_counts = _as_dict(run.file_counts) or _as_dict(verification.get("file_counts"))
    progress_files = _as_dict(progress.get("files"))
    progress_chunks = _as_dict(progress.get("chunks"))

    discovered = _as_int(file_counts.get("discovered"))
    if discovered is None:
        discovered = _as_int(progress_files.get("discovered"))
    changed = _as_int(file_counts.get("indexed"))
    chunks = _as_int(progress_chunks.get("processed"))

    completeness = None
    excluded = _as_int(file_counts.get("excluded")) or 0
    failed = _as_int(file_counts.get("failed"))
    if failed is None:
        failed = _as_int(progress_files.get("failed"))
    if discovered is not None and failed is not None:
        eligible = discovered - excluded
        if eligible > 0:
            completeness = round(max(eligible - failed, 0) / eligible, 4)

    duration = verification.get("duration_seconds")
    duration_seconds: float | None = float(duration) if isinstance(duration, (int, float)) else None
    if duration_seconds is None:
        started = _parse_dt(run.started_at)
        completed = _parse_dt(run.completed_at)
        if started is not None and completed is not None and completed >= started:
            duration_seconds = round((completed - started).total_seconds(), 3)

    return {
        "id": run.id,
        "repository_id": run.repository_id,
        "revision": run.commit_hash,
        # Not recorded: indexing_runs carries no trigger column.
        "trigger": None,
        "status": (run.status or "").lower() or None,
        "completeness": completeness,
        "files": discovered,
        "changed": changed,
        "chunks": chunks,
        "started_at": _iso(run.started_at),
        "duration_seconds": duration_seconds,
    }


@router.get("/index-runs")
async def web_index_runs(
    repository_id: int | None = None,
    limit: int = Query(20, ge=1, le=200),
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1, le=200),
    q: str | None = Query(None, max_length=500),
    status: str | None = Query(None, max_length=50),
) -> dict[str, Any]:
    repository = await _resolve_repository(repository_id)
    if repository is None:
        return {"repository": None, "runs": []}
    async with async_session_factory() as session:
        size = page_size or limit
        where = [IndexingRun.repository_id == repository.id]
        if q:
            needle = f"%{q.strip()}%"
            where.append(IndexingRun.commit_hash.ilike(needle) | cast(IndexingRun.id, String).ilike(needle))
        facet_where = list(where)
        if status:
            where.append(func.lower(IndexingRun.status) == status.lower())
        total = int((await session.execute(select(func.count()).select_from(IndexingRun).where(*where))).scalar() or 0)
        facet_rows = (
            await session.execute(
                select(IndexingRun.status, func.count())
                .where(*facet_where)
                .group_by(IndexingRun.status)
            )
        ).all()
        runs = (
            (
                await session.execute(
                    select(IndexingRun)
                    .where(*where)
                    .order_by(IndexingRun.id.desc())
                    .offset((page - 1) * size)
                    .limit(size)
                )
            )
            .scalars()
            .all()
        )
    return {
        "repository": _repository_dict(repository),
        "runs": [shape_index_run(run) for run in runs],
        "total": total,
        "page": page,
        "page_size": size,
        "facets": {str(k): int(v) for k, v in facet_rows},
        "total_pages": max(1, (total + size - 1) // size),
    }


# --------------------------------------------------------------------------- module graph


def _dotted(path: str) -> str:
    stem = path[:-3] if path.endswith(".py") else path
    return stem.replace("\\", "/").strip("/").replace("/", ".")


def file_edge_violates(source: str, target: str, rules: list[Any]) -> bool:
    """Same matching as DriftAnalyzer._evaluate_import, applied to a File→File edge."""
    imported = _dotted(target)
    for rule in rules:
        if not source.startswith(rule.source_layer_pattern.lstrip("/")):
            continue
        if rule.forbidden_import_pattern not in imported:
            continue
        if any(exc in source for exc in rule.allowed_exceptions):
            continue
        return True
    return False


def aggregate_module_edges(file_edges: list[tuple[str, str]], rules: list[Any]) -> list[dict[str, Any]]:
    weights: dict[tuple[str, str], int] = defaultdict(int)
    violations: dict[tuple[str, str], bool] = defaultdict(bool)
    for source, target in file_edges:
        key = (module_of(source), module_of(target))
        if key[0] == key[1]:
            continue
        weights[key] += 1
        if not violations[key] and file_edge_violates(source, target, rules):
            violations[key] = True
    edges: list[dict[str, Any]] = [
        {"from": src, "to": dst, "weight": weight, "violation": violations[(src, dst)]}
        for (src, dst), weight in weights.items()
    ]
    edges.sort(key=lambda edge: (-edge["weight"], edge["from"], edge["to"]))
    return edges


async def load_file_import_edges(repository_id: int) -> list[tuple[str, str]]:
    from brain.database.session import neo4j_driver

    query = """
    MATCH (a:File)-[:IMPORTS]->(b:File)
    WHERE a.repository_id = $repository_id AND b.repository_id = $repository_id
    RETURN a.name AS source, b.name AS target
    LIMIT $limit
    """
    edges: list[tuple[str, str]] = []
    async with neo4j_driver.session() as session:
        result = await session.run(query, repository_id=repository_id, limit=MODULE_GRAPH_EDGE_LIMIT)
        async for record in result:
            source, target = record["source"], record["target"]
            if isinstance(source, str) and isinstance(target, str):
                edges.append((source, target))
    return edges


@router.get("/module-graph")
async def web_module_graph(repository_id: int | None = None) -> dict[str, Any]:
    from brain.insights.drift_rules import get_default_rule_registry

    repository = await _resolve_repository(repository_id)
    if repository is None:
        return {"repository": None, "edges": [], "truncated": False}
    try:
        file_edges = await load_file_import_edges(repository.id)
    except Exception as exc:  # noqa: BLE001 — graph store down is a 503, not a 500
        raise HTTPException(status_code=503, detail="Graph store is unavailable") from exc
    rules = get_default_rule_registry().list_active_rules()
    return {
        "repository": _repository_dict(repository),
        "edges": aggregate_module_edges(file_edges, rules),
        "truncated": len(file_edges) >= MODULE_GRAPH_EDGE_LIMIT,
    }


# --------------------------------------------------------------------------- modules


def _add_module_row(row: tuple[Any, ...], modules: dict[str, dict[str, Any]], config: Any) -> None:
    from brain.embeddings.integrity import fast_embedding_reason
    from brain.search.filters import should_exclude_from_retrieval

    path, emb_id, dim, provider, model = row
    name = module_of(path)
    bucket = modules.setdefault(
        name,
        {"name": name, "chunks": 0, "current": 0, "outdated": 0, "missing": 0, "excluded": 0},
    )
    bucket["chunks"] += 1
    if should_exclude_from_retrieval(path):
        bucket["excluded"] += 1
        return
    reason = fast_embedding_reason(emb_id, dim, provider, model, None, config)
    bucket["current" if reason == "current" else "missing" if reason == "missing" else "outdated"] += 1


def aggregate_modules(rows: list[tuple[Any, ...]], config: Any) -> list[dict[str, Any]]:
    """rows: (path, embedding_id, dimension, provider, model) per chunk."""
    modules: dict[str, dict[str, Any]] = {}
    for row in rows:
        _add_module_row(row, modules, config)
    return sorted(modules.values(), key=lambda item: (-item["chunks"], item["name"]))


@router.get("/modules")
async def web_modules(repository_id: int | None = None) -> dict[str, Any]:
    from brain.embeddings.config import get_embedding_config

    repository = await _resolve_repository(repository_id)
    if repository is None:
        return {"repository": None, "modules": []}
    config = get_embedding_config()
    modules: dict[str, dict[str, Any]] = {}
    async with async_session_factory() as session:
        rows = await session.stream(
            select(File.path, Embedding.id, Embedding.dimension, Embedding.provider, Embedding.model)
            .select_from(FileChunk)
            .join(File, FileChunk.file_id == File.id)
            .outerjoin(Embedding, FileChunk.embedding_id == Embedding.id)
            .where(File.repository_id == repository.id)
            .execution_options(yield_per=1000)
        )
        async for row in rows:
            _add_module_row(tuple(row), modules, config)
    return {
        "repository": _repository_dict(repository),
        "modules": sorted(modules.values(), key=lambda item: (-item["chunks"], item["name"])),
    }


# --------------------------------------------------------------------------- overview


def shape_job(job: dict[str, Any]) -> dict[str, Any]:
    created = _parse_dt(job.get("created_at"))
    updated = _parse_dt(job.get("updated_at"))
    status = str(job.get("status") or "") or None
    duration = None
    if created is not None and updated is not None and status not in ("queued", "processing", "running"):
        delta = (updated - created).total_seconds()
        duration = round(delta, 3) if delta >= 0 else None
    detail: str | None = None
    error = job.get("error")
    result = job.get("result")
    if isinstance(error, str) and error.strip():
        detail = error.strip()
    elif isinstance(result, str) and result.strip():
        detail = result.strip()
    elif isinstance(result, dict):
        for key in ("message", "summary", "status"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                detail = value.strip()
                break
    if detail is not None and len(detail) > 300:
        detail = detail[:297] + "..."
    return {
        "id": job.get("id"),
        "kind": job.get("type"),
        "status": status,
        "started_at": _iso(created),
        "duration": duration,
        "detail": detail,
    }


async def _repository_freshness(record: Repository) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "indexed_commit": None,
        "head_commit": None,
        "commits_behind": None,
        "status": None,
        "note": None,
        "last_indexed_at": None,
    }
    try:
        from brain.memory.repo_freshness import assess_repository_freshness

        freshness = await assess_repository_freshness(record)
    except Exception:  # noqa: BLE001 — a failed probe is not a verdict
        return facts
    facts["indexed_commit"] = freshness.get("indexed_commit")
    facts["head_commit"] = freshness.get("source_head_commit")
    facts["commits_behind"] = _as_int(freshness.get("commits_behind"))
    facts["status"] = freshness.get("status")
    facts["note"] = freshness.get("note")
    last = freshness.get("last_indexed_at")
    facts["last_indexed_at"] = _iso(last) if isinstance(last, datetime) else last
    return facts


def build_coverage(embedding_health: dict[str, Any], total_chunks: int | None) -> dict[str, Any]:
    eligible = _as_int(embedding_health.get("total_eligible_chunks"))
    populated = _as_int(embedding_health.get("pgvector_populated"))
    current = _as_int(embedding_health.get("chunks_with_current_embeddings"))
    excluded = None
    if total_chunks is not None and eligible is not None:
        excluded = max(total_chunks - eligible, 0)
    return {
        "eligible_chunks": eligible,
        "vector": {"populated": populated, "pct": _pct(populated, eligible)},
        "retrieval": {"current": current, "pct": _pct(current, eligible)},
        "missing": _as_int(embedding_health.get("missing_embeddings")),
        "stale": _as_int(embedding_health.get("stale_embeddings")),
        "incompatible": _as_int(embedding_health.get("incompatible_embeddings")),
        "excluded": excluded,
        "vector_search_status": embedding_health.get("vector_search_status"),
        "pgvector_extension": embedding_health.get("pgvector_extension"),
        "pgvector_index_exists": embedding_health.get("pgvector_index_exists"),
        "issues": embedding_health.get("issues") or [],
    }


@router.get("/overview")
async def web_overview(
    repository_id: int | None = None,
    jobs_limit: int = Query(10, ge=1, le=100),
) -> dict[str, Any]:
    from brain.embeddings.config import get_embedding_config

    services = await check_health()

    try:
        repository = await _resolve_repository(repository_id)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — registry down: report services/jobs anyway
        repository = None

    counts: dict[str, Any] | None = None
    coverage: dict[str, Any] | None = None
    freshness: dict[str, Any] | None = None
    last_run: dict[str, Any] | None = None
    if repository is not None:
        embedding_health = await get_embedding_retrieval_health(
            repository_id=repository.id, repository_path=repository.path
        )
        async with async_session_factory() as session:
            counts = dict(await get_db_counts(session, repository_id=repository.id, repository_path=repository.path))
            run = (
                await session.execute(
                    select(IndexingRun)
                    .where(IndexingRun.repository_id == repository.id)
                    .order_by(IndexingRun.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        coverage = build_coverage(embedding_health, _as_int(counts.get("chunks")))
        freshness = await _repository_freshness(repository)
        last_run = shape_index_run(run) if run is not None else None
        index_moment = _parse_dt(freshness.get("last_indexed_at")) or _parse_dt(getattr(run, "started_at", None))
        freshness["index_age_seconds"] = (
            max(int((datetime.now(timezone.utc) - index_moment).total_seconds()), 0) if index_moment else None
        )

    config = get_embedding_config()
    job_data = await get_recent_brain_jobs(
        limit=jobs_limit,
        repository_id=repository.id if repository else None,
        repository_path=repository.path if repository else None,
    )
    return {
        "repository": _repository_dict(repository),
        "services": services,
        "counts": counts,
        "coverage": coverage,
        "freshness": freshness,
        "last_index_run": last_run,
        "embedding": {"provider": config.provider, "model": config.model, "dimension": config.dimension},
        "jobs": [shape_job(job) for job in job_data.get("jobs") or []],
        "jobs_error": job_data.get("error"),
    }


# --------------------------------------------------------------------------- health


@router.get("/health")
async def web_health() -> dict[str, Any]:
    from brain.database.session import redis_client
    from brain.workers.scheduler import scheduler_status

    services = await check_health()
    try:
        scheduler: dict[str, Any] = await scheduler_status(redis_client)
    except Exception as exc:  # noqa: BLE001 — Redis down is reported in services
        scheduler = {"error": f"{type(exc).__name__}: {exc}", "jobs": [], "stale": []}
    job_data = await get_recent_brain_jobs(limit=10)
    diagnostics = await get_self_diagnosis_status()
    return {
        "services": services,
        "orchestration": {
            "scheduler": scheduler,
            "recent_jobs": [shape_job(job) for job in job_data.get("jobs") or []],
            "redis_error": job_data.get("error"),
            "diagnostics": diagnostics,
        },
    }
