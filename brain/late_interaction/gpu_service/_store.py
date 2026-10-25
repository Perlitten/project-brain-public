"""SQLite-backed matrix store: revisioned manifests, release identity, inventory."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Sequence
import numpy as np
from brain.late_interaction.codec import decode_matrix, encode_matrix


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
                """,  # nosec B608
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
                """,  # nosec B608
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
                    """,  # nosec B608
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
                """,  # nosec B608
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
