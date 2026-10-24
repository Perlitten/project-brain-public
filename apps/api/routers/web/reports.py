"""Report listing and detail endpoints."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from fastapi import HTTPException, Query
from apps.api.helpers import get_reports_list
from brain.config.paths import reports_dir
from ._common import TEAM_ADMIN, _as_int, _resolve_inside, router


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


@router.get("/reports", dependencies=[TEAM_ADMIN])
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


@router.get("/reports/{report_name}", dependencies=[TEAM_ADMIN])
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
