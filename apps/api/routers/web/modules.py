"""Module aggregation and the module-graph endpoint."""
from __future__ import annotations

from collections import defaultdict
from typing import Any
from fastapi import HTTPException
from sqlalchemy import case, func, select
from brain.database.models import Embedding, File, FileChunk
from brain.database.session import async_session_factory
from ._common import REPOSITORY_READ, _repository_dict, _resolve_repository, module_of, router


MODULE_GRAPH_EDGE_LIMIT = 50_000


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


@router.get("/module-graph", dependencies=[REPOSITORY_READ])
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


def _add_module_row(row: tuple[Any, ...], modules: dict[str, dict[str, Any]], config: Any) -> None:
    from brain.embeddings.integrity import fast_embedding_reason
    from brain.search.filters import should_exclude_from_retrieval

    path, emb_id, dim, provider, model = row[:5]
    count = int(row[5]) if len(row) > 5 and row[5] is not None else 1
    name = module_of(path)
    bucket = modules.setdefault(
        name,
        {"name": name, "chunks": 0, "current": 0, "outdated": 0, "missing": 0, "excluded": 0},
    )
    bucket["chunks"] += count
    if should_exclude_from_retrieval(path):
        bucket["excluded"] += count
        return
    reason = fast_embedding_reason(emb_id, dim, provider, model, None, config)
    bucket["current" if reason == "current" else "missing" if reason == "missing" else "outdated"] += count


def aggregate_modules(rows: list[tuple[Any, ...]], config: Any) -> list[dict[str, Any]]:
    """rows: (path, embedding_id, dimension, provider, model[, chunk_count]) per chunk or file group."""
    modules: dict[str, dict[str, Any]] = {}
    for row in rows:
        _add_module_row(row, modules, config)
    return sorted(modules.values(), key=lambda item: (-item["chunks"], item["name"]))


@router.get("/modules", dependencies=[REPOSITORY_READ])
async def web_modules(repository_id: int | None = None) -> dict[str, Any]:
    from brain.embeddings.config import get_embedding_config

    repository = await _resolve_repository(repository_id)
    if repository is None:
        return {"repository": None, "modules": []}
    config = get_embedding_config()
    modules: dict[str, dict[str, Any]] = {}
    has_emb = case((Embedding.id.is_not(None), 1), else_=None)
    async with async_session_factory() as session:
        rows = await session.stream(
            select(
                File.path,
                has_emb,
                Embedding.dimension,
                Embedding.provider,
                Embedding.model,
                func.count(FileChunk.id),
            )
            .select_from(FileChunk)
            .join(File, FileChunk.file_id == File.id)
            .outerjoin(Embedding, FileChunk.embedding_id == Embedding.id)
            .where(File.repository_id == repository.id)
            .group_by(File.path, has_emb, Embedding.dimension, Embedding.provider, Embedding.model)
            .execution_options(yield_per=1000)
        )
        async for row in rows:
            _add_module_row(tuple(row), modules, config)
    return {
        "repository": _repository_dict(repository),
        "modules": sorted(modules.values(), key=lambda item: (-item["chunks"], item["name"])),
    }
