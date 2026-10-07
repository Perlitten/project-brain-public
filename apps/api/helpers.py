import json
import re
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.config.paths import get_repo_root, reports_dir
from brain.config.settings import settings
from brain.context.context_pack_builder import ContextPackBuilder
from brain.database.models import (
    ContextPack,
    Decision,
    DiffReview,
    Embedding,
    File,
    FileChunk,
    Rule,
    Symbol,
)


def _iter_log_lines(log_file, *, newest_first: bool):
    """Yield a large log in either direction without loading it into memory."""
    if not newest_first:
        with open(log_file, "r", encoding="utf-8", errors="ignore") as stream:
            yield from stream
        return

    block_size = 64 * 1024
    with open(log_file, "rb") as stream:
        stream.seek(0, 2)
        position = stream.tell()
        buffer = b""
        while position:
            read_size = min(block_size, position)
            position -= read_size
            stream.seek(position)
            buffer = stream.read(read_size) + buffer
            lines = buffer.split(b"\n")
            buffer = lines[0]
            for raw_line in reversed(lines[1:]):
                yield raw_line.decode("utf-8", errors="ignore")
        if buffer:
            yield buffer.decode("utf-8", errors="ignore")


def _parse_log_line(line: str) -> Dict[str, str] | None:
    line = line.strip()
    if not line:
        return None
    parts = line.split(" | ", 2)
    if len(parts) < 3:
        return None
    timestamp = parts[0].strip()
    level = parts[1].strip()
    rest = parts[2].strip()
    rest_parts = rest.split(" - ", 1)
    component = rest_parts[0].strip() if len(rest_parts) > 1 else "unknown"
    message = rest_parts[1].strip() if len(rest_parts) > 1 else rest
    return {"timestamp": timestamp, "level": level, "component": component, "message": message}


def read_recent_logs(
    level: Optional[str] = None,
    component: Optional[str] = None,
    limit: int = 100,
    page: int = 1,
    page_size: int = 25,
    direction: str = "desc",
) -> Dict[str, Any]:
    """Read one bounded log page while counting the complete filtered set."""
    log_file = reports_dir() / "brain.log"
    if not log_file.exists():
        return {"logs": [], "total": 0, "page": 1, "page_size": page_size, "total_pages": 1, "level_counts": {}, "error": None}

    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 25), 100))
    component_query = (component or "").casefold()

    def scan(target_page: int):
        page_logs = []
        total = 0
        level_counts: Dict[str, int] = {}
        start = (target_page - 1) * page_size
        stop = start + page_size
        for raw_line in _iter_log_lines(log_file, newest_first=direction != "asc"):
            event = _parse_log_line(raw_line)
            if event is None:
                continue
            if component_query and component_query not in event["component"].casefold() and component_query not in event["message"].casefold():
                continue
            event_level = event["level"]
            level_counts[event_level] = level_counts.get(event_level, 0) + 1
            if level and level != event_level:
                continue
            if start <= total < stop:
                page_logs.append(event)
            total += 1
        return page_logs, total, level_counts

    error = None
    try:
        logs, total, level_counts = scan(page)
    except Exception as exc:
        logger.error(f"Error reading log file: {exc}")
        logs, total, level_counts = [], 0, {}
        error = "The event log is temporarily unavailable. Existing services continue to run."

    total_pages = max(1, (total + page_size - 1) // page_size)
    clamped_page = min(page, total_pages)
    if clamped_page != page and error is None:
        logs, total, level_counts = scan(clamped_page)
    page = clamped_page
    return {
        "logs": logs,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "level_counts": level_counts,
        "error": error,
    }

def get_reports_list() -> List[Dict[str, Any]]:
    reports_path = reports_dir()
    if not reports_path.exists():
        return []

    reports: List[Dict[str, Any]] = []
    for file_obj in reports_path.glob("*.md"):
        stat = file_obj.stat()
        reports.append(
            {
                "name": file_obj.name,
                "created_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "size": stat.st_size,
            }
        )
    reports.sort(key=lambda item: item["created_at"], reverse=True)
    return reports


async def get_db_counts(
    session: AsyncSession,
    *,
    repository_id: int | None = None,
    repository_path: str | None = None,
) -> Dict[str, int]:
    """Count dashboard records, optionally scoped to one indexed repository.

    File-backed entities have an explicit repository lineage and are filtered
    through it. Normative memory intentionally includes global rows alongside
    rows owned by the selected repository. Context packs and diff reviews do
    not yet carry repository identity, so their counts remain global and the
    dashboard labels them as such instead of attributing them to a repository.
    """
    if repository_id is None:
        statements = {
            "files": select(func.count()).select_from(File),
            "chunks": select(func.count()).select_from(FileChunk),
            "symbols": select(func.count()).select_from(Symbol),
            "embeddings": select(func.count()).select_from(Embedding),
            "decisions": select(func.count()).select_from(Decision),
            "rules": select(func.count()).select_from(Rule),
            "context_packs": select(func.count()).select_from(ContextPack),
            "diff_reviews": select(func.count()).select_from(DiffReview),
        }
    else:
        from brain.memory.repo_scope import repository_scope_clause

        statements = {
            "files": (
                select(func.count())
                .select_from(File)
                .where(File.repository_id == repository_id)
            ),
            "chunks": (
                select(func.count())
                .select_from(FileChunk)
                .join(File, FileChunk.file_id == File.id)
                .where(File.repository_id == repository_id)
            ),
            "symbols": (
                select(func.count())
                .select_from(Symbol)
                .join(File, Symbol.file_id == File.id)
                .where(File.repository_id == repository_id)
            ),
            # The command screen's vector figure describes retrievable chunks,
            # not unrelated embedding rows such as cards from another repo.
            "embeddings": (
                select(func.count())
                .select_from(FileChunk)
                .join(File, FileChunk.file_id == File.id)
                .where(
                    File.repository_id == repository_id,
                    FileChunk.embedding_id.is_not(None),
                )
            ),
            "decisions": (
                select(func.count())
                .select_from(Decision)
                .where(repository_scope_clause(Decision, repository_path))
            ),
            "rules": (
                select(func.count())
                .select_from(Rule)
                .where(repository_scope_clause(Rule, repository_path))
            ),
            # These schemas are global. Keep the real number, but callers must
            # disclose its scope rather than presenting it as repository data.
            "context_packs": select(func.count()).select_from(ContextPack),
            "diff_reviews": select(func.count()).select_from(DiffReview),
        }
    counts = {name: 0 for name in statements}
    for name, stmt in statements.items():
        try:
            res = await session.execute(stmt)
            counts[name] = res.scalar() or 0
        except Exception as exc:
            logger.error(f"Error counting {name} in DB: {exc}")
            # PostgreSQL marks the whole transaction as failed after errors such
            # as a deadlock. Clear that state before the dashboard issues its
            # next count or query through the same session.
            try:
                await session.rollback()
            except Exception as rollback_exc:
                logger.error(f"Rollback after {name} count failure did not succeed: {rollback_exc}")
    return counts


async def get_neo4j_counts(repository_id: Optional[int] = None) -> Dict[str, Any]:
    from brain.database.session import neo4j_driver

    node_counts = {}
    rel_counts = {}
    total_nodes = 0
    total_rels = 0
    try:
        async with neo4j_driver.session() as session:
            node_where = " WHERE n.repository_id = $repository_id" if repository_id is not None else ""
            n_res = await session.run(
                f"MATCH (n){node_where} RETURN labels(n)[0] AS label, count(*) AS count",
                repository_id=repository_id,
            )
            async for record in n_res:
                lbl = record["label"] or "Unknown"
                cnt = record["count"]
                node_counts[lbl] = cnt
                total_nodes += cnt

            rel_where = (
                " WHERE a.repository_id = $repository_id AND b.repository_id = $repository_id"
                if repository_id is not None
                else ""
            )
            r_res = await session.run(
                f"MATCH (a)-[r]->(b){rel_where} RETURN type(r) AS type, count(*) AS count",
                repository_id=repository_id,
            )
            async for record in r_res:
                t = record["type"]
                cnt = record["count"]
                rel_counts[t] = cnt
                total_rels += cnt
    except Exception as exc:
        logger.error(f"Failed to get Neo4j counts: {exc}")
    return {
        "nodes": node_counts,
        "relationships": rel_counts,
        "total_nodes": total_nodes,
        "total_relationships": total_rels,
    }


def format_datetime_utc(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


async def get_top_connected_nodes(
    limit: int = 10,
    repository_id: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    from brain.database.session import neo4j_driver

    query = """
    MATCH (n)
    WHERE $repository_id IS NULL
       OR n.repository_id = $repository_id
    WITH n, COUNT { (n)--() } AS degree
    RETURN n.name AS name, labels(n)[0] AS label, degree
    ORDER BY degree DESC
    LIMIT $limit
    """
    results = []
    try:
        async with neo4j_driver.session() as session:
            res = await session.run(query, limit=limit, repository_id=repository_id)
            async for record in res:
                results.append(
                    {
                        "name": record["name"] or "Unknown",
                        "label": record["label"] or "Unknown",
                        "degree": record["degree"],
                    }
                )
    except Exception as exc:
        logger.error(f"Failed to get top connected nodes: {exc}")
        return [], str(exc)
    return results, None


async def get_orphan_nodes(
    limit: int = 10,
    repository_id: Optional[int] = None,
) -> Tuple[List[str], Optional[str]]:
    from brain.database.session import neo4j_driver

    query = """
    MATCH (f:File)
    WHERE ($repository_id IS NULL
       OR f.repository_id = $repository_id)
      AND COUNT { (f)--() } = 0
    RETURN f.name AS name
    LIMIT $limit
    """
    results = []
    try:
        async with neo4j_driver.session() as session:
            res = await session.run(query, limit=limit, repository_id=repository_id)
            async for record in res:
                if record["name"]:
                    results.append(record["name"])
    except Exception as exc:
        logger.error(f"Failed to get orphan nodes: {exc}")
        return [], str(exc)
    return results, None


def get_mcp_tools_list() -> List[Dict[str, Any]]:
    mcp_tools = []
    try:
        from apps.mcp_server.server import mcp as mcp_instance

        tool_manager = mcp_instance._tool_manager
        for tool_name, tool_obj in tool_manager._tools.items():
            tool_desc = getattr(tool_obj, "description", "") or ""
            if not tool_desc:
                tool_fn = getattr(tool_obj, "fn", None)
                if tool_fn is not None:
                    tool_desc = str(tool_fn.__doc__ or "")
            schema_dict = {}
            if hasattr(tool_obj, "parameters") and tool_obj.parameters:
                schema_dict = tool_obj.parameters
            elif hasattr(tool_obj, "input_model") and tool_obj.input_model:
                try:
                    schema_dict = tool_obj.input_model.model_json_schema()
                except Exception:
                    schema_dict = {}
            mcp_tools.append({"name": tool_name, "description": tool_desc.strip(), "schema": schema_dict})
    except Exception as exc:
        logger.warning(f"Failed to inspect MCP tools: {exc}")
    return mcp_tools


def _short_commit_ref(commit: Any, *, max_len: int = 10) -> str | None:
    """Return a UI-safe commit identifier.

    Snapshot provenance tags (`snapshot:<sha>:<digest>`) are used as an
    integrity token and must stay untouched. Truncate everything else only when
    the raw value is truly long.
    """
    if not commit:
        return None
    text = str(commit)
    if text.startswith("snapshot:"):
        return text
    return text[:max_len]


async def get_mcp_runtime_status() -> Dict[str, Any]:
    """Cheap MCP liveness: tool registration plus datastore connectivity."""
    from time import perf_counter

    started = perf_counter()
    tools = get_mcp_tools_list()
    registration_ms = round((perf_counter() - started) * 1000, 2)
    if not tools:
        return {
            "server_status": "unavailable",
            "status": "unavailable",
            "available": False,
            "tools": [],
            "total_tools": 0,
            "diagnostics": {
                "available": False,
                "error": "MCP tool surface is empty",
                "probe_status": "skipped",
                "probe_target": "registration_and_datastores",
                "stage_timings_ms": {
                    "registration": registration_ms,
                    "datastores": 0.0,
                    "total": registration_ms,
                },
            },
        }

    datastore_started = perf_counter()
    datastore_error = None
    datastore_status: Dict[str, str] = {}
    try:
        from brain.database.session import check_health as check_datastore_health

        services = await asyncio.wait_for(check_datastore_health(), timeout=5.0)
        datastore_status = {
            name: str((services.get(name) or {}).get("status") or "unavailable")
            for name in ("postgres", "redis", "neo4j")
        }
    except asyncio.TimeoutError:
        datastore_error = "Datastore liveness check timed out"
    except Exception as exc:
        datastore_error = str(exc)

    datastores_ms = round((perf_counter() - datastore_started) * 1000, 2)
    unhealthy = [name for name, status in datastore_status.items() if status != "healthy"]
    probe_ok = datastore_error is None and not unhealthy
    status = "online" if probe_ok else "degraded"
    if datastore_error is None and unhealthy:
        datastore_error = f"Unhealthy MCP datastores: {', '.join(unhealthy)}"
    return {
        "server_status": status,
        "status": status,
        "available": probe_ok,
        "tools": tools,
        "total_tools": len(tools),
        "diagnostics": {
            "available": True,
            "error": datastore_error,
            "probe_target": "registration_and_datastores",
            "probe_payload": {"datastores": datastore_status},
            "probe_status": "passed" if probe_ok else "failed",
            "stage_timings_ms": {
                "registration": registration_ms,
                "datastores": datastores_ms,
                "total": round((perf_counter() - started) * 1000, 2),
            },
        },
    }


async def run_mcp_self_check(timeout_seconds: float = 30.0) -> Dict[str, Any]:
    """Run the MCP self-check and record its outcome against the current
    configuration fingerprint. The checklist displays only this recorded,
    labeled result — and invalidates it when the config it ran against
    changes."""
    from brain.onboarding.readiness import config_fingerprint
    from brain.onboarding.setup_state import record_self_check

    fingerprint = config_fingerprint()
    runtime = await get_mcp_readiness_status(timeout_seconds)
    record_self_check(
        str(runtime.get("readiness_status") or "failed"),
        fingerprint,
        {
            "retrieval_probe": (runtime.get("retrieval") or {}).get("probe_status"),
            "stage_timings_ms": runtime.get("stage_timings_ms") or {},
            "error": (runtime.get("retrieval") or {}).get("error"),
        },
    )
    return runtime


async def get_mcp_readiness_status(timeout_seconds: float = 30.0) -> Dict[str, Any]:
    """Full MCP readiness with real retrieval, isolated from dashboard liveness."""
    from time import perf_counter

    started = perf_counter()
    runtime = await get_mcp_runtime_status()
    timings = dict((runtime.get("diagnostics") or {}).get("stage_timings_ms") or {})
    if not runtime.get("available"):
        timings["retrieval"] = 0.0
        timings["total"] = round((perf_counter() - started) * 1000, 2)
        return {
            "readiness_status": "degraded",
            "runtime": runtime,
            "retrieval": {"probe_status": "skipped", "error": "MCP runtime liveness failed"},
            "stage_timings_ms": timings,
        }

    retrieval_started = perf_counter()
    retrieval_error = None
    retrieval_payload: Dict[str, Any] = {}
    from brain.config.paths import resolve_repo_path

    probe_repo = str(resolve_repo_path(settings.TARGET_REPO_PATH))
    try:
        from apps.mcp_server import server as mcp_server

        probe_target = getattr(mcp_server, "search_code", None)
        if not callable(probe_target):
            retrieval_error = "search_code tool entrypoint is not callable"
        else:
            payload = await asyncio.wait_for(
                probe_target("brain readiness probe", repo_path=probe_repo),
                timeout=max(1.0, min(float(timeout_seconds), 120.0)),
            )
            if isinstance(payload, dict):
                retrieval_payload = {
                    key: payload[key]
                    for key in ("status", "error", "retrieval_debug", "repository_scope")
                    if key in payload
                }
                if payload.get("error") or payload.get("status") == "failed":
                    retrieval_error = str(payload.get("error") or "Retrieval probe failed")
    except asyncio.TimeoutError:
        retrieval_error = f"Retrieval readiness timed out after {timeout_seconds:g} seconds"
    except Exception as exc:
        retrieval_error = str(exc)

    timings["retrieval"] = round((perf_counter() - retrieval_started) * 1000, 2)
    timings["total"] = round((perf_counter() - started) * 1000, 2)
    return {
        "readiness_status": "ready" if retrieval_error is None else "degraded",
        "runtime": runtime,
        "retrieval": {
            "probe_status": "passed" if retrieval_error is None else "failed",
            "probe_target": "search_code",
            "repo_path": probe_repo,
            "timeout_seconds": timeout_seconds,
            "error": retrieval_error,
            "payload": retrieval_payload,
        },
        "stage_timings_ms": timings,
    }


def get_latest_eval_metrics(repo_path: Optional[Path] = None) -> Dict[str, Any]:
    report_path = reports_dir(repo_path or get_repo_root()) / "eval-report.md"
    if not report_path.exists():
        return {"avg_precision": None, "avg_recall": None}
    try:
        content = report_path.read_text(encoding="utf-8")
        precision = None
        recall = None
        # The evaluator writes Markdown labels as ``**Average ...@10**:``.
        # Keep accepting the older plain-text form as well.
        metric_value = r"([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)"
        match_precision = re.search(
            rf"^[ \t]*(?:-[ \t]+)?(?:\*\*)?Average[ \t]+Precision@10"
            rf"(?:\*\*)?[ \t]*:[ \t]*{metric_value}[ \t]*$",
            content,
            flags=re.MULTILINE,
        )
        match_recall = re.search(
            rf"^[ \t]*(?:-[ \t]+)?(?:\*\*)?Average[ \t]+Recall@10"
            rf"(?:\*\*)?[ \t]*:[ \t]*{metric_value}[ \t]*$",
            content,
            flags=re.MULTILINE,
        )
        if match_precision:
            precision = float(match_precision.group(1))
        if match_recall:
            recall = float(match_recall.group(1))
        return {"avg_precision": precision, "avg_recall": recall}
    except Exception as exc:
        logger.error(f"Error reading eval report: {exc}")
        return {"avg_precision": None, "avg_recall": None}


def get_cache_status() -> Dict[str, Any]:
    cache = ContextPackBuilder._cache
    return {
        "initialized": cache.initialized,
        "rules_count": len(cache.rules),
        "decisions_count": len(cache.decisions),
        "files_count": len(cache.files),
        "symbols_count": len(cache.symbols),
    }


def get_model_routing_summary() -> Dict[str, Any]:
    """Model orchestration routing table and API key status for dashboard."""
    from brain.llm.router import get_model_router

    return get_model_router().dashboard_summary()


async def get_embedding_retrieval_health(
    repository_id: int | None = None,
    repository_path: str | Path | None = None,
) -> Dict[str, Any]:
    """Summarize vector readiness for the requested indexed repository."""
    from brain.database.repository_utils import get_repository_by_path
    from brain.embeddings.integrity import collect_embedding_inventory

    repo_path = Path(repository_path or settings.TARGET_REPO_PATH).resolve()
    try:
        repo_record = None
        if repository_id is None:
            repo_record = await get_repository_by_path(repo_path)
            repository_id = repo_record.id if repo_record else None
        resolved_repository_path = (
            repo_record.path if repo_record else repo_path.as_posix()
        )
    except Exception as exc:
        logger.error(f"Failed to resolve repository for embedding health: {exc}")
        return {
            "status": "unhealthy",
            "error": str(exc),
            "vector_search_status": "UNKNOWN",
            "repository_path": repo_path.as_posix(),
        }

    try:
        inventory = await collect_embedding_inventory(
            repository_id,
            resolved_repository_path,
            fast=True,
        )
        inv = inventory.to_dict()
        issues: List[str] = []
        if not inv["pgvector_extension"]:
            issues.append("pgvector extension not installed")
        if inv["missing_embeddings"] > 0:
            issues.append(f"{inv['missing_embeddings']} chunks missing embeddings")
        if inv["stale_embeddings"] > 0:
            issues.append(f"{inv['stale_embeddings']} stale embeddings")
        if inv["incompatible_embeddings"] > 0:
            issues.append(f"{inv['incompatible_embeddings']} incompatible embeddings")
        if inv["pgvector_extension"] and inv["pgvector_coverage_pct"] < 100:
            issues.append(
                f"pgvector coverage {inv['pgvector_coverage_pct']}% "
                f"({inv['pgvector_populated']}/{inv['total_eligible_chunks']})"
            )
        if inv["pgvector_extension"] and not inv["pgvector_index_exists"]:
            issues.append("pgvector ANN index missing")

        if not inv["pgvector_extension"]:
            vector_status = "PGVECTOR_UNAVAILABLE"
        elif issues:
            vector_status = "DEGRADED"
        else:
            vector_status = "OK"

        if vector_status == "OK":
            overall = "healthy"
        elif vector_status == "DEGRADED":
            overall = "degraded"
        else:
            overall = "unhealthy"

        return {
            "status": overall,
            "vector_search_status": vector_status,
            "repository_path": resolved_repository_path,
            "total_eligible_chunks": inv["total_eligible_chunks"],
            "chunks_with_current_embeddings": inv["chunks_with_current_embeddings"],
            "missing_embeddings": inv["missing_embeddings"],
            "stale_embeddings": inv["stale_embeddings"],
            "incompatible_embeddings": inv["incompatible_embeddings"],
            "pgvector_populated": inv["pgvector_populated"],
            "pgvector_coverage_pct": inv["pgvector_coverage_pct"],
            "pgvector_extension": inv["pgvector_extension"],
            "pgvector_index_exists": inv["pgvector_index_exists"],
            "issues": issues,
        }
    except Exception as exc:
        logger.error(f"Embedding health check failed: {exc}")
        return {
            "status": "unhealthy",
            "error": str(exc),
            "vector_search_status": "UNKNOWN",
            "repository_path": repo_path.as_posix(),
        }


async def get_recent_brain_jobs(
    limit: int = 10,
    *,
    repository_id: int | None = None,
    repository_path: str | None = None,
) -> Dict[str, Any]:
    """List recent worker jobs stored in Redis (Phase 9 queue)."""
    from brain.database.session import redis_client
    from brain.workers.queue import JobQueue

    jobs: List[Dict[str, Any]] = []
    try:
        queue = JobQueue(redis_client, prefix=settings.WORKER_REDIS_PREFIX)
        # Repository filtering happens after loading job payloads, so fetch the
        # largest indexed window before applying the requested result limit.
        # Otherwise unrelated repositories can hide matching jobs among the
        # first 100 global entries.
        scoped_request = repository_id is not None or repository_path
        lookup_limit = 1000 if scoped_request else max(limit, 100)
        job_ids = await queue.recent_job_ids(limit=lookup_limit)
        # Upgrade fallback: jobs created before the ZSET index was introduced.
        if not job_ids:
            cursor = 0
            keys: List[str] = []
            while True:
                cursor, batch = await redis_client.scan(cursor, match=f"{queue.prefix}:job:*", count=100)
                keys.extend(cast(List[str], batch))
                if cursor == 0 or len(keys) >= 5000:
                    break
            job_ids = [key.rsplit(":", 1)[-1] for key in keys]
        wanted = str(Path(repository_path).resolve()) if repository_path else None

        def belongs_to_scope(job: Dict[str, Any]) -> bool:
            raw_params = job.get("params")
            params = raw_params if isinstance(raw_params, dict) else {}
            job_repo_id = params.get("repository_id")
            job_repo_path = params.get("repo_path") or params.get("repository_path")
            if repository_id is not None and str(job_repo_id) == str(repository_id):
                return True
            if wanted and isinstance(job_repo_path, str):
                try:
                    return str(Path(job_repo_path).resolve()) == wanted
                except (OSError, RuntimeError):
                    return False
            return False

        offset = 0
        while True:
            for job_id in job_ids:
                job = await queue.get_job(job_id)
                if not job:
                    continue
                if not job.get("id"):
                    job["id"] = job_id
                jobs.append(job)
            if not scoped_request or len(job_ids) < lookup_limit:
                break
            if sum(belongs_to_scope(job) for job in jobs) >= limit:
                break
            offset += lookup_limit
            job_ids = await queue.recent_job_ids(limit=lookup_limit, offset=offset)
        if scoped_request:
            scoped = []
            for job in jobs:
                if belongs_to_scope(job):
                    scoped.append(job)
            jobs = scoped[:limit]
        jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return {"jobs": jobs[:limit], "error": None}
    except Exception as exc:
        logger.warning(f"Failed to list brain jobs: {exc}")
        return {"jobs": [], "error": str(exc)}


async def get_self_diagnosis_status() -> Dict[str, Any]:
    """Safe operator-facing state for the autonomous diagnostic loop."""
    from brain.database.session import redis_client
    from brain.version import build_info
    from brain.workers.queue import JobQueue

    def parse(raw: str | None) -> Dict[str, Any] | None:
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    last_result = None
    last_delivery = None
    queue_stats: Dict[str, Any] = {
        "queued": None,
        "processing": None,
        "retrying": None,
    }
    redis_error = None
    try:
        raw_result, raw_delivery = await redis_client.mget(
            "brain:self-diagnosis:last-result",
            "brain:self-diagnosis:last-delivery",
        )
        last_result = parse(raw_result.decode() if isinstance(raw_result, bytes) else raw_result)
        last_delivery = parse(raw_delivery.decode() if isinstance(raw_delivery, bytes) else raw_delivery)
        queue_stats = await JobQueue(redis_client, prefix=settings.WORKER_REDIS_PREFIX).get_stats()
    except Exception as exc:
        redis_error = f"{type(exc).__name__}: {exc}"[:260]

    return {
        "enabled": settings.SELF_DIAGNOSIS_ENABLED,
        "llm_enabled": settings.SELF_DIAGNOSIS_USE_LLM,
        "telegram_enabled": settings.TELEGRAM_ALERTS_ENABLED,
        "telegram_credentials_present": bool(settings.TELEGRAM_ALERT_BOT_TOKEN and settings.TELEGRAM_ALERT_CHAT_ID),
        "build": build_info(),
        "queue": queue_stats,
        "last_result": last_result,
        "last_delivery": last_delivery,
        "redis_error": redis_error,
    }


def get_latest_critic_status(path: str) -> str:
    if not path:
        return "UNKNOWN"
    file_path = Path(path)
    if not file_path.exists():
        return "UNKNOWN"
    try:
        content = file_path.read_text(encoding="utf-8")
        match = re.search(r"## Critic Evaluation Status:\s*\*\*([^*]+)\*\*", content)
        if match:
            return match.group(1).strip()
    except Exception as exc:
        logger.error(f"Error reading context pack for critic status: {exc}")
    return "UNKNOWN"
