from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
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
    DateTime,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from brain.embeddings.constants import EMBEDDING_DIMENSION
from brain.embeddings.pgvector_sql import pgvector_index_dimension


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    files: Mapped[list["File"]] = relationship("File", back_populates="repository", cascade="all, delete-orphan")
    indexing_runs: Mapped[list["IndexingRun"]] = relationship(
        "IndexingRun", back_populates="repository", cascade="all, delete-orphan"
    )


class File(Base):
    __tablename__ = "files"
    __table_args__ = (
        Index("ix_files_repository_path", "repository_id", "path"),
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
    repo_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)  # e.g., "active", "deprecated", "superseded"
    date: Mapped[date | None] = mapped_column(nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    consequences: Mapped[str | None] = mapped_column(Text, nullable=True)
    affected_features: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)  # List of strings
    affected_modules: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)  # List of strings
    affected_files: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)  # List of strings
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class IndexingRun(Base):
    __tablename__ = "indexing_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    repository_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    commit_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)  # "running", "completed", "degraded", "failed"
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
        Vector(pgvector_index_dimension(EMBEDDING_DIMENSION)), nullable=True
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
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
        DateTime(timezone=True),
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    embedding: Mapped["Embedding | None"] = relationship("Embedding", foreign_keys="[FileCard.embedding_id]")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g., "pending", "in_progress", "completed"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DiffReview(Base):
    __tablename__ = "diff_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    commit_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    report_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g., "approved", "needs_review", "blocked"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


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
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    principal: Mapped["Principal"] = relationship("Principal")


class AuditEvent(Base):
    """Append-only record of who did what through the API (B7c).

    Written by the audit middleware for every mutating authenticated call —
    including denied attempts — so customers can answer "which credential
    changed this" without trusting application logs alone."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    principal_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    principal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    credential_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # allowed | denied | error
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
