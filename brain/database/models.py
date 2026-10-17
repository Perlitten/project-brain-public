from __future__ import annotations

from datetime import date, datetime
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Float,
    Index,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from brain.database.portable_types import utc_datetime
from brain.embeddings.constants import EMBEDDING_DIMENSION
from brain.embeddings.pgvector_sql import pgvector_column_type


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy declarative models."""

    pass


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    type: Mapped[str | None] = mapped_column(String(50), nullable=True)  # e.g., "git", "local"
    language_stack: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)  # List of primary languages
    last_indexed_commit: Mapped[str | None] = mapped_column(String(255), nullable=True)
    indexing_status: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # e.g., "pending", "indexing", "completed", "failed"
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        utc_datetime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    files: Mapped[list["File"]] = relationship("File", back_populates="repository", cascade="all, delete-orphan")
    indexing_runs: Mapped[list["IndexingRun"]] = relationship(
        "IndexingRun", back_populates="repository", cascade="all, delete-orphan"
    )



class RepositoryOriginState(Base):
    """Where origin stands for an attached repository, written by the origin poll and reindex.

    One row per repository. ``remote_url`` is always stored redacted (no
    userinfo); the usable URL comes from configuration at run time.
    """

    __tablename__ = "repository_origin_state"

    repository_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )
    remote_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    tracked_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # "checkout": the mounted checkout is the source; "mirror": a Brain-owned bare mirror is.
    source_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    remote_head: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remote_head_seen_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    polled_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    # "ok", "error" or "unconfigured" (no origin to poll).
    poll_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    poll_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    mirror_head: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Commits in commits_behind_head that commits_behind_base lacks.
    commits_behind: Mapped[int | None] = mapped_column(Integer, nullable=True)
    commits_behind_base: Mapped[str | None] = mapped_column(String(64), nullable=True)
    commits_behind_head: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_reindex_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_reindex_head: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_reindex_requested_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        utc_datetime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )

class File(Base):
    __tablename__ = "files"
    __table_args__ = (
        Index("ix_files_repository_path", "repository_id", "path"),
        UniqueConstraint("repository_id", "path", name="uq_files_repository_path"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    repository_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    language: Mapped[str | None] = mapped_column(String(100), nullable=True)
    file_type: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )  # e.g., "source_code", "test", "documentation"
    hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        utc_datetime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    last_indexed_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)

    # Relationships
    repository: Mapped["Repository"] = relationship("Repository", back_populates="files")
    chunks: Mapped[list["FileChunk"]] = relationship("FileChunk", back_populates="file", cascade="all, delete-orphan")
    symbols: Mapped[list["Symbol"]] = relationship("Symbol", back_populates="file", cascade="all, delete-orphan")


class FileChunk(Base):
    __tablename__ = "file_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    file_id: Mapped[int] = mapped_column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("embeddings.id", ondelete="SET NULL"), nullable=True
    )

    # Relationships
    file: Mapped["File"] = relationship("File", back_populates="chunks")
    embedding: Mapped["Embedding | None"] = relationship("Embedding", foreign_keys="[FileChunk.embedding_id]")


class Symbol(Base):
    __tablename__ = "symbols"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    file_id: Mapped[int] = mapped_column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str | None] = mapped_column(String(100), nullable=True)  # e.g., "class", "function", "method"
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    file: Mapped["File"] = relationship("File", back_populates="symbols")


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # Duplicate historical policy is retained under its original ID/content.
    # This pointer identifies the canonical record without injecting both.
    superseded_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    repo_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)  # e.g., "active", "deprecated", "superseded"
    date: Mapped[date | None] = mapped_column(nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    consequences: Mapped[str | None] = mapped_column(Text, nullable=True)
    affected_features: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)  # List of strings
    affected_modules: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)  # List of strings
    affected_files: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)  # List of strings
    # Principal that first recorded it; NULL for rows that predate authorship or came from the CLI.
    author_principal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Principal behind the latest content change (the author until someone else edits it).
    updated_by_principal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # History row that held the previous content of this canonical decision (None for v1).
    supersedes_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Monotonic content version; the compare-and-swap token for ``expected_version``.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        utc_datetime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, index=True)  # e.g., "brand-logo-source-of-truth"
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    repo_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[str | None] = mapped_column(String(100), nullable=True)  # e.g., "architecture", "design", "security"
    severity: Mapped[str | None] = mapped_column(String(50), nullable=True)  # e.g., "low", "medium", "high", "critical"
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)  # e.g., "active", "disabled"
    applies_to: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )  # e.g., dict mapping features/modules
    author_principal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_by_principal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Supersede chain, like decisions: a content change copies the previous content into a
    # history row (status "superseded", superseded_by_id -> canonical) and bumps ``version``.
    supersedes_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    superseded_by_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        utc_datetime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ContextPack(Base):
    __tablename__ = "context_packs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    task_description: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    # Repository the pack was built for; null on rows that predate pack
    # provenance (those cannot be attributed to any repository).
    repository_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("repositories.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Commit the pack's evidence came from (latest completed index run at
    # build time); null when the repo had no completed index then.
    repo_commit: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)

    # What the pack actually delivered, recorded at build time. These were
    # previously re-derived per request by parsing the markdown artifact, which
    # made them unavailable in aggregate and impossible once a file was gone.
    # Null means the pack predates this accounting — never read a null as zero,
    # or every old pack looks like it delivered nothing.
    budget: Mapped[str | None] = mapped_column(String(50), nullable=True)
    token_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Memory that reached the agent inside this pack: the whole point of
    # recording decisions and rules is that they turn up in the next task.
    rule_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decision_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    learning_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Principal that asked for the pack (e.g. "mcp:claude-code"), as recorded
    # by the transport; null when the caller was not attributed.
    consumer: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)


class IndexingRun(Base):
    __tablename__ = "indexing_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    repository_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    commit_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)  # "running", "completed", "degraded", "failed"
    started_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    progress: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    verification: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Per-outcome file counters + bounded error samples written when the run
    # finishes; null for runs started before completeness accounting existed.
    file_counts: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Relationships
    repository: Mapped["Repository"] = relationship("Repository", back_populates="indexing_runs")


class Embedding(Base):
    __tablename__ = "embeddings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g., "file_chunk", "symbol"
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    vector_data: Mapped[list[float]] = mapped_column(JSON, nullable=False)  # List of floats or text
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(
        pgvector_column_type(EMBEDDING_DIMENSION), nullable=True
    )


class LateInteractionEmbedding(Base):
    """Compressed per-token vectors for an optional late-interaction channel.

    This table is deliberately separate from ``embeddings``.  A ColBERT
    document is a matrix, not a single point, and pretending otherwise would
    either corrupt the current pgvector contract or force a destructive schema
    replacement.  Rows cascade with their chunk/repository and can therefore be
    backfilled or rolled back without touching NVIDIA embeddings.
    """

    __tablename__ = "late_interaction_embeddings"
    __table_args__ = (
        UniqueConstraint("chunk_id", "model", "model_revision", name="uq_late_embedding_chunk_model_revision"),
        CheckConstraint("dimension > 0", name="ck_late_embedding_dimension"),
        CheckConstraint("token_count > 0", name="ck_late_embedding_token_count"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    repository_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("file_chunks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    model_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    vector_dtype: Mapped[str] = mapped_column(
        String(16), nullable=False, default="float16", server_default="float16"
    )
    vector_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    was_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        utc_datetime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class LateInteractionShadowEvent(Base):
    """Durable, query-redacted evidence for counterfactual late-interaction ranking.

    ``query_hash`` is the only query identity accepted by the public recorder.
    Final rankings and numeric diagnostics are bounded by
    :mod:`brain.late_interaction.shadow` before they reach these JSON columns.
    """

    __tablename__ = "late_interaction_shadow_events"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_late_shadow_idempotency_key",
        ),
        CheckConstraint(
            "coverage >= 0 AND coverage <= 1",
            name="ck_late_shadow_coverage",
        ),
        Index(
            "ix_late_shadow_created",
            "created_at",
        ),
        Index(
            "ix_late_shadow_repository_created",
            "repository_id",
            "created_at",
        ),
        Index(
            "ix_late_shadow_status_created",
            "status",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    repository_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("repositories.id", ondelete="CASCADE"),
        nullable=False,
    )
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    query_class: Mapped[str] = mapped_column(String(64), nullable=False)
    baseline_final_top_k: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    counterfactual_final_top_k: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    score_metrics: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    coverage: Mapped[float] = mapped_column(Float, nullable=False)
    timing_ms: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False)
    model_revision: Mapped[str] = mapped_column(String(255), nullable=False)
    index_revision: Mapped[str] = mapped_column(String(255), nullable=False)
    skip_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        utc_datetime(),
        server_default=func.now(),
        nullable=False,
    )


class FileCard(Base):
    """v6 P3 — one deterministic structured 'card' per file, embedded separately.

    The card embedding lives in the shared ``embeddings`` table with
    ``entity_type='file_card'`` and ``entity_id=file_card.id`` (mirrors FileChunk),
    so card vectors are verifiable independently of chunk vectors.
    """

    __tablename__ = "file_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    repository_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_id: Mapped[int] = mapped_column(Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False, index=True)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    surface: Mapped[str | None] = mapped_column(String(50), nullable=True)
    file_role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    card_text: Mapped[str] = mapped_column(Text, nullable=False)
    card_text_format: Mapped[str] = mapped_column(String(32), nullable=False, default="code_shaped")
    card_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("embeddings.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        utc_datetime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    embedding: Mapped["Embedding | None"] = relationship("Embedding", foreign_keys="[FileCard.embedding_id]")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g., "pending", "in_progress", "completed"
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)


class DiffReview(Base):
    __tablename__ = "diff_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    commit_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    report_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g., "approved", "needs_review", "blocked"
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)


class BrainInsight(Base):
    """Evidence-bound proactive signal generated by deterministic checks and LLM synthesis."""

    __tablename__ = "insights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    insight_type: Mapped[str] = mapped_column(String(100), nullable=False)
    severity: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    recommended_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[str] = mapped_column(String(50), nullable=False, default="medium")
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="new")
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="deterministic")
    source_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    engine_version: Mapped[str] = mapped_column(String(64), nullable=False, default="p0.1")
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        utc_datetime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)


class Learning(Base):
    """L3 semantic memory: a durable fact promoted from an L2 episode.

    Written by the consolidation pipeline (L1 events → L2 episode → gates G1–G4,
    see brain/memory/consolidation.py), by human approval of a pending episode,
    or directly via POST /learnings.
    Supersede, never delete — the chain is the audit trail.
    """

    __tablename__ = "memory_learnings"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, index=True)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    # active | superseded | rejected
    superseded_by: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), ForeignKey("memory_learnings.id", ondelete="SET NULL"), nullable=True
    )
    valid_until: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    repo_scope: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    promoted_from: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(
        pgvector_column_type(EMBEDDING_DIMENSION), nullable=True
    )
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Provenance: who contributed the evidence (sole distinct author, else NULL) and who last
    # changed the row (approval, supersede, reject). NULL for machine-only or legacy rows.
    author_principal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_by_principal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        utc_datetime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MemoryProposal(Base):
    """A proposed change to normative memory, waiting for an independent reviewer (team state).

    Proposals are drafts: no retrieval path reads this table, so nothing in it
    reaches context packs, search or the decision/rule listings until a
    reviewer approves it. Approval writes the canonical row through the
    decision/rule store (supersede history, compare-and-swap against
    ``base_version``) and records the result here; the proposal row itself is
    never deleted, so it stays the provenance record of who proposed what and
    who approved or rejected it, and why (brain/memory/proposals.py)."""

    __tablename__ = "memory_proposals"
    __table_args__ = (
        CheckConstraint("kind IN ('decision','rule','learning')", name="ck_memory_proposals_kind"),
        CheckConstraint("state IN ('pending','approved','rejected','withdrawn')", name="ck_memory_proposals_state"),
        Index("ix_memory_proposals_state_scope", "state", "repo_path"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    # Compare-and-swap token for review decisions; bumped on every state change.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # Normalized scope; NULL is system-global knowledge (team administrators review it).
    repo_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # The path as the author spelled it: authorization resolves the repository from this spelling,
    # because normalization case-folds Windows paths that the repository registry stores verbatim.
    requested_repo_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Canonical decision id, rule id or learning id this proposal changes; NULL proposes a new item.
    target_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Version of the target when the proposal was made (0 = new); approval writes with it as expected_version.
    base_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    author_principal_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    reviewed_by_principal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    # Canonical row the approval produced and its new version.
    result_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    result_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        utc_datetime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkerJobLease(Base):
    """Durable Postgres lease for background jobs.

    The Redis lease is the fast path; this row is what actually fences
    commits — writers take a FOR UPDATE lock on it inside their transaction,
    so a competing claim serializes against the in-flight commit instead of
    slipping between a pre-commit Redis check and the commit itself.
    """

    __tablename__ = "worker_job_leases"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fencing_token: Mapped[str] = mapped_column(String(64), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(utc_datetime(), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        utc_datetime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Principal(Base):
    """An attributable identity (service account or human) that can act on

    Project Brain. One shared API key today becomes many scoped, revocable
    credentials bound to named principals; ``org_id`` carries the future
    tenant boundary (enforcement is a later stage)."""

    __tablename__ = "principals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="service")  # service | human
    org_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)


class ApiCredential(Base):
    """A scoped API credential bound to a principal.

    Only the SHA-256 hash of the presented key is stored — the plaintext is
    shown once at mint time. ``revoked_at``/``expires_at`` enforce fail-closed
    denial at resolution time."""

    __tablename__ = "api_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    principal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("principals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)

    principal: Mapped["Principal"] = relationship("Principal")


class OAuthClient(Base):
    """An MCP client registered through OAuth dynamic client registration.

    ``client_info`` is the RFC 7591 registration as the client sent it (with
    the issued id and secret), so the authorization server can re-validate
    redirect URIs and client authentication after a restart."""

    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_info: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)


class OAuthRefreshToken(Base):
    """A refresh token for an OAuth-issued access credential.

    Access tokens are ordinary ``api_credentials`` rows; this row is what lets
    the client swap one for the next. Rotation revokes both the token and the
    credential it last issued. Only the SHA-256 of the token is stored."""

    __tablename__ = "oauth_refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    client_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("oauth_clients.client_id", ondelete="CASCADE"), nullable=False, index=True
    )
    principal_name: Mapped[str] = mapped_column(String(128), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    access_credential_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("api_credentials.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(utc_datetime(), nullable=True)


class AuditEvent(Base):
    """Append-only record of who did what through the API (B7c).

    Written by the audit middleware for every mutating authenticated call —
    including denied attempts — so customers can answer "which credential
    changed this" without trusting application logs alone."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        utc_datetime(), server_default=func.now(), nullable=False, index=True
    )
    principal_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    principal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    credential_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # allowed | denied | error
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


class BrainRequestTelemetry(Base):
    """Durable, payload-free telemetry for search/context requests."""
    __tablename__ = "brain_request_telemetry"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), index=True)
    operation: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    surface: Mapped[str] = mapped_column(String(16), nullable=False, default="http", server_default="http")
    repository_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    repository_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    principal_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)



class McpToolCall(Base):
    """One MCP tool call: which tool, over which transport, how it ended, how long it took.
    Payload-free; feeds the per-tool health on the Agent tools screen."""
    __tablename__ = "mcp_tool_calls"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), index=True)
    tool: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    surface: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)


class RuleUsageEvent(Base):
    """One occasion on which a recorded rule did something, payload-free.

    A rule is worth keeping only if it reaches the work. Two kinds are
    recorded, and they mean different things:

    - ``delivered``: the rule was put into a context pack an agent asked for,
      so the agent saw it. Proves reach.
    - ``flagged``: a diff review cited the rule against an actual change.
      Proves the rule caught something.

    Rows rather than counters on ``rules``, so "in the last 30 days" is a real
    window instead of a total that can only grow.
    """

    __tablename__ = "rule_usage_events"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), index=True)
    rule_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # delivered | flagged
    repository_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # Pack the rule was delivered in, when the event came from a pack build.
    context_pack_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    principal_name: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)


class MemoryEpisode(Base):
    """L2 episodic memory: one distilled cluster of L1 events and its gate outcome.

    Memory tiers: L1 working (``agent_task_events``), L2 episodic (this table),
    L3 semantic (``memory_learnings``), L4 procedural (``memory_skills``).
    Status: pending (awaiting human approval) | promoted | rejected | duplicate.
    """

    __tablename__ = "memory_episodes"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, index=True)
    # Cluster of L1 event IDs that form this episode
    source_event_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    # LLM-distilled summary of the episode
    distilled_summary: Mapped[str] = mapped_column(Text, nullable=False)
    # Topic/category for retrieval
    topic: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    # L2 lifecycle: pending → promoted | rejected | merged
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    # If promoted, link to the L3 learning
    promoted_to_learning_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), ForeignKey("memory_learnings.id", ondelete="SET NULL"), nullable=True
    )
    # G1 outcome: the active L3 learning this episode duplicates
    duplicate_of_learning_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), ForeignKey("memory_learnings.id", ondelete="SET NULL"), nullable=True
    )
    gate_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # G3 outcome: active L3 learnings this episode corrects. Applied (status
    # superseded) when the episode is promoted or approved.
    supersedes_learning_ids: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    # If merged, link to the surviving episode
    merged_into_episode_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), ForeignKey("memory_episodes.id", ondelete="SET NULL"), nullable=True
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    embedding: Mapped[list[float] | None] = mapped_column(
        pgvector_column_type(EMBEDDING_DIMENSION), nullable=True
    )
    repo_scope: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Provenance: the sole principal behind the source L1 events (NULL when mixed or unknown)
    # and the principal that approved/rejected the episode.
    author_principal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_by_principal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        utc_datetime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MemoryEpisodeEvent(Base):
    """Consumption ledger: each L1 event belongs to at most one L2 episode.

    The primary key makes consolidation idempotent: a re-run (or a concurrent
    run) cannot turn the same L1 event into a second episode.
    """

    __tablename__ = "memory_episode_events"

    event_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    episode_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), ForeignKey("memory_episodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)


class MemorySkill(Base):
    """L4 procedural memory: learned skills and workflows.

    Industry-standard 4-tier architecture:
    - L4 answers "How do I perform this task?"
    - Skills are distilled from successful L2 episodes / L3 learnings
    - Each skill has triggers (when to use), workflow steps, and success tracking
    """

    __tablename__ = "memory_skills"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # When to use this skill (keywords, patterns)
    triggers: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # Workflow steps as JSON: [{"step": 1, "action": "...", "tool": "..."}]
    workflow: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    # Source: which L2 episodes or L3 learnings informed this skill
    source_episode_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    source_learning_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    # Success tracking
    times_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    times_successful: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # active | deprecated
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    embedding: Mapped[list[float] | None] = mapped_column(
        pgvector_column_type(EMBEDDING_DIMENSION), nullable=True
    )
    repo_scope: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Explicit opt-in for prompt injection: a NULL scope is *not* global —
    # only is_global skills may be injected into contexts for any repo.
    is_global: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Provenance: who created the skill (API caller or the distiller's evidence author) and
    # who last approved/rejected/changed it.
    author_principal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_by_principal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        utc_datetime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SkillOutcome(Base):
    """Idempotent, evidence-linked result of applying one L4 skill to a task."""
    __tablename__ = "memory_skill_outcomes"
    __table_args__ = (UniqueConstraint("task_id", "skill_id", name="uq_memory_skill_outcome_task_skill"),
                      CheckConstraint("outcome IN ('success', 'failure')", name="ck_memory_skill_outcome"))

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    skill_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), ForeignKey("memory_skills.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("harness.agent_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    validation_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), ForeignKey("harness.agent_validation_results.id", ondelete="RESTRICT"), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    reported_validation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(utc_datetime(), server_default=func.now(), nullable=False)
