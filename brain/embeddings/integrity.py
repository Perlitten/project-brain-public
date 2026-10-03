"""Embedding inventory, compatibility checks, and pgvector coverage reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import cast, func, null, select, text
from sqlalchemy.dialects.postgresql import JSONB

from brain.database.models import Embedding, File, FileChunk
from brain.database.session import async_session_factory
from brain.embeddings.config import EmbeddingConfig, content_hash, get_embedding_config
from brain.embeddings.pgvector_sql import pgvector_index_names


@dataclass
class EmbeddingInventory:
    repository_id: Optional[int] = None
    repository_path: Optional[str] = None
    configured: EmbeddingConfig = field(default_factory=get_embedding_config)
    total_eligible_chunks: int = 0
    chunks_with_current_embeddings: int = 0
    stale_embeddings: int = 0
    incompatible_embeddings: int = 0
    missing_embeddings: int = 0
    pgvector_populated: int = 0
    pgvector_coverage_pct: float = 0.0
    pgvector_extension: bool = False
    pgvector_index_exists: bool = False
    # True when collected in fast mode (skips content-hash staleness checks);
    # consumers should treat the staleness counts as approximate, not exact.
    fast_mode: bool = False

    def to_dict(self) -> Dict[str, Any]:
        eligible = self.total_eligible_chunks or 0
        current_pct = (self.chunks_with_current_embeddings / eligible * 100) if eligible else 0.0
        return {
            "repository_id": self.repository_id,
            "repository_path": self.repository_path,
            "configured": self.configured.as_metadata(),
            "total_eligible_chunks": self.total_eligible_chunks,
            "chunks_with_current_embeddings": self.chunks_with_current_embeddings,
            "stale_embeddings": self.stale_embeddings,
            "incompatible_embeddings": self.incompatible_embeddings,
            "missing_embeddings": self.missing_embeddings,
            "pgvector_populated": self.pgvector_populated,
            "pgvector_coverage_pct": round(self.pgvector_coverage_pct, 2),
            "current_embedding_pct": round(current_pct, 2),
            "pgvector_extension": self.pgvector_extension,
            "pgvector_index_exists": self.pgvector_index_exists,
            "fast_mode": self.fast_mode,
        }


def _is_current_embedding(
    emb: Optional[Embedding],
    chunk_content: str,
    config: EmbeddingConfig,
) -> tuple[bool, str]:
    """Return (is_current, reason) for an embedding row."""
    if emb is None:
        return False, "missing"
    if emb.dimension is not None and emb.dimension != config.dimension:
        return False, "incompatible_dimension"
    if emb.provider and emb.provider != config.provider:
        return False, "stale_provider"
    if emb.model and emb.model != config.model:
        return False, "stale_model"
    if emb.content_hash and emb.content_hash != content_hash(chunk_content):
        return False, "stale_content"
    if emb.vector_data and len(emb.vector_data) != config.dimension:
        return False, "incompatible_vector_length"
    return True, "current"


def _embedding_reason(
    emb_id: Optional[int],
    dim: Optional[int],
    provider: Optional[str],
    model: Optional[str],
    chash: Optional[str],
    vec_len: Optional[int],
    chunk_content: str,
    config: EmbeddingConfig,
) -> str:
    """Scalar-column equivalent of ``_is_current_embedding`` (no vector loaded)."""
    if emb_id is None:
        return "missing"
    if dim is not None and dim != config.dimension:
        return "incompatible_dimension"
    if provider and provider != config.provider:
        return "stale_provider"
    if model and model != config.model:
        return "stale_model"
    if chash and chash != content_hash(chunk_content):
        return "stale_content"
    if vec_len is not None and vec_len != config.dimension:
        return "incompatible_vector_length"
    return "current"


async def _pgvector_health(session) -> tuple[bool, bool]:
    extension = False
    index_exists = False
    try:
        row = (
            await session.execute(
                text("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'vector')")
            )
        ).scalar()
        extension = bool(row)
    except Exception:
        extension = False
    try:
        index_list = ", ".join(f"'{name}'" for name in pgvector_index_names())
        row = (
            await session.execute(
                text(
                    f"SELECT EXISTS("
                    f"SELECT 1 FROM pg_indexes "
                    f"WHERE indexname IN ({index_list})"
                    f")"
                )
            )
        ).scalar()
        index_exists = bool(row)
    except Exception:
        index_exists = False
    return extension, index_exists


def fast_embedding_reason(
    emb_id: Optional[int],
    dim: Optional[int],
    provider: Optional[str],
    model: Optional[str],
    vec_len: Optional[int],
    config: EmbeddingConfig,
) -> str:
    """Metadata-only freshness verdict (no content hashing) used by fast inventories."""
    if emb_id is None:
        return "missing"
    if dim is not None and dim != config.dimension:
        return "incompatible_dimension"
    if provider and provider != config.provider:
        return "stale_provider"
    if model and model != config.model:
        return "stale_model"
    if vec_len is not None and vec_len != config.dimension:
        return "incompatible_vector_length"
    return "current"


async def collect_embedding_inventory(
    repository_id: Optional[int] = None,
    repository_path: Optional[str] = None,
    fast: bool = False,
) -> EmbeddingInventory:
    from brain.search.filters import should_exclude_from_retrieval

    config = get_embedding_config()
    inventory = EmbeddingInventory(
        repository_id=repository_id,
        repository_path=repository_path,
        configured=config,
        fast_mode=fast,
    )

    async with async_session_factory() as session:
        inventory.pgvector_extension, inventory.pgvector_index_exists = await _pgvector_health(session)

        # Select scalar metadata only — NEVER load the 4000-dim vectors (Embedding.embedding
        # / Embedding.vector_data). Vector length/presence are computed in SQL. Loading the full
        # vectors here made this query take >2min on a populated repo (dashboard hang).
        # fast=True (dashboard health) also skips chunk content + stale-content hashing — a pure
        # coverage count in well under a second. fast=False (CLI verify) keeps full drift checks.
        # fast mode avoids loading chunk content AND parsing each 4000-element JSON vector
        # (jsonb_array_length); it relies on the stored Embedding.dimension instead.
        content_col = func.length(FileChunk.content).label("clen") if fast else FileChunk.content
        vec_len_col = (
            null().label("vec_len") if fast
            else func.jsonb_array_length(cast(Embedding.vector_data, JSONB)).label("vec_len")
        )
        stmt = (
            select(
                File.path,
                content_col,
                Embedding.id,
                Embedding.dimension,
                Embedding.provider,
                Embedding.model,
                Embedding.content_hash,
                vec_len_col,
                Embedding.embedding.isnot(None).label("has_vec"),
            )
            .join(File, FileChunk.file_id == File.id)
            .outerjoin(Embedding, FileChunk.embedding_id == Embedding.id)
        )
        if repository_id is not None:
            stmt = stmt.where(File.repository_id == repository_id)

        rows = (await session.execute(stmt)).all()
        # Many chunks share a file; decide each path once (45k chunks -> ~7k paths).
        excluded: dict[str, bool] = {}
        for path, content, emb_id, dim, provider, model, chash, vec_len, has_vec in rows:
            skip = excluded.get(path)
            if skip is None:
                skip = excluded[path] = should_exclude_from_retrieval(path)
            if skip:
                continue
            inventory.total_eligible_chunks += 1
            if fast:
                reason = fast_embedding_reason(emb_id, dim, provider, model, vec_len, config)
            else:
                reason = _embedding_reason(emb_id, dim, provider, model, chash, vec_len, content, config)
            if reason == "missing":
                inventory.missing_embeddings += 1
            elif reason.startswith("incompatible"):
                inventory.incompatible_embeddings += 1
            elif reason.startswith("stale"):
                inventory.stale_embeddings += 1
            elif reason == "current":
                inventory.chunks_with_current_embeddings += 1

            if has_vec:
                inventory.pgvector_populated += 1

        if inventory.total_eligible_chunks:
            inventory.pgvector_coverage_pct = (
                inventory.pgvector_populated / inventory.total_eligible_chunks * 100
            )

    return inventory


async def verify_embeddings(
    repository_id: Optional[int] = None,
    repository_path: Optional[str] = None,
) -> Dict[str, Any]:
    inventory = await collect_embedding_inventory(repository_id, repository_path)
    config = inventory.configured
    issues: List[str] = []

    if inventory.missing_embeddings:
        issues.append(f"{inventory.missing_embeddings} chunks missing embeddings")
    if inventory.stale_embeddings:
        issues.append(f"{inventory.stale_embeddings} stale embeddings (provider/model/hash drift)")
    if inventory.incompatible_embeddings:
        issues.append(f"{inventory.incompatible_embeddings} incompatible dimension vectors")
    if not inventory.pgvector_extension:
        issues.append("pgvector extension not installed")
    elif inventory.pgvector_coverage_pct < 100.0:
        issues.append(
            f"pgvector coverage {inventory.pgvector_coverage_pct:.1f}% "
            f"({inventory.pgvector_populated}/{inventory.total_eligible_chunks})"
        )
    if not inventory.pgvector_index_exists and inventory.pgvector_extension:
        if config.dimension <= 2000:
            issues.append("pgvector IVFFlat/HNSW index missing")
        elif config.dimension > 4000:
            issues.append(
                f"pgvector index dimension capped at 4000 (configured {config.dimension}); "
                "truncated halfvec index required"
            )

    eligible = inventory.total_eligible_chunks
    # Index required for ANN-backed vector search at any configured dimension.
    all_current = (
        eligible > 0
        and inventory.chunks_with_current_embeddings == eligible
        and inventory.pgvector_populated == eligible
        and inventory.pgvector_extension
        and inventory.pgvector_index_exists
    )

    return {
        **inventory.to_dict(),
        "pass": all_current and not issues,
        "issues": issues,
        "query_vector_dimension": config.dimension,
        "schema_dimension": config.dimension,
        "dimension_match": True,
    }
