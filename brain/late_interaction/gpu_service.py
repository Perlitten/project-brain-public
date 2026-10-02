"""Authenticated late-interaction service with a local SQLite matrix store."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Protocol, Sequence
from urllib.parse import urlparse

import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from brain.late_interaction.codec import decode_matrix, encode_matrix


_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
logger = logging.getLogger(__name__)


class AsyncRevisionLock:
    """Writer-exclusive lock: mutations exclude rerank snapshots."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        async with self._condition:
            await self._condition.wait_for(lambda: not self._writer and self._waiting_writers == 0)
            self._readers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._readers -= 1
                if self._readers == 0:
                    self._condition.notify_all()

    @asynccontextmanager
    async def write(self) -> AsyncIterator[None]:
        async with self._condition:
            self._waiting_writers += 1
            try:
                await self._condition.wait_for(lambda: not self._writer and self._readers == 0)
            except BaseException:
                self._waiting_writers -= 1
                self._condition.notify_all()
                raise
            self._waiting_writers -= 1
            self._writer = True
        try:
            yield
        finally:
            async with self._condition:
                self._writer = False
                self._condition.notify_all()


@dataclass(frozen=True)
class ServiceConfig:
    database_path: str = "./data/late-interaction.sqlite3"
    token: str = ""
    environment: str = "production"
    model_name: str = "LiquidAI/LFM2.5-ColBERT-350M"
    model_revision: str = "59633c2e31717b3502343ff566bee9fda3261943"
    license_acknowledged: bool = False
    dimension: int = 128
    query_max_tokens: int = 32
    document_max_tokens: int = 512
    max_candidates: int = 500
    max_documents: int = 64
    max_text_chars: int = 64_000
    encoder_batch_size: int = 16
    score_batch_size: int = 32
    max_concurrency: int = 2
    allow_empty_content_hash: bool = False
    encoder_backend: str = "pylate"
    llama_cpp_url: str = "http://127.0.0.1:8089"
    llama_cpp_timeout_s: float = 60.0
    llama_cpp_max_concurrency: int = 1
    llama_cpp_expected_model_alias: str = ""
    llama_cpp_expected_model_ftype: str = ""
    approved_repository_id: int | None = None
    minimum_index_revision: str | None = None
    minimum_document_count: int = 0

    @classmethod
    def from_env(cls) -> ServiceConfig:
        return cls(
            database_path=os.getenv(
                "LATE_INTERACTION_SERVICE_DATABASE_PATH",
                "./data/late-interaction.sqlite3",
            ),
            token=os.getenv("LATE_INTERACTION_SERVICE_TOKEN", ""),
            environment=os.getenv("LATE_INTERACTION_SERVICE_ENV", "production"),
            model_name=os.getenv(
                "LATE_INTERACTION_SERVICE_MODEL",
                "LiquidAI/LFM2.5-ColBERT-350M",
            ),
            model_revision=os.getenv(
                "LATE_INTERACTION_SERVICE_MODEL_REVISION",
                "59633c2e31717b3502343ff566bee9fda3261943",
            ),
            license_acknowledged=_env_bool("LFM_LICENSE_ACKNOWLEDGED"),
            dimension=int(os.getenv("LATE_INTERACTION_SERVICE_DIMENSION", "128")),
            query_max_tokens=int(os.getenv("LATE_INTERACTION_SERVICE_QUERY_MAX_TOKENS", "32")),
            document_max_tokens=int(os.getenv("LATE_INTERACTION_SERVICE_DOCUMENT_MAX_TOKENS", "512")),
            max_candidates=int(os.getenv("LATE_INTERACTION_SERVICE_MAX_CANDIDATES", "500")),
            max_documents=int(os.getenv("LATE_INTERACTION_SERVICE_MAX_DOCUMENTS", "64")),
            max_text_chars=int(os.getenv("LATE_INTERACTION_SERVICE_MAX_TEXT_CHARS", "64000")),
            encoder_batch_size=int(os.getenv("LATE_INTERACTION_SERVICE_ENCODER_BATCH_SIZE", "16")),
            score_batch_size=int(os.getenv("LATE_INTERACTION_SERVICE_SCORE_BATCH_SIZE", "32")),
            max_concurrency=int(os.getenv("LATE_INTERACTION_SERVICE_MAX_CONCURRENCY", "2")),
            allow_empty_content_hash=_env_bool("LATE_INTERACTION_SERVICE_ALLOW_EMPTY_CONTENT_HASH"),
            encoder_backend=(
                os.getenv("LATE_INTERACTION_SERVICE_ENCODER_BACKEND", "pylate").strip().lower() or "pylate"
            ),
            llama_cpp_url=os.getenv(
                "LATE_INTERACTION_SERVICE_LLAMA_CPP_URL",
                "http://127.0.0.1:8089",
            ).rstrip("/"),
            llama_cpp_timeout_s=float(os.getenv("LATE_INTERACTION_SERVICE_LLAMA_CPP_TIMEOUT_S", "60")),
            llama_cpp_max_concurrency=int(
                os.getenv(
                    "LATE_INTERACTION_SERVICE_LLAMA_CPP_MAX_CONCURRENCY",
                    "1",
                )
            ),
            llama_cpp_expected_model_alias=os.getenv(
                "LATE_INTERACTION_SERVICE_LLAMA_CPP_EXPECTED_MODEL_ALIAS",
                "",
            ),
            llama_cpp_expected_model_ftype=os.getenv(
                "LATE_INTERACTION_SERVICE_LLAMA_CPP_EXPECTED_MODEL_FTYPE",
                "",
            ),
            approved_repository_id=_env_optional_int("LATE_INTERACTION_SERVICE_APPROVED_REPOSITORY_ID"),
            minimum_index_revision=(
                os.getenv(
                    "LATE_INTERACTION_SERVICE_MINIMUM_INDEX_REVISION",
                    "",
                ).strip()
                or None
            ),
            minimum_document_count=int(
                os.getenv(
                    "LATE_INTERACTION_SERVICE_MINIMUM_DOCUMENT_COUNT",
                    "0",
                )
            ),
        )

    def validate_startup(self) -> None:
        if not self.token:
            raise RuntimeError("LATE_INTERACTION_SERVICE_TOKEN is required")
        if self.environment.lower() != "test" and not self.license_acknowledged:
            raise RuntimeError("non-test late-interaction service requires LFM_LICENSE_ACKNOWLEDGED=true")
        positive = {
            "dimension": self.dimension,
            "query_max_tokens": self.query_max_tokens,
            "document_max_tokens": self.document_max_tokens,
            "max_candidates": self.max_candidates,
            "max_documents": self.max_documents,
            "max_text_chars": self.max_text_chars,
            "encoder_batch_size": self.encoder_batch_size,
            "score_batch_size": self.score_batch_size,
            "max_concurrency": self.max_concurrency,
            "llama_cpp_max_concurrency": self.llama_cpp_max_concurrency,
        }
        invalid = [name for name, value in positive.items() if value <= 0]
        if invalid:
            raise RuntimeError("positive late-interaction service settings required: " + ", ".join(invalid))
        if self.max_candidates > 500:
            raise RuntimeError("LATE_INTERACTION_SERVICE_MAX_CANDIDATES cannot exceed 500")
        backend = self.encoder_backend.strip().lower()
        if backend not in {"pylate", "llama_cpp"}:
            raise RuntimeError("LATE_INTERACTION_SERVICE_ENCODER_BACKEND must be pylate or llama_cpp")
        if self.llama_cpp_timeout_s <= 0:
            raise RuntimeError("LATE_INTERACTION_SERVICE_LLAMA_CPP_TIMEOUT_S must be positive")
        if self.approved_repository_id is not None and self.approved_repository_id < 1:
            raise RuntimeError("LATE_INTERACTION_SERVICE_APPROVED_REPOSITORY_ID must be positive")
        if self.minimum_document_count < 0:
            raise RuntimeError("LATE_INTERACTION_SERVICE_MINIMUM_DOCUMENT_COUNT cannot be negative")
        if self.minimum_index_revision is not None and _revision_number(self.minimum_index_revision) is None:
            raise RuntimeError("LATE_INTERACTION_SERVICE_MINIMUM_INDEX_REVISION must use the rN format")
        if self.approved_repository_id is None and (
            self.minimum_index_revision is not None or self.minimum_document_count > 0
        ):
            raise RuntimeError("service index floors require LATE_INTERACTION_SERVICE_APPROVED_REPOSITORY_ID")
        if backend == "llama_cpp":
            parsed = urlparse(self.llama_cpp_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise RuntimeError("LATE_INTERACTION_SERVICE_LLAMA_CPP_URL must be an absolute HTTP(S) URL")
            if parsed.username or parsed.password:
                raise RuntimeError("LATE_INTERACTION_SERVICE_LLAMA_CPP_URL must not contain credentials")
            if self.environment.lower() != "test" and not self.llama_cpp_expected_model_alias:
                raise RuntimeError("production llama_cpp backend requires an expected model alias")
            if self.environment.lower() != "test" and not self.llama_cpp_expected_model_ftype:
                raise RuntimeError("production llama_cpp backend requires an expected model type")


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_optional_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    return int(value) if value else None


def _create_encoder(config: ServiceConfig) -> LateInteractionEncoder:
    backend = config.encoder_backend.strip().lower()
    if backend == "pylate":
        from brain.late_interaction.pylate_encoder import PyLateEncoder

        return PyLateEncoder(
            model_name=config.model_name,
            model_revision=config.model_revision,
            dimension=config.dimension,
            batch_size=config.encoder_batch_size,
            query_max_tokens=config.query_max_tokens,
            document_max_tokens=config.document_max_tokens,
        )
    if backend == "llama_cpp":
        from brain.late_interaction.llama_cpp_encoder import LlamaCppEncoder

        return LlamaCppEncoder(
            base_url=config.llama_cpp_url,
            model_name=config.model_name,
            model_revision=config.model_revision,
            dimension=config.dimension,
            query_max_tokens=config.query_max_tokens,
            document_max_tokens=config.document_max_tokens,
            timeout_s=config.llama_cpp_timeout_s,
            max_concurrency=config.llama_cpp_max_concurrency,
            expected_model_alias=config.llama_cpp_expected_model_alias,
            expected_model_ftype=config.llama_cpp_expected_model_ftype,
        )
    raise RuntimeError("LATE_INTERACTION_SERVICE_ENCODER_BACKEND must be pylate or llama_cpp")


class LateInteractionEncoder(Protocol):
    model_revision: str
    dimension: int

    async def encode_queries(self, texts: Sequence[str]) -> list[np.ndarray]: ...

    async def encode_documents(self, texts: Sequence[str]) -> list[np.ndarray]: ...


@dataclass(frozen=True)
class StoredMatrix:
    chunk_id: int
    path: str
    content_hash: str
    matrix: np.ndarray


@dataclass(frozen=True)
class IndexInventory:
    repository_id: int
    model_revision: str
    index_revision: str
    lineage_id: str
    identity_digest: str
    document_count: int
    bytes: int
    last_update: str | None


@dataclass(frozen=True)
class IndexReleaseIdentity:
    repository_id: int
    model_revision: str
    index_revision: str
    lineage_id: str
    identity_digest: str
    document_count: int


class SQLiteMatrixStore:
    """Single-process persistent store, strictly revision-scoped."""

    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._identity_digest_cache: dict[tuple[int, str], IndexReleaseIdentity] = {}
        self._initialize()

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                CREATE TABLE IF NOT EXISTS late_interaction_documents (
                    repository_id INTEGER NOT NULL,
                    chunk_id INTEGER NOT NULL,
                    model_revision TEXT NOT NULL,
                    path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    token_count INTEGER NOT NULL,
                    dimension INTEGER NOT NULL,
                    vector_dtype TEXT NOT NULL,
                    matrix BLOB NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (repository_id, chunk_id, model_revision)
                );
                CREATE TABLE IF NOT EXISTS late_interaction_manifests (
                    repository_id INTEGER NOT NULL,
                    model_revision TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    lineage_id TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (repository_id, model_revision)
                );
                CREATE TABLE IF NOT EXISTS late_interaction_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO late_interaction_metadata(key, value)
                VALUES ('global_revision', '0');
                """
            )
            manifest_columns = {
                str(row["name"])
                for row in self._connection.execute("PRAGMA table_info(late_interaction_manifests)").fetchall()
            }
            if "lineage_id" not in manifest_columns:
                self._connection.execute("ALTER TABLE late_interaction_manifests ADD COLUMN lineage_id TEXT")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _revision(value: int) -> str:
        return f"r{value}"

    def global_revision(self) -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM late_interaction_metadata WHERE key = 'global_revision'"
            ).fetchone()
            return self._revision(int(row["value"]) if row else 0)

    def index_revision(self, repository_id: int, model_revision: str) -> str:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT revision FROM late_interaction_manifests
                WHERE repository_id = ? AND model_revision = ?
                """,
                (repository_id, model_revision),
            ).fetchone()
            return self._revision(int(row["revision"]) if row else 0)

    def document_count(self, repository_id: int, model_revision: str) -> int:
        """Return readiness count without materializing corpus identities."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COUNT(*) AS document_count
                FROM late_interaction_documents
                WHERE repository_id = ? AND model_revision = ?
                """,
                (repository_id, model_revision),
            ).fetchone()
            return int(row["document_count"]) if row else 0

    def _ensure_manifest_unlocked(
        self,
        repository_id: int,
        model_revision: str,
    ) -> tuple[int, str]:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO late_interaction_manifests (
                repository_id, model_revision, revision, lineage_id, updated_at
            ) VALUES (?, ?, 0, ?, NULL)
            """,
            (repository_id, model_revision, str(uuid.uuid4())),
        )
        row = self._connection.execute(
            """
            SELECT revision, lineage_id
            FROM late_interaction_manifests
            WHERE repository_id = ? AND model_revision = ?
            """,
            (repository_id, model_revision),
        ).fetchone()
        if row is None:
            raise RuntimeError("late-interaction manifest creation failed")
        lineage_id = str(row["lineage_id"] or "")
        if not lineage_id:
            lineage_id = str(uuid.uuid4())
            self._connection.execute(
                """
                UPDATE late_interaction_manifests
                SET lineage_id = ?
                WHERE repository_id = ? AND model_revision = ?
                """,
                (lineage_id, repository_id, model_revision),
            )
            self._identity_digest_cache.pop((repository_id, model_revision), None)
        return int(row["revision"]), lineage_id

    def _identity_digest_unlocked(
        self,
        repository_id: int,
        model_revision: str,
    ) -> str:
        rows = self._connection.execute(
            """
            SELECT chunk_id, path, content_hash
            FROM late_interaction_documents
            WHERE repository_id = ? AND model_revision = ?
            ORDER BY chunk_id
            """,
            (repository_id, model_revision),
        ).fetchall()
        digest = hashlib.sha256()
        for row in rows:
            identity = json.dumps(
                [
                    int(row["chunk_id"]),
                    str(row["path"]),
                    str(row["content_hash"]).lower(),
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            digest.update(identity)
            digest.update(b"\n")
        return digest.hexdigest()

    def _release_identity_unlocked(
        self,
        repository_id: int,
        model_revision: str,
    ) -> IndexReleaseIdentity:
        manifest = self._connection.execute(
            """
            SELECT revision, lineage_id
            FROM late_interaction_manifests
            WHERE repository_id = ? AND model_revision = ?
            """,
            (repository_id, model_revision),
        ).fetchone()
        revision = int(manifest["revision"]) if manifest else 0
        lineage_id = str(manifest["lineage_id"] or "") if manifest else ""
        cache_key = (repository_id, model_revision)
        cached = self._identity_digest_cache.get(cache_key)
        if cached is not None and cached.index_revision == self._revision(revision) and cached.lineage_id == lineage_id:
            return cached
        count_row = self._connection.execute(
            """
            SELECT COUNT(*) AS document_count
            FROM late_interaction_documents
            WHERE repository_id = ? AND model_revision = ?
            """,
            (repository_id, model_revision),
        ).fetchone()
        identity = IndexReleaseIdentity(
            repository_id=repository_id,
            model_revision=model_revision,
            index_revision=self._revision(revision),
            lineage_id=lineage_id,
            identity_digest=self._identity_digest_unlocked(
                repository_id,
                model_revision,
            ),
            document_count=int(count_row["document_count"]) if count_row else 0,
        )
        self._identity_digest_cache[cache_key] = identity
        return identity

    def _cached_release_identity_unlocked(
        self,
        repository_id: int,
        model_revision: str,
    ) -> IndexReleaseIdentity | None:
        manifest = self._connection.execute(
            """
            SELECT revision, lineage_id
            FROM late_interaction_manifests
            WHERE repository_id = ? AND model_revision = ?
            """,
            (repository_id, model_revision),
        ).fetchone()
        revision = int(manifest["revision"]) if manifest else 0
        lineage_id = str(manifest["lineage_id"] or "") if manifest else ""
        cached = self._identity_digest_cache.get((repository_id, model_revision))
        if cached is None or cached.index_revision != self._revision(revision) or cached.lineage_id != lineage_id:
            return None
        return cached

    def release_identity(
        self,
        repository_id: int,
        model_revision: str,
    ) -> IndexReleaseIdentity:
        """Return cached corpus identity for one immutable index revision."""
        with self._lock:
            return self._release_identity_unlocked(
                repository_id,
                model_revision,
            )

    def finalize_release_identity(
        self,
        repository_id: int,
        model_revision: str,
    ) -> IndexReleaseIdentity:
        """Finalize identity and migrate a legacy manifest lineage explicitly."""
        with self._lock, self._connection:
            self._ensure_manifest_unlocked(repository_id, model_revision)
            return self._release_identity_unlocked(
                repository_id,
                model_revision,
            )

    def cached_release_identity(
        self,
        repository_id: int,
        model_revision: str,
    ) -> IndexReleaseIdentity | None:
        """Return only an already-finalized identity; never scan the corpus."""
        with self._lock:
            return self._cached_release_identity_unlocked(
                repository_id,
                model_revision,
            )

    def document_identities(
        self,
        repository_id: int,
        model_revision: str,
        chunk_ids: Sequence[int],
    ) -> dict[int, tuple[str, str]]:
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" for _ in chunk_ids)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT chunk_id, path, content_hash FROM late_interaction_documents
                WHERE repository_id = ? AND model_revision = ?
                  AND chunk_id IN ({placeholders})
                """,
                (repository_id, model_revision, *chunk_ids),
            ).fetchall()
            return {
                int(row["chunk_id"]): (
                    str(row["path"]),
                    str(row["content_hash"]),
                )
                for row in rows
            }

    def put(
        self,
        repository_id: int,
        model_revision: str,
        entries: Sequence[StoredMatrix],
    ) -> str:
        if not entries:
            with self._lock, self._connection:
                revision, _lineage_id = self._ensure_manifest_unlocked(
                    repository_id,
                    model_revision,
                )
            return self._revision(revision)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            for entry in entries:
                matrix = np.asarray(entry.matrix, dtype=np.float32)
                self._connection.execute(
                    """
                    INSERT INTO late_interaction_documents (
                        repository_id, chunk_id, model_revision, path, content_hash,
                        token_count, dimension, vector_dtype, matrix, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'float16', ?, ?)
                    ON CONFLICT(repository_id, chunk_id, model_revision) DO UPDATE SET
                        path = excluded.path,
                        content_hash = excluded.content_hash,
                        token_count = excluded.token_count,
                        dimension = excluded.dimension,
                        vector_dtype = excluded.vector_dtype,
                        matrix = excluded.matrix,
                        updated_at = excluded.updated_at
                    """,
                    (
                        repository_id,
                        entry.chunk_id,
                        model_revision,
                        entry.path,
                        entry.content_hash,
                        int(matrix.shape[0]),
                        int(matrix.shape[1]),
                        encode_matrix(matrix),
                        now,
                    ),
                )
            revision = self._bump_revisions(repository_id, model_revision, now)
        return self._revision(revision)

    def delete(
        self,
        repository_id: int,
        model_revision: str,
        chunk_ids: Sequence[int],
    ) -> tuple[int, str]:
        if not chunk_ids:
            return 0, self.index_revision(repository_id, model_revision)
        placeholders = ",".join("?" for _ in chunk_ids)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                f"""
                DELETE FROM late_interaction_documents
                WHERE repository_id = ? AND model_revision = ?
                  AND chunk_id IN ({placeholders})
                """,
                (repository_id, model_revision, *chunk_ids),
            )
            deleted = max(cursor.rowcount, 0)
            if deleted:
                revision = self._bump_revisions(repository_id, model_revision, now)
            else:
                revision = self._manifest_revision_unlocked(repository_id, model_revision)
        return deleted, self._revision(revision)

    def prune(
        self,
        repository_id: int,
        model_revision: str,
        keep_chunk_ids: Sequence[int],
    ) -> tuple[int, str]:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            if keep_chunk_ids:
                placeholders = ",".join("?" for _ in keep_chunk_ids)
                cursor = self._connection.execute(
                    f"""
                    DELETE FROM late_interaction_documents
                    WHERE repository_id = ? AND model_revision = ?
                      AND chunk_id NOT IN ({placeholders})
                    """,
                    (repository_id, model_revision, *keep_chunk_ids),
                )
            else:
                cursor = self._connection.execute(
                    """
                    DELETE FROM late_interaction_documents
                    WHERE repository_id = ? AND model_revision = ?
                    """,
                    (repository_id, model_revision),
                )
            deleted = max(cursor.rowcount, 0)
            if deleted:
                revision = self._bump_revisions(repository_id, model_revision, now)
            else:
                revision = self._manifest_revision_unlocked(repository_id, model_revision)
        return deleted, self._revision(revision)

    def fetch(
        self,
        repository_id: int,
        model_revision: str,
        chunk_ids: Sequence[int],
    ) -> dict[int, StoredMatrix]:
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" for _ in chunk_ids)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT chunk_id, path, content_hash, token_count, dimension,
                       vector_dtype, matrix
                FROM late_interaction_documents
                WHERE repository_id = ? AND model_revision = ?
                  AND chunk_id IN ({placeholders})
                """,
                (repository_id, model_revision, *chunk_ids),
            ).fetchall()
        return {
            int(row["chunk_id"]): StoredMatrix(
                chunk_id=int(row["chunk_id"]),
                path=str(row["path"]),
                content_hash=str(row["content_hash"]),
                matrix=decode_matrix(
                    row["matrix"],
                    token_count=int(row["token_count"]),
                    dimension=int(row["dimension"]),
                    dtype=str(row["vector_dtype"]),
                ),
            )
            for row in rows
        }

    def inventory(self, repository_id: int, model_revision: str) -> IndexInventory:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COALESCE(SUM(LENGTH(matrix)), 0) AS bytes,
                       MAX(updated_at) AS last_update
                FROM late_interaction_documents
                WHERE repository_id = ? AND model_revision = ?
                """,
                (repository_id, model_revision),
            ).fetchone()
            identity = self._release_identity_unlocked(
                repository_id,
                model_revision,
            )
        return IndexInventory(
            repository_id=repository_id,
            model_revision=model_revision,
            index_revision=identity.index_revision,
            lineage_id=identity.lineage_id,
            identity_digest=identity.identity_digest,
            document_count=identity.document_count,
            bytes=int(row["bytes"]),
            last_update=row["last_update"],
        )

    def cached_inventory(
        self,
        repository_id: int,
        model_revision: str,
    ) -> IndexInventory | None:
        """Return an inventory only when its digest is already finalized."""
        with self._lock:
            identity = self._cached_release_identity_unlocked(
                repository_id,
                model_revision,
            )
            if identity is None:
                return None
            row = self._connection.execute(
                """
                SELECT COALESCE(SUM(LENGTH(matrix)), 0) AS bytes,
                       MAX(updated_at) AS last_update
                FROM late_interaction_documents
                WHERE repository_id = ? AND model_revision = ?
                """,
                (repository_id, model_revision),
            ).fetchone()
        return IndexInventory(
            repository_id=repository_id,
            model_revision=model_revision,
            index_revision=identity.index_revision,
            lineage_id=identity.lineage_id,
            identity_digest=identity.identity_digest,
            document_count=identity.document_count,
            bytes=int(row["bytes"]),
            last_update=row["last_update"],
        )

    def inventory_summary(
        self,
        repository_id: int,
        model_revision: str,
    ) -> IndexInventory:
        """Return bounded metadata without calculating the canonical digest."""
        with self._lock:
            manifest = self._connection.execute(
                """
                SELECT revision, lineage_id, updated_at
                FROM late_interaction_manifests
                WHERE repository_id = ? AND model_revision = ?
                """,
                (repository_id, model_revision),
            ).fetchone()
            row = self._connection.execute(
                """
                SELECT COUNT(*) AS document_count
                FROM late_interaction_documents
                WHERE repository_id = ? AND model_revision = ?
                """,
                (repository_id, model_revision),
            ).fetchone()
        revision = int(manifest["revision"]) if manifest else 0
        return IndexInventory(
            repository_id=repository_id,
            model_revision=model_revision,
            index_revision=self._revision(revision),
            lineage_id=str(manifest["lineage_id"] or "") if manifest else "",
            identity_digest="",
            document_count=int(row["document_count"]),
            bytes=0,
            last_update=str(manifest["updated_at"]) if manifest else None,
        )

    def list_documents(
        self,
        repository_id: int,
        model_revision: str,
        *,
        after_chunk_id: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT chunk_id, path, content_hash
                FROM late_interaction_documents
                WHERE repository_id = ? AND model_revision = ?
                  AND chunk_id > ?
                ORDER BY chunk_id
                LIMIT ?
                """,
                (
                    repository_id,
                    model_revision,
                    max(0, after_chunk_id),
                    max(1, min(limit, 1000)),
                ),
            ).fetchall()
        return [
            {
                "chunk_id": int(row["chunk_id"]),
                "path": str(row["path"]),
                "content_hash": str(row["content_hash"]),
            }
            for row in rows
        ]

    def _manifest_revision_unlocked(self, repository_id: int, model_revision: str) -> int:
        row = self._connection.execute(
            """
            SELECT revision FROM late_interaction_manifests
            WHERE repository_id = ? AND model_revision = ?
            """,
            (repository_id, model_revision),
        ).fetchone()
        return int(row["revision"]) if row else 0

    def _bump_revisions(self, repository_id: int, model_revision: str, now: str) -> int:
        self._ensure_manifest_unlocked(repository_id, model_revision)
        self._connection.execute(
            """
            UPDATE late_interaction_manifests
            SET revision = revision + 1,
                updated_at = ?
            WHERE repository_id = ? AND model_revision = ?
            """,
            (now, repository_id, model_revision),
        )
        self._connection.execute(
            """
            UPDATE late_interaction_metadata
            SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
            WHERE key = 'global_revision'
            """
        )
        self._identity_digest_cache.pop((repository_id, model_revision), None)
        return self._manifest_revision_unlocked(repository_id, model_revision)


class DocumentInput(BaseModel):
    chunk_id: int
    path: str = Field(min_length=1, max_length=4096)
    content_hash: str
    text: str = Field(min_length=1)


class CandidateInput(BaseModel):
    chunk_id: int
    path: str = Field(min_length=1, max_length=4096)
    content_hash: str = ""


class UpsertRequest(BaseModel):
    repository_id: int
    model_revision: str
    index_revision: str | None = None
    minimum_index_revision: str | None = None
    documents: list[DocumentInput] = Field(max_length=64)


class DeleteRequest(BaseModel):
    repository_id: int
    model_revision: str
    index_revision: str | None = None
    minimum_index_revision: str | None = None
    chunk_ids: list[int] = Field(max_length=500)


class PruneRequest(BaseModel):
    repository_id: int
    model_revision: str
    index_revision: str | None = None
    minimum_index_revision: str | None = None
    keep_chunk_ids: list[int] = Field(max_length=5000)


class FinalizeRequest(BaseModel):
    repository_id: int
    model_revision: str


class RerankRequest(BaseModel):
    query: str = Field(min_length=1)
    repository_id: int
    model_revision: str
    index_revision: str | None = None
    minimum_index_revision: str | None = None
    # Requests above the 500 scoring cap are accepted and truncated so callers
    # still get a measured subset instead of a whole-request skip.
    candidates: list[CandidateInput] = Field(max_length=5000)


def _valid_hash(value: str, *, allow_empty: bool) -> bool:
    return bool(_SHA256_RE.fullmatch(value)) or (allow_empty and not value)


def _revision_mismatch(
    *,
    requested_model: str,
    configured_model: str,
    requested_index: str | None,
    minimum_index: str | None,
    current_index: str,
    enforced_minimum_index: str | None = None,
) -> str:
    if requested_model != configured_model:
        return "model_revision_mismatch"
    if requested_index and requested_index != current_index:
        return "index_revision_mismatch"
    for floor in (enforced_minimum_index, minimum_index):
        if not floor:
            continue
        requested_floor = _revision_number(floor)
        current_revision = _revision_number(current_index)
        if requested_floor is None or current_revision is None or current_revision < requested_floor:
            return "index_revision_below_approved_floor"
    return ""


def _revision_number(value: str) -> int | None:
    if not value.startswith("r") or not value[1:].isdigit():
        return None
    return int(value[1:])


def _batched_maxsim(
    query: np.ndarray,
    documents: Sequence[np.ndarray],
    *,
    batch_size: int,
) -> list[float]:
    q = np.asarray(query, dtype=np.float32)
    if q.ndim != 2 or q.shape[0] == 0:
        raise ValueError("query matrix must be non-empty and two-dimensional")
    scores: list[float] = []
    for offset in range(0, len(documents), batch_size):
        batch = [np.asarray(value, dtype=np.float32) for value in documents[offset : offset + batch_size]]
        max_tokens = max(value.shape[0] for value in batch)
        padded = np.zeros((len(batch), max_tokens, q.shape[1]), dtype=np.float32)
        mask = np.zeros((len(batch), max_tokens), dtype=bool)
        for index, matrix in enumerate(batch):
            if matrix.ndim != 2 or matrix.shape[0] == 0 or matrix.shape[1] != q.shape[1]:
                raise ValueError("document/query matrix shape mismatch")
            padded[index, : matrix.shape[0], :] = matrix
            mask[index, : matrix.shape[0]] = True
        similarities = np.einsum("qd,btd->bqt", q, padded, optimize=True)
        similarities = np.where(mask[:, None, :], similarities, -np.inf)
        scores.extend(float(value) for value in similarities.max(axis=2).sum(axis=1))
    return scores


async def _encode_in_batches(
    encoder: LateInteractionEncoder,
    texts: Sequence[str],
    *,
    batch_size: int,
    is_query: bool,
) -> list[np.ndarray]:
    encoded: list[np.ndarray] = []
    for offset in range(0, len(texts), batch_size):
        batch = texts[offset : offset + batch_size]
        values = await encoder.encode_queries(batch) if is_query else await encoder.encode_documents(batch)
        if len(values) != len(batch):
            raise RuntimeError("encoder_batch_size_mismatch")
        encoded.extend(np.asarray(value, dtype=np.float32) for value in values)
    return encoded


def create_app(
    config: ServiceConfig | None = None,
    *,
    encoder: LateInteractionEncoder | None = None,
    store: SQLiteMatrixStore | None = None,
) -> FastAPI:
    config = config or ServiceConfig.from_env()
    config.validate_startup()
    if encoder is None:
        encoder = _create_encoder(config)
    if encoder.model_revision != config.model_revision:
        raise RuntimeError("configured encoder model revision mismatch")
    if encoder.dimension != config.dimension:
        raise RuntimeError("configured encoder dimension mismatch")
    store = store or SQLiteMatrixStore(config.database_path)

    app = FastAPI(title="Project Brain Late Interaction Service", version="1")
    app.state.config = config
    app.state.encoder = encoder
    app.state.store = store
    app.state.semaphore = asyncio.Semaphore(config.max_concurrency)
    app.state.revision_lock = AsyncRevisionLock()
    app.state.identity_refresh_tasks = {}

    def require_approved_repository(repository_id: int) -> None:
        if config.approved_repository_id is not None and repository_id != config.approved_repository_id:
            raise HTTPException(
                status_code=403,
                detail="repository is not approved by service policy",
            )

    def inventory_policy_reason(inventory: IndexInventory) -> str:
        revision_reason = _revision_mismatch(
            requested_model=inventory.model_revision,
            configured_model=config.model_revision,
            requested_index=None,
            minimum_index=None,
            current_index=inventory.index_revision,
            enforced_minimum_index=config.minimum_index_revision,
        )
        if revision_reason:
            return revision_reason
        if inventory.document_count < config.minimum_document_count:
            return "document_count_below_service_minimum"
        return ""

    async def refresh_release_identity(
        repository_id: int,
    ) -> IndexReleaseIdentity:
        return await asyncio.to_thread(
            store.release_identity,
            repository_id,
            config.model_revision,
        )

    def schedule_release_identity_refresh(repository_id: int) -> None:
        """Coalesce maintenance refreshes without blocking a search request."""
        key = (repository_id, config.model_revision)
        existing = app.state.identity_refresh_tasks.get(key)
        if existing is not None and not existing.done():
            return

        async def refresh() -> None:
            try:
                await refresh_release_identity(repository_id)
            except Exception:
                logger.exception(
                    "late-interaction release identity refresh failed",
                    extra={"repository_id": repository_id},
                )
            finally:
                current = asyncio.current_task()
                if app.state.identity_refresh_tasks.get(key) is current:
                    app.state.identity_refresh_tasks.pop(key, None)

        app.state.identity_refresh_tasks[key] = asyncio.create_task(refresh())

    async def drain_release_identity_refreshes() -> None:
        tasks = list(app.state.identity_refresh_tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    app.router.add_event_handler("shutdown", drain_release_identity_refreshes)

    async def revision_read_guard():
        async with app.state.revision_lock.read():
            yield

    async def revision_write_guard():
        async with app.state.revision_lock.write():
            yield

    async def encode_available(
        texts: Sequence[str],
        *,
        batch_size: int,
        is_query: bool,
    ) -> list[np.ndarray]:
        try:
            return await _encode_in_batches(
                encoder,
                texts,
                batch_size=batch_size,
                is_query=is_query,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="late-interaction encoder unavailable",
            ) from exc

    async def require_token(
        supplied: Annotated[str | None, Header(alias="X-Late-Interaction-Token")] = None,
    ) -> None:
        candidate = supplied or ""
        if not hmac.compare_digest(candidate.encode("utf-8"), config.token.encode("utf-8")):
            raise HTTPException(status_code=401, detail="invalid late-interaction token")

    auth = Depends(require_token)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "encoder_backend": config.encoder_backend,
            "model_revision": config.model_revision,
            "index_revision": store.global_revision(),
        }

    @app.get("/ready", dependencies=[auth])
    async def ready() -> dict[str, Any]:
        probe = getattr(encoder, "ready", None)
        if probe is not None:
            try:
                await probe()
            except Exception as exc:
                raise HTTPException(status_code=503, detail="late-interaction encoder unavailable") from exc
        payload: dict[str, Any] = {
            "status": "ready",
            "encoder_backend": config.encoder_backend,
            "model_revision": config.model_revision,
            "index_revision": store.global_revision(),
        }
        if config.approved_repository_id is not None:
            inventory = await asyncio.to_thread(
                store.cached_inventory,
                config.approved_repository_id,
                config.model_revision,
            )
            if inventory is None:
                schedule_release_identity_refresh(
                    config.approved_repository_id,
                )
                raise HTTPException(
                    status_code=503,
                    detail="identity_refresh_pending",
                )
            reason = inventory_policy_reason(inventory)
            if reason:
                raise HTTPException(status_code=503, detail=reason)
            payload.update(
                {
                    **inventory.__dict__,
                    "minimum_document_count": config.minimum_document_count,
                }
            )
        return payload

    @app.get("/v1/index/status", dependencies=[auth])
    async def index_status(repository_id: int, model_revision: str) -> dict[str, Any]:
        require_approved_repository(repository_id)
        inventory = await asyncio.to_thread(
            store.cached_inventory,
            repository_id,
            config.model_revision,
        )
        if inventory is None:
            schedule_release_identity_refresh(repository_id)
            summary = await asyncio.to_thread(
                store.inventory_summary,
                repository_id,
                config.model_revision,
            )
            return {
                "status": "not_ready",
                **summary.__dict__,
                "minimum_document_count": config.minimum_document_count,
                "reason": "identity_refresh_pending",
            }
        mismatch = _revision_mismatch(
            requested_model=model_revision,
            configured_model=config.model_revision,
            requested_index=None,
            minimum_index=None,
            current_index=inventory.index_revision,
            enforced_minimum_index=config.minimum_index_revision,
        )
        if mismatch:
            return {
                "status": "revision_mismatch",
                **inventory.__dict__,
                "minimum_document_count": config.minimum_document_count,
                "reason": mismatch,
            }
        if inventory.document_count < config.minimum_document_count:
            return {
                "status": "not_ready",
                **inventory.__dict__,
                "minimum_document_count": config.minimum_document_count,
                "reason": "document_count_below_service_minimum",
            }
        return {
            "status": "ready",
            **inventory.__dict__,
            "minimum_document_count": config.minimum_document_count,
            "reason": "",
        }

    @app.get("/v1/index/documents", dependencies=[auth])
    async def index_documents(
        repository_id: int,
        model_revision: str,
        after_chunk_id: int = 0,
        limit: int = 1000,
    ) -> dict[str, Any]:
        require_approved_repository(repository_id)
        if after_chunk_id < 0 or not 1 <= limit <= 1000:
            raise HTTPException(
                status_code=422,
                detail="after_chunk_id must be non-negative and limit must be 1..1000",
            )
        current = store.index_revision(repository_id, config.model_revision)
        mismatch = _revision_mismatch(
            requested_model=model_revision,
            configured_model=config.model_revision,
            requested_index=None,
            minimum_index=None,
            current_index=current,
            # Authenticated inventory access is the recovery plane. It must
            # remain readable while a new or restored index is below the
            # serving floor, otherwise remote sync cannot rebuild from r0.
            enforced_minimum_index=None,
        )
        if mismatch:
            return {
                "status": "revision_mismatch",
                "repository_id": repository_id,
                "model_revision": config.model_revision,
                "index_revision": current,
                "documents": [],
                "next_after_chunk_id": None,
                "reason": mismatch,
            }
        documents = await asyncio.to_thread(
            store.list_documents,
            repository_id,
            model_revision,
            after_chunk_id=after_chunk_id,
            limit=limit,
        )
        next_after = documents[-1]["chunk_id"] if len(documents) == limit else None
        return {
            "status": "ready",
            "repository_id": repository_id,
            "model_revision": config.model_revision,
            "index_revision": current,
            "documents": documents,
            "next_after_chunk_id": next_after,
            "reason": "",
        }

    @app.post(
        "/v1/index/finalize",
        dependencies=[auth, Depends(revision_read_guard)],
    )
    async def finalize_index(request: FinalizeRequest) -> dict[str, Any]:
        started = time.perf_counter()
        require_approved_repository(request.repository_id)
        identity = await asyncio.to_thread(
            store.finalize_release_identity,
            request.repository_id,
            config.model_revision,
        )
        inventory = await asyncio.to_thread(
            store.cached_inventory,
            request.repository_id,
            config.model_revision,
        )
        if inventory is None:
            raise HTTPException(
                status_code=503,
                detail="identity_refresh_failed",
            )
        mismatch = _revision_mismatch(
            requested_model=request.model_revision,
            configured_model=config.model_revision,
            requested_index=None,
            minimum_index=None,
            current_index=identity.index_revision,
            enforced_minimum_index=config.minimum_index_revision,
        )
        reason = mismatch or inventory_policy_reason(inventory)
        return {
            "status": "ready" if not reason else "not_ready",
            **inventory.__dict__,
            "minimum_document_count": config.minimum_document_count,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "reason": reason,
        }

    @app.post(
        "/v1/index/upsert",
        dependencies=[auth],
    )
    async def upsert(request: UpsertRequest) -> dict[str, Any]:
        started = time.perf_counter()
        require_approved_repository(request.repository_id)
        if len(request.documents) > config.max_documents:
            raise HTTPException(status_code=413, detail="document batch exceeds service limit")
        if sum(len(document.text) for document in request.documents) > (config.max_documents * config.max_text_chars):
            raise HTTPException(status_code=413, detail="document payload exceeds service limit")
        if any(len(document.text) > config.max_text_chars for document in request.documents):
            raise HTTPException(status_code=413, detail="document text exceeds service limit")
        if any(
            not _valid_hash(
                document.content_hash,
                allow_empty=config.allow_empty_content_hash,
            )
            for document in request.documents
        ):
            raise HTTPException(status_code=422, detail="valid SHA-256 content_hash required")

        encoded: dict[tuple[int, str, str], np.ndarray] = {}
        chunk_ids = [document.chunk_id for document in request.documents]

        def matrix_key(document: DocumentInput) -> tuple[int, str, str]:
            return (
                document.chunk_id,
                document.path,
                document.content_hash.lower(),
            )

        def conflicting_documents(
            existing: dict[int, tuple[str, str]],
        ) -> list[DocumentInput]:
            return [
                document
                for document in request.documents
                if document.chunk_id in existing and existing[document.chunk_id][1] != document.content_hash.lower()
            ]

        def changed_documents(
            existing: dict[int, tuple[str, str]],
        ) -> list[DocumentInput]:
            return [
                document
                for document in request.documents
                if existing.get(document.chunk_id) != (document.path, document.content_hash.lower())
            ]

        while True:
            async with app.state.revision_lock.read():
                current = await asyncio.to_thread(
                    store.index_revision,
                    request.repository_id,
                    config.model_revision,
                )
                mismatch = _revision_mismatch(
                    requested_model=request.model_revision,
                    configured_model=config.model_revision,
                    requested_index=request.index_revision,
                    minimum_index=None,
                    current_index=current,
                )
                if mismatch:
                    return _mutation_response(
                        status="revision_mismatch",
                        model_revision=config.model_revision,
                        index_revision=current,
                        started=started,
                        reason=mismatch,
                    )
                existing = await asyncio.to_thread(
                    store.document_identities,
                    request.repository_id,
                    request.model_revision,
                    chunk_ids,
                )
                if conflicting_documents(existing):
                    return _mutation_response(
                        status="identity_conflict",
                        model_revision=config.model_revision,
                        index_revision=current,
                        started=started,
                        reason="immutable_chunk_content_hash_conflict",
                    )
                changed = changed_documents(existing)

            missing = [document for document in changed if matrix_key(document) not in encoded]
            if missing:
                async with app.state.semaphore:
                    matrices = await encode_available(
                        [document.text for document in missing],
                        batch_size=config.encoder_batch_size,
                        is_query=False,
                    )
                for document, matrix in zip(missing, matrices):
                    if matrix.ndim != 2 or matrix.shape[1] != config.dimension:
                        raise HTTPException(
                            status_code=502,
                            detail="encoder returned invalid matrix shape",
                        )
                    encoded[matrix_key(document)] = matrix
                continue

            async with app.state.revision_lock.write():
                current = await asyncio.to_thread(
                    store.index_revision,
                    request.repository_id,
                    config.model_revision,
                )
                mismatch = _revision_mismatch(
                    requested_model=request.model_revision,
                    configured_model=config.model_revision,
                    requested_index=request.index_revision,
                    minimum_index=None,
                    current_index=current,
                )
                if mismatch:
                    return _mutation_response(
                        status="revision_mismatch",
                        model_revision=config.model_revision,
                        index_revision=current,
                        started=started,
                        reason=mismatch,
                    )
                existing = await asyncio.to_thread(
                    store.document_identities,
                    request.repository_id,
                    request.model_revision,
                    chunk_ids,
                )
                if conflicting_documents(existing):
                    return _mutation_response(
                        status="identity_conflict",
                        model_revision=config.model_revision,
                        index_revision=current,
                        started=started,
                        reason="immutable_chunk_content_hash_conflict",
                    )
                changed = changed_documents(existing)
                if any(matrix_key(document) not in encoded for document in changed):
                    continue
                entries = [
                    StoredMatrix(
                        chunk_id=document.chunk_id,
                        path=document.path,
                        content_hash=document.content_hash.lower(),
                        matrix=encoded[matrix_key(document)],
                    )
                    for document in changed
                ]
                revision = await asyncio.to_thread(
                    store.put,
                    request.repository_id,
                    request.model_revision,
                    entries,
                )
                return _mutation_response(
                    status="updated" if changed else "unchanged",
                    model_revision=config.model_revision,
                    index_revision=revision,
                    started=started,
                    accepted=len(changed),
                    unchanged=len(request.documents) - len(changed),
                )

    @app.post(
        "/v1/index/delete",
        dependencies=[auth, Depends(revision_write_guard)],
    )
    async def delete(request: DeleteRequest) -> dict[str, Any]:
        started = time.perf_counter()
        require_approved_repository(request.repository_id)
        current = store.index_revision(request.repository_id, config.model_revision)
        mismatch = _revision_mismatch(
            requested_model=request.model_revision,
            configured_model=config.model_revision,
            requested_index=request.index_revision,
            minimum_index=None,
            current_index=current,
        )
        if mismatch:
            return _mutation_response(
                status="revision_mismatch",
                model_revision=config.model_revision,
                index_revision=current,
                started=started,
                reason=mismatch,
            )
        deleted, revision = await asyncio.to_thread(
            store.delete,
            request.repository_id,
            request.model_revision,
            request.chunk_ids,
        )
        return _mutation_response(
            status="updated" if deleted else "unchanged",
            model_revision=config.model_revision,
            index_revision=revision,
            started=started,
            deleted=deleted,
        )

    @app.post(
        "/v1/index/prune",
        dependencies=[auth, Depends(revision_write_guard)],
    )
    async def prune(request: PruneRequest) -> dict[str, Any]:
        started = time.perf_counter()
        require_approved_repository(request.repository_id)
        current = store.index_revision(request.repository_id, config.model_revision)
        mismatch = _revision_mismatch(
            requested_model=request.model_revision,
            configured_model=config.model_revision,
            requested_index=request.index_revision,
            minimum_index=None,
            current_index=current,
        )
        if mismatch:
            return _mutation_response(
                status="revision_mismatch",
                model_revision=config.model_revision,
                index_revision=current,
                started=started,
                reason=mismatch,
            )
        deleted, revision = await asyncio.to_thread(
            store.prune,
            request.repository_id,
            request.model_revision,
            request.keep_chunk_ids,
        )
        return _mutation_response(
            status="updated" if deleted else "unchanged",
            model_revision=config.model_revision,
            index_revision=revision,
            started=started,
            deleted=deleted,
        )

    @app.post(
        "/v1/rerank",
        dependencies=[auth, Depends(revision_read_guard)],
    )
    async def rerank(request: RerankRequest) -> dict[str, Any]:
        started = time.perf_counter()
        require_approved_repository(request.repository_id)
        if len(request.query) > config.max_text_chars:
            raise HTTPException(
                status_code=413,
                detail="query text exceeds service limit",
            )
        identity = await asyncio.to_thread(
            store.cached_release_identity,
            request.repository_id,
            config.model_revision,
        )
        if identity is None:
            schedule_release_identity_refresh(request.repository_id)
            inventory = await asyncio.to_thread(
                store.inventory_summary,
                request.repository_id,
                config.model_revision,
            )
            pending_identity = IndexReleaseIdentity(
                repository_id=inventory.repository_id,
                model_revision=inventory.model_revision,
                index_revision=inventory.index_revision,
                lineage_id=inventory.lineage_id,
                identity_digest="",
                document_count=inventory.document_count,
            )
            return _rerank_response(
                status="index_not_ready",
                identity=pending_identity,
                started=started,
                reason="identity_refresh_pending",
            )
        mismatch = _revision_mismatch(
            requested_model=request.model_revision,
            configured_model=config.model_revision,
            requested_index=request.index_revision,
            minimum_index=request.minimum_index_revision,
            current_index=identity.index_revision,
            enforced_minimum_index=config.minimum_index_revision,
        )
        if mismatch:
            return _rerank_response(
                status="revision_mismatch",
                identity=identity,
                started=started,
                reason=mismatch,
            )
        if identity.document_count < config.minimum_document_count:
            return _rerank_response(
                status="index_not_ready",
                identity=identity,
                started=started,
                reason="document_count_below_service_minimum",
            )
        requested_count = len(request.candidates)
        candidates = request.candidates[: config.max_candidates]
        stored = await asyncio.to_thread(
            store.fetch,
            request.repository_id,
            request.model_revision,
            [candidate.chunk_id for candidate in candidates],
        )
        eligible: list[tuple[CandidateInput, StoredMatrix]] = []
        for candidate in candidates:
            value = stored.get(candidate.chunk_id)
            if value is None:
                continue
            if not _valid_hash(candidate.content_hash, allow_empty=config.allow_empty_content_hash):
                continue
            if candidate.content_hash and not hmac.compare_digest(
                candidate.content_hash.lower(),
                value.content_hash,
            ):
                continue
            if not hmac.compare_digest(candidate.path, value.path):
                continue
            eligible.append((candidate, value))
        denominator = max(requested_count, 1)
        coverage = len(eligible) / denominator
        if not eligible:
            return _rerank_response(
                status="insufficient_coverage",
                identity=identity,
                started=started,
                coverage=coverage,
                reason="no_current_candidate_matrices",
            )
        async with app.state.semaphore:
            query_matrices = await encode_available(
                [request.query],
                batch_size=1,
                is_query=True,
            )
            scores = await asyncio.to_thread(
                _batched_maxsim,
                query_matrices[0],
                [value.matrix for _, value in eligible],
                batch_size=config.score_batch_size,
            )
        ranked: list[dict[str, Any]] = [
            {
                "chunk_id": candidate.chunk_id,
                "path": value.path,
                "score": score,
            }
            for (candidate, value), score in zip(eligible, scores)
        ]
        ranked.sort(
            key=lambda item: (
                -float(item["score"]),
                int(item["chunk_id"]),
            )
        )
        partial = requested_count > config.max_candidates or len(eligible) < len(candidates)
        return _rerank_response(
            status="partial" if partial else "scored",
            identity=identity,
            started=started,
            scores=ranked,
            coverage=coverage,
            reason="bounded_or_stale_candidates" if partial else "",
        )

    return app


def _mutation_response(
    *,
    status: str,
    model_revision: str,
    index_revision: str,
    started: float,
    accepted: int = 0,
    unchanged: int = 0,
    deleted: int = 0,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "status": status,
        "accepted": accepted,
        "unchanged": unchanged,
        "deleted": deleted,
        "model_revision": model_revision,
        "index_revision": index_revision,
        "latency_ms": (time.perf_counter() - started) * 1000,
        "reason": reason,
    }


def _rerank_response(
    *,
    status: str,
    identity: IndexReleaseIdentity,
    started: float,
    scores: list[dict[str, Any]] | None = None,
    coverage: float = 0.0,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "status": status,
        "scores": scores or [],
        "coverage": coverage,
        "latency_ms": (time.perf_counter() - started) * 1000,
        "repository_id": identity.repository_id,
        "model_revision": identity.model_revision,
        "index_revision": identity.index_revision,
        "lineage_id": identity.lineage_id,
        "identity_digest": identity.identity_digest,
        "document_count": identity.document_count,
        "reason": reason,
    }


def create_app_from_env() -> FastAPI:
    """Uvicorn factory; startup validation happens before the server binds."""
    return create_app(ServiceConfig.from_env())
