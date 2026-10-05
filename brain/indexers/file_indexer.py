import asyncio
import hashlib
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Union
from sqlalchemy import select, delete, func, update
from loguru import logger

from brain.config.settings import settings
from brain.database.models import Repository, File, FileChunk, FileCard, Symbol, Embedding, IndexingRun
from brain.database.repository_utils import clear_repository_index, purge_repository_graph
from brain.database.session import async_session_factory
from brain.embeddings.store import build_embedding_record
from brain.embeddings.integrity import verify_embeddings
from brain.indexers.reliability import bounded_map, embed_chunks, repository_index_lock, git_source_is_clean
from brain.memory.source_manifest import revision_matches
from brain.workers.runtime import current_job, check_job_lease
from brain.workers.db_fencing import assert_current_db_fence
from brain.llm.router import get_model_router
from brain.indexers.repo_indexer import classify_file, should_ignore_dir, should_ignore_file, get_git_commit_hash
from brain.indexers.symbol_extractors import (
    extract_import_target_symbols,
    extract_structural_symbols,
)
from brain.search.filters import (
    annotate_knowledge_summary,
    classify_knowledge_status,
    should_exclude_from_indexing,
    should_exclude_from_retrieval,
)

# Public chunking defaults. Kept as module constants for configuration and reuse.
#
# Sizing rationale (2026-07-26): the embedding provider enforces a hard input
# cap (NvidiaEmbeddingProvider.MAX_INPUT_CHARS = 2048, probed against the live
# API — a dense 4096-char chunk returns 500). At the previous 100-line chunk
# size a typical chunk was 4000-8000 chars, so more than half of every chunk was
# truncated away before it was ever embedded: the vectors described only the
# first ~40% of the code they claimed to represent. The cap is NOT the bug and
# must not be removed; the chunk was simply too big for it. 50 lines lands most
# chunks under the cap intact, and the overlap keeps symbols that straddle a
# boundary retrievable from both sides.
#
# Changing these values changes the shape of the index: stored vectors from
# earlier runs keep their old boundaries until the affected files are
# re-indexed (incremental indexing is content-hash driven, so edited files
# migrate on their own; a full re-index makes it uniform).
chunk_size = int(os.environ.get("BRAIN_CHUNK_SIZE_LINES", "50"))
chunk_overlap = int(os.environ.get("BRAIN_CHUNK_OVERLAP_LINES", "10"))


def _normalize_chunk_params(max_chunk_lines: int, overlap_lines: int) -> Tuple[int, int]:
    """Clamp chunking settings to safe values to avoid zero-step windows."""
    safe_chunk_lines = int(max_chunk_lines) if max_chunk_lines else chunk_size
    if safe_chunk_lines <= 0:
        logger.warning(
            "chunk_file_content: invalid max_chunk_lines={}, falling back to {}",
            max_chunk_lines,
            chunk_size,
        )
        safe_chunk_lines = chunk_size

    safe_overlap = int(overlap_lines) if overlap_lines else 0
    if safe_overlap < 0:
        logger.warning(
            "chunk_file_content: negative chunk_overlap={}, clamping to 0",
            overlap_lines,
        )
        safe_overlap = 0

    if safe_overlap >= safe_chunk_lines:
        logger.warning(
            "chunk_file_content: chunk_overlap={} >= chunk_size={}; reducing overlap to {}",
            safe_overlap,
            safe_chunk_lines,
            max(0, safe_chunk_lines - 1),
        )
        safe_overlap = max(0, safe_chunk_lines - 1)

    return safe_chunk_lines, safe_overlap


def _iter_overlap_ranges(
    start_line: int, end_line: int, max_chunk_lines: int, overlap_lines: int
) -> List[Tuple[int, int]]:
    """Yield inclusive line ranges with overlap between consecutive chunks."""
    if end_line < start_line or max_chunk_lines <= 0:
        return []

    ranges: List[Tuple[int, int]] = []
    cursor = start_line
    while cursor <= end_line:
        chunk_start = cursor
        chunk_end = min(cursor + max_chunk_lines - 1, end_line)
        ranges.append((chunk_start, chunk_end))
        if chunk_end >= end_line:
            break

        cursor = chunk_end - overlap_lines + 1
        if cursor <= chunk_start:
            logger.warning(
                "chunk_file_content: overlap window stalled at {}; forcing one-line progression",
                cursor,
            )
            cursor = chunk_start + 1

    return ranges


def _infer_embedding_text_limit(embedding_provider: Any) -> Optional[int]:
    """Return provider-level char limit if explicitly configured."""
    for candidate in ("MAX_INPUT_CHARS", "max_input_chars", "max_input_characters"):
        limit = getattr(embedding_provider, candidate, None)
        if isinstance(limit, int) and limit > 0:
            return limit
    return None


def _coerce_symbol_line(value: Any, fallback: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return fallback


# Helper to calculate MD5 hash of a file's content
def calculate_file_hash(content: str) -> str:
    return hashlib.md5(content.encode("utf-8", errors="ignore")).hexdigest()


# Simple symbol extractor using regex for Python, TS/JS, Kotlin, Go, Rust
def extract_symbols_from_content(content: str, ext: str) -> List[Dict[str, Any]]:
    symbols: List[Dict[str, Any]] = []
    lines = content.splitlines()

    # Python extractor
    if ext == ".py":
        for i, line in enumerate(lines):
            # Class match
            class_match = re.match(r"^\s*class\s+([a-zA-Z0-9_]+)", line)
            if class_match:
                symbols.append(
                    {
                        "name": class_match.group(1),
                        "kind": "class",
                        "signature": line.strip(),
                        "start_line": i + 1,
                        "end_line": i + 1,  # will update end_line later
                    }
                )
            # Function match (also captures async def)
            func_match = re.match(r"^\s*(?:async\s+)?def\s+([a-zA-Z0-9_]+)", line)
            if func_match:
                symbols.append(
                    {
                        "name": func_match.group(1),
                        "kind": "function",
                        "signature": line.strip(),
                        "start_line": i + 1,
                        "end_line": i + 1,
                    }
                )
    # TS/JS extractor
    elif ext in {".ts", ".tsx", ".js", ".jsx"}:
        for i, line in enumerate(lines):
            class_match = re.search(r"\bclass\s+([a-zA-Z0-9_]+)", line)
            if class_match:
                symbols.append(
                    {
                        "name": class_match.group(1),
                        "kind": "class",
                        "signature": line.strip(),
                        "start_line": i + 1,
                        "end_line": i + 1,
                    }
                )
            func_match = re.search(
                r"\b(?:function\s+([a-zA-Z0-9_]+)|const\s+([a-zA-Z0-9_]+)\s*=\s*(?:\([^)]*\)|[a-zA-Z0-9_]+)\s*=>)", line
            )
            if func_match:
                name = func_match.group(1) or func_match.group(2)
                if name:
                    symbols.append(
                        {
                            "name": name,
                            "kind": "function",
                            "signature": line.strip(),
                            "start_line": i + 1,
                            "end_line": i + 1,
                        }
                    )
    # Kotlin extractor
    elif ext in {".kt", ".kts"}:
        for i, line in enumerate(lines):
            class_match = re.search(r"\b(?:class|interface|object)\s+([a-zA-Z0-9_]+)", line)
            if class_match:
                symbols.append(
                    {
                        "name": class_match.group(1),
                        "kind": "class",
                        "signature": line.strip(),
                        "start_line": i + 1,
                        "end_line": i + 1,
                    }
                )
            func_match = re.search(r"\bfun\s+([a-zA-Z0-9_]+)", line)
            if func_match:
                symbols.append(
                    {
                        "name": func_match.group(1),
                        "kind": "function",
                        "signature": line.strip(),
                        "start_line": i + 1,
                        "end_line": i + 1,
                    }
                )
    # Go extractor
    elif ext == ".go":
        for i, line in enumerate(lines):
            type_match = re.search(r"^type\s+([a-zA-Z0-9_]+)\s+(?:struct|interface)", line)
            if type_match:
                symbols.append(
                    {
                        "name": type_match.group(1),
                        "kind": "class",
                        "signature": line.strip(),
                        "start_line": i + 1,
                        "end_line": i + 1,
                    }
                )
            func_match = re.search(r"^func\s+(?:\([^)]*\)\s+)?([a-zA-Z0-9_]+)\(", line)
            if func_match:
                symbols.append(
                    {
                        "name": func_match.group(1),
                        "kind": "function",
                        "signature": line.strip(),
                        "start_line": i + 1,
                        "end_line": i + 1,
                    }
                )
    # Rust extractor
    elif ext == ".rs":
        for i, line in enumerate(lines):
            struct_match = re.search(r"\b(?:struct|enum|trait)\s+([a-zA-Z0-9_]+)", line)
            if struct_match:
                symbols.append(
                    {
                        "name": struct_match.group(1),
                        "kind": "class",
                        "signature": line.strip(),
                        "start_line": i + 1,
                        "end_line": i + 1,
                    }
                )
            func_match = re.search(r"\bfn\s+([a-zA-Z0-9_]+)\b", line)
            if func_match:
                symbols.append(
                    {
                        "name": func_match.group(1),
                        "kind": "function",
                        "signature": line.strip(),
                        "start_line": i + 1,
                        "end_line": i + 1,
                    }
                )

    # Update end_line for symbols
    for idx, sym in enumerate(symbols):
        if idx < len(symbols) - 1:
            sym["end_line"] = max(sym["start_line"], symbols[idx + 1]["start_line"] - 1)
        else:
            sym["end_line"] = len(lines)

    return symbols


# Chunking logic: split text into symbol-based or line-based chunks
def chunk_file_content(
    content: str,
    symbols: List[Dict[str, Any]],
    max_chunk_lines: int = chunk_size,
    chunk_overlap_lines: int = chunk_overlap,
) -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []
    lines = content.splitlines()
    total_lines = len(lines)
    max_chunk_lines, chunk_overlap_lines = _normalize_chunk_params(
        max_chunk_lines,
        chunk_overlap_lines,
    )

    if not lines:
        return []

    if not symbols:
        for chunk_start, chunk_end in _iter_overlap_ranges(
            1,
            total_lines,
            max_chunk_lines,
            chunk_overlap_lines,
        ):
            chunk_content = "\n".join(lines[chunk_start - 1 : chunk_end])
            chunks.append(
                {
                    "chunk_index": len(chunks),
                    "content": chunk_content,
                    "start_line": chunk_start,
                    "end_line": chunk_end,
                }
            )
        return chunks

    # Otherwise, chunk based on symbol boundaries; split any long symbol segments with overlap.
    current_line = 1
    sorted_symbols = sorted(
        symbols,
        key=lambda x: _coerce_symbol_line(x.get("start_line"), 1),
    )

    for sym in sorted_symbols:
        sym_start = _coerce_symbol_line(sym.get("start_line"), current_line)
        sym_end = _coerce_symbol_line(sym.get("end_line"), sym_start)
        if sym_end < sym_start:
            sym_end = sym_start
        if sym_start > total_lines or sym_end < 1:
            continue
        sym_start = min(max(1, sym_start), total_lines)
        sym_end = min(max(sym_start, sym_end), total_lines)

        if sym_start > current_line:
            for chunk_start, chunk_end in _iter_overlap_ranges(
                current_line,
                sym_start - 1,
                max_chunk_lines,
                chunk_overlap_lines,
            ):
                chunk_content = "\n".join(lines[chunk_start - 1 : chunk_end])
                if chunk_content.strip():
                    chunks.append(
                        {
                            "chunk_index": len(chunks),
                            "content": chunk_content,
                            "start_line": chunk_start,
                            "end_line": chunk_end,
                        }
                    )

        sym_chunk_start = max(sym_start, current_line)
        for chunk_start, chunk_end in _iter_overlap_ranges(
            sym_chunk_start,
            sym_end,
            max_chunk_lines,
            chunk_overlap_lines,
        ):
            chunk_content = "\n".join(lines[chunk_start - 1 : chunk_end])
            if chunk_content.strip():
                chunks.append(
                    {
                        "chunk_index": len(chunks),
                        "content": chunk_content,
                        "start_line": chunk_start,
                        "end_line": chunk_end,
                    }
                )
        current_line = max(current_line, sym_end + 1)

    # If there is remaining text at the end, chunk it
    if current_line <= total_lines:
        for chunk_start, chunk_end in _iter_overlap_ranges(
            current_line,
            total_lines,
            max_chunk_lines,
            chunk_overlap_lines,
        ):
            chunk_content = "\n".join(lines[chunk_start - 1 : chunk_end])
            if chunk_content.strip():
                chunks.append(
                    {
                        "chunk_index": len(chunks),
                        "content": chunk_content,
                        "start_line": chunk_start,
                        "end_line": chunk_end,
                    }
                )

    return chunks


def _read_text_file(path, errors: str = "ignore") -> str:
    """Blocking file read — call via asyncio.to_thread to keep the loop free."""
    with open(path, "r", encoding="utf-8", errors=errors) as f:
        text = f.read()
    if "\x00" in text:
        raise ValueError("file contains NUL bytes; treating as binary")
    return text


def compute_run_status(*, processed: int, unchanged: int, failed: int,
                       graph_failed: int, graph_error: bool) -> str:
    if not failed and not graph_failed and not graph_error:
        return "completed"
    return "degraded" if processed + unchanged + graph_failed > 0 else "failed"


def _scan_repo_files(path_obj: Path) -> List[Path]:
    """Walk the repo (blocking os.walk/listdir) and return the includable files.
    Runs in a worker thread so the scan doesn't stall the event loop."""
    scanned: List[Path] = []
    for root, dirs, files in os.walk(str(path_obj)):
        for d in list(dirs):
            if should_ignore_dir(d):
                dirs.remove(d)
        for f in files:
            if should_ignore_file(f):
                continue
            full_path = Path(root) / f
            rel_path = full_path.relative_to(path_obj).as_posix()
            if should_exclude_from_indexing(rel_path):
                continue
            scanned.append(full_path)
    return scanned


def _source_content_snapshot(path_obj: Path, files: List[Path]) -> dict:
    hashes = {}
    for filepath in files:
        if filepath.stat().st_size > 10 * 1024 * 1024:
            digest = hashlib.sha256()
            with filepath.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            value = digest.hexdigest()
        else:
            try:
                value = calculate_file_hash(_read_text_file(filepath))
            except ValueError:  # Binary files are excluded by index_file.
                value = hashlib.sha256(filepath.read_bytes()).hexdigest()
        hashes[filepath.relative_to(path_obj).as_posix()] = value
    return hashes


def source_content_digest(path_obj: Path, files: List[Path]) -> str:
    hashes = _source_content_snapshot(path_obj, files)
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


class FileIndexer:
    """Handles parsing, chunking, embedding generation, and DB persistence for files."""

    def __init__(self):
        self.router = get_model_router()
        self.graph_client: Any = None  # Scoped GraphClient, or its batching proxy.
        self._late_provider = None
        self._late_dual_write_failures = 0
        self._late_dual_write_circuit_open = False
        self._provider_slots = asyncio.Semaphore(settings.INDEX_PROVIDER_CONCURRENCY)
        self._progress_lock = asyncio.Lock()
        self._repository_run_lock = asyncio.Lock()
        self._reset_progress()

    def _reset_progress(self) -> None:
        self.progress: dict[str, Any] = {
            "phase": "scan",
            "files": dict(discovered=0, skipped=0, processed=0, failed=0),
            "chunks": dict(discovered=0, skipped=0, processed=0, failed=0),
            "graph": dict(discovered=0, processed=0, failed=0),
            "provider_failures": [], "provider_failure_count": 0,
        }
        self.verification: dict[str, Any] = {}
        self.indexing_run_id: int | None = None
        self._source_content_hashes: dict[str, str] | None = None
        self.file_counts: dict[str, Any] = dict(discovered=0, indexed=0, unchanged=0, excluded=0,
                                failed=0, graph_failed=0, graph_post_process_error=None, error_samples=[])

    def _failure(self, phase: str, exc: Exception, path: str = "", chunk_index=None) -> None:
        # Exception strings can contain request URLs/headers. Only safe metadata
        # is durable; bound examples without dropping the total failure count.
        response = getattr(exc, "response", None)
        failure = {"phase": phase, "path": path, "chunk_index": chunk_index,
                   "error_type": type(exc).__name__, "status_code": getattr(response, "status_code", None)}
        self.progress["provider_failure_count"] += 1
        if phase.startswith("graph") and phase != "graph_file" and not self.file_counts["graph_post_process_error"]:
            self.file_counts["graph_post_process_error"] = type(exc).__name__
        if len(self.progress["provider_failures"]) < 100:
            self.progress["provider_failures"].append(failure)

    async def _publish_progress(self) -> None:
        async with self._progress_lock:
            await check_job_lease()
            import copy
            snapshot = copy.deepcopy(self.progress)
            now = datetime.now(timezone.utc)
            async with async_session_factory() as session:
                async with session.begin():
                    await assert_current_db_fence(session)
                    await session.execute(update(IndexingRun).where(IndexingRun.id == self.indexing_run_id)
                                          .values(progress=snapshot, updated_at=now))
            runtime = current_job.get()
            if runtime is not None:
                await runtime.report({**snapshot, "indexing_run_id": self.indexing_run_id})

    async def _progress_heartbeat(self) -> None:
        while True:
            await asyncio.sleep(settings.INDEX_PROGRESS_INTERVAL_SECONDS)
            try:
                await self._publish_progress()
            except Exception as exc:
                # Foreground progress and the terminal commit still enforce
                # ownership. Transient heartbeat errors are retried next tick.
                logger.warning("Indexing progress heartbeat failed: {}", type(exc).__name__)

    async def _reset_late_dual_write_circuit(self) -> None:
        if self._late_provider is not None:
            await self._late_provider.aclose()
        self._late_provider = None
        self._late_dual_write_failures = 0
        self._late_dual_write_circuit_open = False

    async def _record_late_dual_write_failure(self) -> None:
        self._late_dual_write_failures += 1
        threshold = settings.LATE_INTERACTION_DUAL_WRITE_MAX_CONSECUTIVE_FAILURES
        if self._late_dual_write_failures < threshold:
            return
        self._late_dual_write_circuit_open = True
        if self._late_provider is not None:
            await self._late_provider.aclose()
            self._late_provider = None
        logger.error(
            "Late-interaction dual-write circuit opened after {} consecutive failures",
            self._late_dual_write_failures,
        )

    async def _sync_remote_late_documents(
        self,
        *,
        repository_id: int,
        path: str,
        documents: list[dict[str, Any]],
        obsolete_chunk_ids: list[int],
    ) -> None:
        """Dual-write chunk text/identity to the co-located GPU service.

        This runs only after the authoritative Postgres transaction commits.
        Failures therefore cannot roll back dense indexing, and active ranking
        remains protected by the candidate-coverage gate.
        """
        if not documents or self._late_dual_write_circuit_open:
            return

        from brain.late_interaction.client import (
            LateInteractionDocument,
            get_late_interaction_client,
        )
        from brain.late_interaction.metrics import increment

        client = get_late_interaction_client()
        batch_size = settings.LATE_INTERACTION_REMOTE_MAX_DOCUMENTS
        for offset in range(0, len(documents), batch_size):
            batch = [
                LateInteractionDocument(
                    chunk_id=int(document["chunk_id"]),
                    path=path,
                    content_hash=str(document["content_hash"]),
                    text=str(document["text"]),
                )
                for document in documents[offset : offset + batch_size]
            ]
            result = await client.upsert_documents(repository_id, batch)
            if result.status not in {"updated", "unchanged"}:
                increment("dual_write_failures")
                await self._record_late_dual_write_failure()
                logger.warning(
                    "Remote late-interaction dual-write failed open for {}: {}",
                    path,
                    result.reason or result.status,
                )
                return
            increment("dual_write_successes", max(1, len(batch)))
            self._late_dual_write_failures = 0

        for offset in range(0, len(obsolete_chunk_ids), 500):
            deletion = await client.delete_documents(
                repository_id,
                obsolete_chunk_ids[offset : offset + 500],
            )
            if deletion.status in {"updated", "unchanged"}:
                continue
            increment("dual_write_failures")
            await self._record_late_dual_write_failure()
            logger.warning(
                "Remote late-interaction cleanup failed open for {}: {}",
                path,
                deletion.reason or deletion.status,
            )
            return

    async def index_repository(self, repo_path: Union[str, Path], clean: bool = False,
                               expected_revision: str = "") -> Repository:
        path_obj = Path(repo_path).resolve()
        if not path_obj.is_dir():
            raise ValueError("Repository source directory does not exist")
        async with self._repository_run_lock:
            self._reset_progress()
            async with repository_index_lock(path_obj.as_posix()):
                return await self._index_repository(path_obj, clean, expected_revision)

    async def _index_repository(self, path_obj: Path, clean: bool, expected_revision: str) -> Repository:
        """Scan a repository, identify new or changed files, index them and save to database."""
        await self._reset_late_dual_write_circuit()
        repo_name = path_obj.name
        commit_hash = get_git_commit_hash(path_obj)
        if expected_revision and not revision_matches(expected_revision, commit_hash):
            raise ValueError("Repository source revision does not match requested revision")
        if settings.ENVIRONMENT.lower() == "production" and commit_hash == "unverifiable":
            raise ValueError(
                f"Refusing to index unverifiable snapshot {path_obj}; generate .brain-source-manifest.json first"
            )

        async with async_session_factory() as session:
            async with session.begin():
                await assert_current_db_fence(session)
                # 1. Fetch or create Repository
                stmt = select(Repository).where(Repository.path == path_obj.as_posix())
                res = await session.execute(stmt)
                repo = res.scalar_one_or_none()

                if not repo:
                    repo = Repository(
                        name=repo_name,
                        path=path_obj.as_posix(),
                        type="git" if len(commit_hash) == 40 else "snapshot",
                        indexing_status="indexing",
                    )
                    session.add(repo)
                    await session.flush()
                else:
                    repo.indexing_status = "indexing"

                # Create Indexing Run entry
                indexing_run = IndexingRun(repository_id=repo.id, commit_hash=commit_hash, status="running")
                session.add(indexing_run)
                await session.flush()
                repo_id = repo.id
                indexing_run_id = indexing_run.id
                self.indexing_run_id = indexing_run_id
                # The repository lock proves no older run still owns its work.
                await session.execute(update(IndexingRun).where(IndexingRun.repository_id == repo_id,
                    IndexingRun.id != indexing_run_id, IndexingRun.status == "running")
                    .values(status="failed", completed_at=datetime.now(timezone.utc),
                            verification={"status": "interrupted", "reason": "worker restart"}))

            # Initialize GraphClient with the repository_id now that we have it
            from brain.graph.graph_client import GraphClient

            self.graph_client = GraphClient(repository_id=repo_id)

            started = time.monotonic()
            heartbeat = asyncio.create_task(self._progress_heartbeat())
            try:
                scanned_files = sorted(await asyncio.to_thread(_scan_repo_files, path_obj))
                if not await asyncio.to_thread(git_source_is_clean, path_obj, scanned_files):
                    raise ValueError("Repository working tree must be clean and indexed files tracked")
                self._source_content_hashes = await asyncio.to_thread(_source_content_snapshot, path_obj, scanned_files)
                if clean:
                    # The cleanup opens its own fenced transaction: don't hold
                    # the job-lease row lock in an outer transaction here.
                    await clear_repository_index(repo_id)
                    await check_job_lease()
                    await purge_repository_graph(repo_id)
                self.progress["phase"] = "files"
                self.progress["files"]["discovered"] = len(scanned_files)
                self.file_counts["discovered"] = len(scanned_files)
                await self._publish_progress()

                async def process(filepath):
                    try:
                        outcome = await self.index_file(repo_id, path_obj, filepath)
                        if outcome in {"excluded", "skipped_large", "unchanged"}:
                            self.progress["files"]["skipped"] += 1
                            self.file_counts["unchanged" if outcome == "unchanged" else "excluded"] += 1
                        else:
                            self.progress["files"]["processed"] += 1
                            self.file_counts["graph_failed" if outcome == "indexed_graph_failed" else "indexed"] += 1
                    except Exception as exc:
                        self.progress["files"]["failed"] += 1
                        self.file_counts["failed"] += 1
                        if len(self.file_counts["error_samples"]) < 25:
                            self.file_counts["error_samples"].append({"file": filepath.relative_to(path_obj).as_posix(),
                                                                     "error": type(exc).__name__})
                        self._failure("file", exc, filepath.relative_to(path_obj).as_posix())
                    await self._publish_progress()

                concurrency = settings.INDEX_FILE_CONCURRENCY
                # The optional local GPU provider has shared circuit/lifecycle
                # state and is not safe for concurrent inference.
                if settings.LATE_INTERACTION_ENABLED and settings.LATE_INTERACTION_DUAL_WRITE_ENABLED:
                    concurrency = 1
                await bounded_map(scanned_files, process, concurrency)
                await self._remove_stale_files(repo_id, {fp.relative_to(path_obj).as_posix() for fp in scanned_files}, repo_name)
                self.progress["phase"] = "graph"
                await self._publish_progress()
                from brain.graph.write_buffer import GraphWriteBuffer
                graph_client = self.graph_client
                self.graph_client = GraphWriteBuffer(graph_client)
                try:
                    await self._post_process_graph(repo.name, path_obj, scanned_files)
                    await self.graph_client.flush()
                finally:
                    self.graph_client = graph_client
                self.progress["phase"] = "verification"
                await self._publish_progress()
                embeddings = await verify_embeddings(repo_id, path_obj.as_posix())
                final_files = sorted(await asyncio.to_thread(_scan_repo_files, path_obj))
                final_hashes = await asyncio.to_thread(_source_content_snapshot, path_obj, final_files)
                source_verified = (get_git_commit_hash(path_obj) == commit_hash
                    and await asyncio.to_thread(git_source_is_clean, path_obj, final_files)
                    and final_hashes == self._source_content_hashes)
                projection_status = compute_run_status(processed=self.file_counts["indexed"],
                    unchanged=self.file_counts["unchanged"], failed=self.progress["files"]["failed"],
                    graph_failed=self.file_counts["graph_failed"], graph_error=bool(self.progress["graph"]["failed"]))
                status = ("failed" if not source_verified else projection_status if projection_status != "completed"
                          else "completed" if embeddings.get("pass") else "degraded")
                self.verification = {
                    "status": status, "repository_id": repo_id, "repository_path": path_obj.as_posix(),
                    "commit_hash": commit_hash, "indexing_run_id": indexing_run_id,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "source_revision_verified": source_verified,
                    "source_content_digest": hashlib.sha256(json.dumps(self._source_content_hashes, sort_keys=True).encode()).hexdigest(),
                    "embedding_verification": embeddings,
                    "counts": self.progress,
                    "file_counts": self.file_counts,
                }
                self.progress["phase"] = status
                await self._publish_progress()
                # Completion is committed only after projection and verification.
                async with session.begin():
                    await assert_current_db_fence(session)
                    repo.indexing_status = status
                    if source_verified and projection_status == "completed":
                        repo.last_indexed_commit = commit_hash
                    run_obj = (await session.execute(select(IndexingRun).where(IndexingRun.id == indexing_run_id))).scalar_one()
                    run_obj.status = status
                    run_obj.verification = self.verification
                    run_obj.file_counts = self.file_counts
                    run_obj.completed_at = datetime.now(timezone.utc)
                return repo
            except BaseException as exc:
                self.progress["phase"] = "interrupted"
                self.verification = {
                    "status": "failed", "repository_id": repo_id, "repository_path": path_obj.as_posix(),
                    "commit_hash": commit_hash, "indexing_run_id": indexing_run_id,
                    "duration_seconds": round(time.monotonic() - started, 3), "counts": self.progress,
                    "source_revision_verified": False, "error_type": type(exc).__name__,
                }
                try:
                    async with async_session_factory() as failure_session:
                        async with failure_session.begin():
                            await assert_current_db_fence(failure_session)
                            await failure_session.execute(update(IndexingRun).where(IndexingRun.id == indexing_run_id)
                                .values(status="failed", progress=self.progress, file_counts=self.file_counts,
                                        updated_at=datetime.now(timezone.utc), completed_at=datetime.now(timezone.utc),
                                        verification=self.verification))
                            await failure_session.execute(update(Repository).where(Repository.id == repo_id)
                                                          .values(indexing_status="failed"))
                except Exception as persistence_error:
                    logger.warning("Could not persist interrupted indexing run: {}", type(persistence_error).__name__)
                raise
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                if self._late_provider is not None:
                    await self._late_provider.aclose()
                    self._late_provider = None

    async def _remove_stale_files(self, repo_id: int, scanned_rel_paths: set[str], repo_name: str) -> None:
        """Delete indexed files that no longer exist in the repository tree."""
        async with async_session_factory() as session:
            async with session.begin():
                await assert_current_db_fence(session)
                stmt = select(File).where(File.repository_id == repo_id)
                existing_files = (await session.execute(stmt)).scalars().all()
                for file_obj in existing_files:
                    if file_obj.path not in scanned_rel_paths:
                        await session.delete(file_obj)
        try:
            await self.graph_client.delete_repository_files_not_in(scanned_rel_paths)
        except Exception as exc:
            logger.warning(f"Neo4j stale file purge failed: {exc}")
            self.progress["graph"]["failed"] += 1
            self._failure("graph_purge", exc)

    async def backfill_v6_symbols(self, repo_path: Union[str, Path]) -> Dict[str, int]:
        """Add v6 P1 structural + import_target symbols to an already-indexed repo.

        Does NOT regenerate chunks or embeddings — existing files keep their frozen
        vectors (v6-off stays byte-identical to v5). Newly-includable files that are
        not yet indexed (e.g. a previously-excluded `test_server.py`) are fully indexed.
        Idempotent: prior v6 symbol rows are cleared before re-insert.
        """
        from brain.search.surfaces import IMPORT_TARGET_KIND, V6_SYMBOL_KINDS

        path_obj = Path(repo_path).resolve()
        stats = {"files_refreshed": 0, "structural_symbols": 0, "import_targets": 0, "newly_indexed": 0}
        v6_kinds = set(V6_SYMBOL_KINDS) | {IMPORT_TARGET_KIND}

        async with async_session_factory() as session:
            repo = (
                await session.execute(select(Repository).where(Repository.path == path_obj.as_posix()))
            ).scalar_one_or_none()
        if not repo:
            logger.error(f"backfill_v6_symbols: repo not indexed: {path_obj}")
            return stats
        repo_id = repo.id

        scanned: List[Path] = await asyncio.to_thread(_scan_repo_files, path_obj)
        all_rel = [p.relative_to(path_obj).as_posix() for p in scanned]

        async with async_session_factory() as session:
            existing = {
                f.path: f.id
                for f in (await session.execute(select(File).where(File.repository_id == repo_id))).scalars().all()
            }

        for full in scanned:
            rel = full.relative_to(path_obj).as_posix()
            ext = full.suffix.lower()
            if rel not in existing:
                # Newly includable (exclusion narrowed) — full index for embeddings.
                try:
                    await self.index_file(repo_id, path_obj, full)
                    stats["newly_indexed"] += 1
                except Exception as exc:
                    logger.error(f"backfill_v6_symbols: index of new file {rel} failed: {exc}")
                continue
            try:
                content = await asyncio.to_thread(_read_text_file, full)
            except Exception:
                continue
            structural = extract_structural_symbols(content, ext, rel)
            imports = extract_import_target_symbols(content, ext, rel, all_rel)
            if not structural and not imports:
                continue
            file_id = existing[rel]
            async with async_session_factory() as session:
                async with session.begin():
                    await assert_current_db_fence(session)
                    await session.execute(delete(Symbol).where(Symbol.file_id == file_id, Symbol.kind.in_(v6_kinds)))
                    for sym in structural + imports:
                        session.add(
                            Symbol(
                                file_id=file_id,
                                name=sym["name"][:255],
                                kind=sym["kind"],
                                signature=sym["signature"],
                                start_line=sym["start_line"],
                                end_line=sym["end_line"],
                                summary=f"v6 {sym['kind']} {sym['name']} in {rel}",
                            )
                        )
            stats["files_refreshed"] += 1
            stats["structural_symbols"] += len(structural)
            stats["import_targets"] += len(imports)

        logger.info(f"backfill_v6_symbols: {stats}")
        return stats

    async def build_file_cards(
        self, repo_path: Union[str, Path], fmt: Optional[str] = None, only_paths: Optional[set] = None
    ) -> Dict[str, Any]:
        """v6 P3 — build/refresh deterministic file cards + card embeddings.

        Idempotent (card_hash gates re-embed). Never regenerates chunk embeddings.
        Card vectors live in ``embeddings`` with entity_type='file_card'.
        ``only_paths`` restricts the build to a subset (used by the format micro-test).
        """
        from brain.config.settings import settings
        from brain.embeddings.card_embeddings import get_card_embedding_provider
        from brain.embeddings.store import build_embedding_record
        from brain.indexers.card_builder import CARD_EXTRACTOR_VERSION, build_card

        card_fmt = fmt or settings.RETRIEVAL_CARD_TEXT_FORMAT
        card_provider = get_card_embedding_provider()
        path_obj = Path(repo_path).resolve()
        stats: Dict[str, Any] = {
            "format": card_fmt,
            "eligible": 0,
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "embed_failures": 0,
            "extractor_version": CARD_EXTRACTOR_VERSION,
        }

        async with async_session_factory() as session:
            repo = (
                await session.execute(select(Repository).where(Repository.path == path_obj.as_posix()))
            ).scalar_one_or_none()
            if not repo:
                logger.error(f"build_file_cards: repo not indexed: {path_obj}")
                return stats
            repo_id = repo.id

            files = (await session.execute(select(File).where(File.repository_id == repo_id))).scalars().all()
            file_rows = [
                (f.id, f.path)
                for f in files
                if not should_exclude_from_retrieval(f.path)
                and (f.file_type or "") != "asset"
                and (only_paths is None or f.path in only_paths)
            ]
            file_ids = [fid for fid, _ in file_rows]

            # Batch-load symbols and existing file cards to avoid N+1 queries
            syms = (await session.execute(select(Symbol).where(Symbol.file_id.in_(file_ids)))).scalars().all()
            cards = (await session.execute(select(FileCard).where(FileCard.file_id.in_(file_ids)))).scalars().all()

        symbols_by_file_id: Dict[int, List[Symbol]] = {}
        for s in syms:
            symbols_by_file_id.setdefault(s.file_id, []).append(s)

        existing_cards_by_file_id: Dict[int, FileCard] = {}
        for c in cards:
            existing_cards_by_file_id[c.file_id] = c

        for file_id, rel_path in file_rows:
            stats["eligible"] += 1
            symbols_by_kind: Dict[str, list] = {}
            for s in symbols_by_file_id.get(file_id, []):
                symbols_by_kind.setdefault(s.kind or "unknown", []).append(s.name)

            card = build_card(rel_path, symbols_by_kind, fmt=card_fmt)

            existing = existing_cards_by_file_id.get(file_id)
            if (
                existing is not None
                and existing.card_hash == card.card_hash
                and existing.embedding_id is not None
                and existing.embedding_provider == card_provider.provider
            ):
                stats["skipped"] += 1
                continue

            try:
                vector = await card_provider.embed_card(card.card_text)
            except Exception as exc:
                logger.warning(f"build_file_cards: embed failed for {rel_path}: {exc}")
                vector = []
            # Single failure count for both the exception and empty-vector paths.
            if not vector:
                stats["embed_failures"] += 1
                continue

            try:
                async with async_session_factory() as session:
                    async with session.begin():
                        await assert_current_db_fence(session)
                        if existing is not None:
                            if existing.embedding_id is not None:
                                await session.execute(delete(Embedding).where(Embedding.id == existing.embedding_id))
                            await session.execute(delete(FileCard).where(FileCard.id == existing.id))
                            await session.flush()
                        emb = build_embedding_record(
                            entity_type="file_card",
                            entity_id=0,
                            vector=vector,
                            source_text=card.card_text,
                            config=card_provider.config,
                        )
                        session.add(emb)
                        await session.flush()
                        fc = FileCard(
                            repository_id=repo_id,
                            file_id=file_id,
                            path=rel_path,
                            surface=card.surface,
                            file_role=card.file_role,
                            card_text=card.card_text,
                            card_text_format=card.card_text_format,
                            card_hash=card.card_hash,
                            embedding_provider=card_provider.provider,
                            embedding_model=card_provider.model,
                            embedding_dim=card_provider.dimension,
                            embedding_id=emb.id,
                        )
                        session.add(fc)
                        await session.flush()
                        emb.entity_id = fc.id
            except Exception as exc:
                logger.warning(f"build_file_cards: write failed for {rel_path}: {exc}")
                stats.setdefault("write_failures", 0)
                stats["write_failures"] += 1
                continue
            if existing is not None:
                stats["updated"] += 1
            else:
                stats["created"] += 1

        eligible = stats["eligible"] or 1
        stats["coverage_pct"] = round((stats["created"] + stats["updated"] + stats["skipped"]) / eligible * 100, 1)
        logger.info(f"build_file_cards: {stats}")
        return stats

    async def index_file(self, repo_id: int, repo_path: Path, filepath: Path) -> str:
        """Parse single file, check if changed, chunk, generate embeddings, and save to Postgres."""
        rel_path = filepath.relative_to(repo_path).as_posix()
        if should_exclude_from_retrieval(rel_path):
            return "excluded"
        ext = filepath.suffix.lower()
        file_type = classify_file(filepath, repo_path)

        try:
            if filepath.stat().st_size > 10 * 1024 * 1024:  # 10MB limit
                logger.warning(f"Skipping large file: {filepath}")
                return "skipped_large"
            content = await asyncio.to_thread(_read_text_file, filepath)
        except Exception as e:
            logger.error(f"Failed to read file {filepath}: {e}")
            if isinstance(e, ValueError):  # Binary files are intentional exclusions.
                return "excluded"
            raise

        file_hash = calculate_file_hash(content)
        if self._source_content_hashes is not None and self._source_content_hashes.get(rel_path) != file_hash:
            raise ValueError("Repository file changed after source snapshot")
        knowledge_status = classify_knowledge_status(rel_path, content)
        obsolete_chunk_ids: list[int] = []

        async with async_session_factory() as session:
            async with session.begin():
                await assert_current_db_fence(session)
                # Check if file has already been indexed and hash matches.
                # The unique index on (repository_id, path) guarantees at most
                # one row; order defensively so a pre-migration duplicate still
                # resolves to the latest instead of raising.
                stmt = (select(File).where(File.repository_id == repo_id, File.path == rel_path)
                        .order_by(File.id.desc()))
                res = await session.execute(stmt)
                existing_file = res.scalars().first()

                existing_chunks = 0
                if existing_file:
                    existing_chunks = (await session.execute(select(func.count(FileChunk.id))
                                       .where(FileChunk.file_id == existing_file.id))).scalar() or 0
                if existing_file and existing_file.hash == file_hash and (existing_chunks or not content.strip()):
                    # File has not changed, skip indexing
                    self.progress["chunks"]["discovered"] += existing_chunks
                    self.progress["chunks"]["skipped"] += existing_chunks
                    return "unchanged"

            # Perform summaries and embeddings generation outside the database transaction
            # (prevents connection lock starvation)
            symbols = extract_symbols_from_content(content, ext)
            chunks = chunk_file_content(content, symbols)
            self.progress["chunks"]["discovered"] += len(chunks)
            embedding_provider = self.router.embedding("passage")
            embedding_char_limit = _infer_embedding_text_limit(embedding_provider)
            late_provider = self._late_provider
            remote_late_write = (
                settings.LATE_INTERACTION_ENABLED
                and settings.LATE_INTERACTION_DUAL_WRITE_ENABLED
                and settings.LATE_INTERACTION_REMOTE_ENABLED
                and not self._late_dual_write_circuit_open
            )
            if (
                settings.LATE_INTERACTION_ENABLED
                and settings.LATE_INTERACTION_DUAL_WRITE_ENABLED
                and not settings.LATE_INTERACTION_REMOTE_ENABLED
                and not self._late_dual_write_circuit_open
                and late_provider is None
            ):
                # Additive/fail-open by design: the dense NVIDIA vector remains
                # the chunk's authoritative embedding even when this sidecar is
                # unavailable.
                from brain.late_interaction.provider import LfmColbertProvider

                late_provider = LfmColbertProvider(
                    timeout_s=settings.LATE_INTERACTION_DUAL_WRITE_TIMEOUT_S
                )
                self._late_provider = late_provider
            # v6 P1: structural symbols (routes/CLI/entrypoints) are persisted for the
            # symbol channel but deliberately NOT fed to chunk_file_content above, so
            # chunk/embedding boundaries stay identical to the frozen v5 index.
            symbols = symbols + extract_structural_symbols(content, ext, rel_path)

            # Generate file summary using LLM (if it is source code/doc/config)
            file_summary = ""
            if file_type in {"source_code", "documentation", "config", "api_spec"}:
                if os.environ.get("FAST_INDEX") == "true":
                    file_summary = f"File {rel_path} with {len(content.splitlines())} lines."
                else:
                    try:
                        async with self._provider_slots:
                            file_summary = await self.router.summarizer().summarize(content[:4000])
                    except Exception as e:
                        logger.warning(f"Failed to generate summary for {rel_path}: {e}")
                        self._failure("summary", e, rel_path)
                        file_summary = f"File {rel_path} with {len(content.splitlines())} lines."
            file_summary = annotate_knowledge_summary(file_summary, knowledge_status)

            # Pre-generate chunk summaries and embeddings outside any DB transaction
            # (long-running LLM calls must not hold a connection open).
            prepared_chunks: list[dict] = []
            vectors = await embed_chunks(embedding_provider, chunks, self._provider_slots,
                                        lambda phase, exc, idx: self._failure(phase, exc, rel_path, idx))
            for idx, chunk in enumerate(chunks):
                chunk_summary = ""
                # Per-chunk LLM summaries are off by default (2026-07-26). They cost
                # one LLM round-trip per chunk — about half of total indexing time —
                # and nothing consumes them: ranking scores embeddings of the chunk
                # *content* (brain/search/code_search.py), and /ask injects
                # c["content"], never c["summary"]. The only consumer is the search
                # payload itself, where they duplicate the content they sit next to
                # and inflate every response. Set BRAIN_CHUNK_SUMMARIES=1 to restore.
                #
                # NOTE: this is deliberately NOT keyed off FAST_INDEX, which also
                # replaces the *file* summary with a stub. File summaries are what the
                # lexical retrieval channel matches against (File.summary ILIKE), and
                # that channel is already the weak one — degrading it would trade a
                # measured 15% Hit@3 for indexing speed.
                if os.environ.get("FAST_INDEX") == "true" or os.environ.get("BRAIN_CHUNK_SUMMARIES", "0") != "1":
                    chunk_summary = (
                        f"Code chunk {idx} from line {chunk['start_line']} to {chunk['end_line']} in {rel_path}"
                    )
                else:
                    try:
                        async with self._provider_slots:
                            chunk_summary = await self.router.summarizer().summarize(chunk["content"])
                    except Exception:
                        chunk_summary = f"Code chunk {idx} in {rel_path}"
                chunk_summary = annotate_knowledge_summary(
                    chunk_summary,
                    knowledge_status,
                )

                embedding_vector = vectors[idx]
                if embedding_char_limit is not None and len(chunk["content"]) > embedding_char_limit:
                    logger.warning("Embedding input truncated for {} chunk {}", rel_path, idx)

                late_embedding = None
                if late_provider is not None:
                    try:
                        late_embedding = await late_provider.embed(chunk["content"], is_query=False)
                        self._late_dual_write_failures = 0
                    except Exception as exc:
                        from brain.late_interaction.metrics import increment

                        increment("dual_write_failures")
                        await self._record_late_dual_write_failure()
                        if self._late_dual_write_circuit_open:
                            late_provider = None
                        logger.warning(
                            "Late-interaction dual-write failed open for {} chunk {}: {}",
                            rel_path,
                            idx,
                            str(exc).strip() or type(exc).__name__,
                        )

                prepared_chunks.append(
                    {
                        **chunk,
                        "summary": chunk_summary,
                        "embedding_vector": embedding_vector,
                        "late_embedding": late_embedding,
                    }
                )

            # Save symbols, chunks, and embeddings in a short-lived DB transaction
            remote_documents: list[dict[str, Any]] = []
            embedded = 0
            async with async_session_factory() as session:
                async with session.begin():
                    await assert_current_db_fence(session)
                    await check_job_lease()
                    # Delete-all (not delete-one): with the unique index there can
                    # only be one row, but a pre-migration duplicate must not
                    # raise MultipleResultsFound and fail the whole run.
                    existing_rows = (await session.scalars(select(File).where(
                        File.repository_id == repo_id, File.path == rel_path))).all()
                    for existing in existing_rows:
                        obsolete_chunk_ids.extend((await session.scalars(select(FileChunk.id)
                                                  .where(FileChunk.file_id == existing.id))).all())
                        await session.delete(existing)
                    if existing_rows:
                        await session.flush()
                    file_obj = File(repository_id=repo_id, path=rel_path, language=ext[1:] if ext else "unknown",
                                    file_type=file_type, hash=file_hash, summary=file_summary,
                                    size_bytes=len(content.encode("utf-8", errors="ignore")),
                                    last_indexed_at=datetime.now(timezone.utc))
                    session.add(file_obj)
                    await session.flush()

                    for sym in symbols:
                        sym_obj = Symbol(
                            file_id=file_obj.id,
                            name=sym["name"],
                            kind=sym["kind"],
                            signature=sym["signature"],
                            start_line=sym["start_line"],
                            end_line=sym["end_line"],
                            summary=f"Code symbol {sym['name']} ({sym['kind']}) in {rel_path}",
                        )
                        session.add(sym_obj)

                    for prepared in prepared_chunks:
                        embedding_obj = None
                        if prepared["embedding_vector"]:
                            try:
                                embedding_obj = build_embedding_record(
                                    entity_type="file_chunk",
                                    entity_id=0,
                                    vector=prepared["embedding_vector"],
                                    source_text=prepared["content"],
                                )
                            except Exception as e:
                                logger.error(f"Invalid embedding for chunk: {e}")
                                self._failure("embedding_validation", e, rel_path, prepared["chunk_index"])
                            if embedding_obj is not None:
                                session.add(embedding_obj)
                                await session.flush()

                        chunk_obj = FileChunk(
                            file_id=file_obj.id,
                            chunk_index=prepared["chunk_index"],
                            content=prepared["content"],
                            summary=prepared["summary"],
                            start_line=prepared["start_line"],
                            end_line=prepared["end_line"],
                            embedding_id=embedding_obj.id if embedding_obj else None,
                        )
                        session.add(chunk_obj)
                        await session.flush()

                        if embedding_obj:
                            embedded += 1
                            embedding_obj.entity_id = chunk_obj.id
                        if remote_late_write:
                            source_text = prepared["content"]
                            remote_documents.append(
                                {
                                    "chunk_id": chunk_obj.id,
                                    "content_hash": hashlib.sha256(
                                        source_text.encode(
                                            "utf-8",
                                            errors="replace",
                                        )
                                    ).hexdigest(),
                                    "text": source_text,
                                }
                            )
                        if prepared["late_embedding"] is not None and late_provider is not None:
                            try:
                                from brain.late_interaction.metrics import increment
                                from brain.late_interaction.store import upsert_late_interaction_embedding

                                # A savepoint is required for real fail-open
                                # semantics: a PostgreSQL statement error aborts
                                # the whole transaction until rollback.
                                async with session.begin_nested():
                                    await upsert_late_interaction_embedding(
                                        session,
                                        repository_id=repo_id,
                                        chunk_id=chunk_obj.id,
                                        source_text=prepared["content"],
                                        encoded=prepared["late_embedding"],
                                        provider=late_provider,
                                    )
                                    await session.flush()
                                increment("dual_write_successes")
                                self._late_dual_write_failures = 0
                            except Exception as exc:
                                from brain.late_interaction.metrics import increment

                                increment("dual_write_failures")
                                await self._record_late_dual_write_failure()
                                if self._late_dual_write_circuit_open:
                                    late_provider = None
                                logger.warning(
                                    "Late-interaction persistence failed open for {} chunk {}: {}",
                                    rel_path,
                                    prepared["chunk_index"],
                                    str(exc).strip() or type(exc).__name__,
                                )

            self.progress["chunks"]["processed"] += embedded
            self.progress["chunks"]["failed"] += len(prepared_chunks) - embedded

            if remote_late_write:
                await self._sync_remote_late_documents(
                    repository_id=repo_id,
                    path=rel_path,
                    documents=remote_documents,
                    obsolete_chunk_ids=obsolete_chunk_ids,
                )

            # Populate Neo4j nodes and relationships
            graph_ok = True
            try:
                from brain.graph.schema import NodeType, RelationshipType

                await self.graph_client.create_node(
                    node_type=NodeType.FILE.value,
                    name=rel_path,
                    properties={
                        "path": rel_path,
                        "language": ext[1:] if ext else "unknown",
                        "file_type": file_type,
                        "repository_id": repo_id,
                        "repository_name": repo_path.name,
                        "size_bytes": len(content.encode("utf-8", errors="ignore")),
                    },
                )

                for sym in symbols:
                    await self.graph_client.create_node(
                        node_type=NodeType.SYMBOL.value,
                        name=sym["name"],
                        properties={
                            "kind": sym["kind"],
                            "signature": sym["signature"],
                            "start_line": sym["start_line"],
                            "end_line": sym["end_line"],
                        },
                    )
                    await self.graph_client.create_relationship(
                        from_node_type=NodeType.FILE.value,
                        from_name=rel_path,
                        to_node_type=NodeType.SYMBOL.value,
                        to_name=sym["name"],
                        rel_type=RelationshipType.CONTAINS.value,
                    )
            except Exception as ge:
                graph_ok = False
                logger.error(f"Failed to update Neo4j for file {rel_path}: {ge}")
                self.progress["graph"]["failed"] += 1
                self._failure("graph_file", ge, rel_path)

        logger.info(f"FileIndexer: Indexed file: {rel_path} with {len(symbols)} symbols and {len(chunks)} chunks.")
        return "indexed" if graph_ok else "indexed_graph_failed"

    async def _post_process_graph(self, repo_name: str, repo_path: Path, scanned_files: List[Path]) -> None:
        """Post-process Neo4j graph to add Modules, Features, IMPORTS, BELONGS_TO, and TESTED_BY."""
        import fnmatch
        from brain.graph.schema import NodeType, RelationshipType

        logger.info("Neo4j: Starting post-processing to enrich graph relationships...")

        # 1. Create Repository node
        try:
            await self.graph_client.create_node(
                node_type=NodeType.REPOSITORY.value, name=repo_name, properties={"path": repo_path.as_posix()}
            )
        except Exception as e:
            logger.warning(f"Neo4j: Failed to create Repository node: {e}")
            self.progress["graph"]["failed"] += 1
            self._failure("graph_repository", e)

        # 2. Parse feature_map.yaml if it exists
        feature_map = {}
        for feature_map_filename in ["rules/feature_map.yaml", "feature_map.yaml"]:
            f_map_path = repo_path / feature_map_filename
            if f_map_path.exists():
                import yaml

                try:
                    _map_text = await asyncio.to_thread(_read_text_file, f_map_path, "strict")
                    data = yaml.safe_load(_map_text) or {}
                    feature_map = data.get("features", data)
                    logger.info(f"Neo4j: Loaded feature map from {feature_map_filename}")
                    break
                except Exception as e:
                    logger.warning(f"Neo4j: Failed to parse feature map {feature_map_filename}: {e}")

        # Ensure Feature nodes are created
        if feature_map:
            for feat_name in feature_map.keys():
                try:
                    await self.graph_client.create_node(node_type=NodeType.FEATURE.value, name=feat_name, properties={})
                except Exception as exc:
                    self.progress["graph"]["failed"] += 1
                    self._failure("graph_feature", exc)

        from brain.retrieval.graph_extractors import (
            extract_affects,
            extract_calls,
            extract_import_file_edges,
            extract_route_edges,
            extract_script_domain_edges,
            extract_tab_core_links,
            extract_tested_by,
            extract_uses,
        )

        all_rel_paths = [fp.relative_to(repo_path).as_posix() for fp in scanned_files]
        by_stem = defaultdict(list)
        for fp, rel in zip(scanned_files, all_rel_paths):
            by_stem[fp.stem].append(rel)
        self.progress["graph"]["discovered"] = len(scanned_files)

        # Track created modules to avoid duplicate queries
        created_modules = set()

        for filepath in scanned_files:
            try:
                rel_path = filepath.relative_to(repo_path).as_posix()
                ext = filepath.suffix.lower()
                content = ""

                # A. Create Modules and BELONGS_TO/CONTAINS hierarchy
                parts = Path(rel_path).parent.parts
                current_module = None

                for idx, part in enumerate(parts):
                    # We define module name/id by its relative subpath, e.g. "apps/api"
                    module_path = "/".join(parts[: idx + 1])
                    if module_path not in created_modules:
                        await self.graph_client.create_node(
                            node_type=NodeType.MODULE.value, name=module_path, properties={"folder_name": part}
                        )
                        created_modules.add(module_path)

                        # Link parent module or repository
                        if idx == 0:
                            # Link root module to repository
                            await self.graph_client.create_relationship(
                                from_node_type=NodeType.REPOSITORY.value,
                                from_name=repo_name,
                                to_node_type=NodeType.MODULE.value,
                                to_name=module_path,
                                rel_type=RelationshipType.CONTAINS.value,
                            )
                        else:
                            parent_path = "/".join(parts[:idx])
                            await self.graph_client.create_relationship(
                                from_node_type=NodeType.MODULE.value,
                                from_name=parent_path,
                                to_node_type=NodeType.MODULE.value,
                                to_name=module_path,
                                rel_type=RelationshipType.CONTAINS.value,
                            )
                    current_module = module_path

                # Link file to its parent module
                if current_module:
                    await self.graph_client.create_relationship(
                        from_node_type=NodeType.MODULE.value,
                        from_name=current_module,
                        to_node_type=NodeType.FILE.value,
                        to_name=rel_path,
                        rel_type=RelationshipType.CONTAINS.value,
                    )
                    await self.graph_client.create_relationship(
                        from_node_type=NodeType.FILE.value,
                        from_name=rel_path,
                        to_node_type=NodeType.MODULE.value,
                        to_name=current_module,
                        rel_type=RelationshipType.BELONGS_TO.value,
                    )
                else:
                    # Link top-level files directly to Repository
                    await self.graph_client.create_relationship(
                        from_node_type=NodeType.REPOSITORY.value,
                        from_name=repo_name,
                        to_node_type=NodeType.FILE.value,
                        to_name=rel_path,
                        rel_type=RelationshipType.CONTAINS.value,
                    )

                # B. Mapped Features
                for feat_name, patterns in feature_map.items():
                    for pattern in patterns:
                        if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(rel_path, f"*{pattern}*"):
                            await self.graph_client.create_relationship(
                                from_node_type=NodeType.FILE.value,
                                from_name=rel_path,
                                to_node_type=NodeType.FEATURE.value,
                                to_name=feat_name,
                                rel_type=RelationshipType.BELONGS_TO.value,
                            )
                            await self.graph_client.create_relationship(
                                from_node_type=NodeType.FEATURE.value,
                                from_name=feat_name,
                                to_node_type=NodeType.FILE.value,
                                to_name=rel_path,
                                rel_type=RelationshipType.RELATED_TO.value,
                            )
                            break

                # C. Extract IMPORTS from Python, JS, TS
                if ext in [".py", ".js", ".ts", ".tsx"]:
                    try:
                        content = await asyncio.to_thread(_read_text_file, filepath)

                        imported_modules = []
                        if ext == ".py":
                            matches1 = re.findall(r"^\s*import\s+([a-zA-Z0-9_\.]+)", content, re.MULTILINE)
                            matches2 = re.findall(r"^\s*from\s+([a-zA-Z0-9_\.]+)\s+import", content, re.MULTILINE)
                            imported_modules = matches1 + matches2
                        else:
                            matches1 = re.findall(r'from\s+[\'"]([^\'"]+)[\'"]', content)
                            matches2 = re.findall(r'require\(\s*[\'"]([^\'"]+)[\'"]\s*\)', content)
                            imported_modules = matches1 + matches2

                        for imp in imported_modules:
                            imp_cleaned = imp.replace(".", "/").strip("/")
                            imp_name = imp_cleaned.split("/")[-1]

                            for cand_rel in by_stem.get(imp_name, []):
                                if imp_cleaned in cand_rel or "/" not in imp_cleaned:
                                    await self.graph_client.create_relationship(
                                        from_node_type=NodeType.FILE.value,
                                        from_name=rel_path,
                                        to_node_type=NodeType.FILE.value,
                                        to_name=cand_rel,
                                        rel_type=RelationshipType.IMPORTS.value,
                                    )
                                    break
                    except Exception as ie:
                        logger.warning(f"Neo4j: Failed to parse imports from {rel_path}: {ie}")
                        self.progress["graph"]["failed"] += 1
                        self._failure("graph_import", ie, rel_path)

                # D. TESTED_BY + CALLS/USES/AFFECTS enrichment
                file_content = ""
                if ext in {".py", ".js", ".ts", ".tsx"}:
                    file_content = content  # already read in section C
                # Re-read if C didn't set it (covers .py whose section-C read
                # failed, plus the languages C doesn't handle).
                if not file_content and ext in {".py", ".kt", ".kts", ".go", ".rs", ".java"}:
                    try:
                        file_content = await asyncio.to_thread(_read_text_file, filepath)
                    except Exception:
                        file_content = ""

                for edge in extract_tested_by(rel_path, all_rel_paths):
                    await self.graph_client.create_relationship(
                        from_node_type=NodeType.FILE.value,
                        from_name=edge.from_path,
                        to_node_type=NodeType.FILE.value,
                        to_name=edge.to_ref,
                        rel_type=RelationshipType.TESTED_BY.value,
                        properties={
                            "confidence": edge.confidence,
                            "provenance": edge.provenance,
                            "inferred": edge.inferred,
                        },
                    )

                for edge in extract_tab_core_links(rel_path, all_rel_paths):
                    await self.graph_client.create_relationship(
                        from_node_type=NodeType.FILE.value,
                        from_name=edge.from_path,
                        to_node_type=NodeType.FILE.value,
                        to_name=edge.to_ref,
                        rel_type=RelationshipType.RELATED_TO.value,
                        properties={
                            "confidence": edge.confidence,
                            "provenance": edge.provenance,
                            "inferred": edge.inferred,
                        },
                    )

                for edge in extract_calls(file_content, rel_path):
                    await self.graph_client.create_relationship(
                        from_node_type=NodeType.FILE.value,
                        from_name=edge.from_path,
                        to_node_type=NodeType.SYMBOL.value,
                        to_name=edge.to_ref,
                        rel_type=RelationshipType.CALLS.value,
                        properties={
                            "confidence": edge.confidence,
                            "provenance": edge.provenance,
                            "inferred": edge.inferred,
                        },
                    )

                for edge in extract_uses(file_content, rel_path, ext):
                    await self.graph_client.create_relationship(
                        from_node_type=NodeType.FILE.value,
                        from_name=edge.from_path,
                        to_node_type=NodeType.SYMBOL.value,
                        to_name=edge.to_ref,
                        rel_type=RelationshipType.USES.value,
                        properties={
                            "confidence": edge.confidence,
                            "provenance": edge.provenance,
                            "inferred": edge.inferred,
                        },
                    )

                for edge in extract_affects(rel_path, feature_map):
                    await self.graph_client.create_relationship(
                        from_node_type=NodeType.FILE.value,
                        from_name=edge.from_path,
                        to_node_type=NodeType.FEATURE.value,
                        to_name=edge.to_ref,
                        rel_type=RelationshipType.AFFECTS.value,
                        properties={
                            "confidence": edge.confidence,
                            "provenance": edge.provenance,
                            "inferred": edge.inferred,
                        },
                    )

                # v6 P1 structural edges: ROUTE->endpoint, TEST->SOURCE (imports), SCRIPT->DOMAIN
                for edge in extract_route_edges(file_content, rel_path, ext):
                    await self.graph_client.create_relationship(
                        from_node_type=NodeType.FILE.value,
                        from_name=edge.from_path,
                        to_node_type=NodeType.API_ENDPOINT.value,
                        to_name=edge.to_ref,
                        rel_type=RelationshipType.ROUTE.value,
                        properties={"confidence": edge.confidence, "provenance": edge.provenance},
                    )
                for edge in extract_import_file_edges(file_content, rel_path, ext, all_rel_paths):
                    if edge.rel_type != "TESTS":
                        continue
                    await self.graph_client.create_relationship(
                        from_node_type=NodeType.FILE.value,
                        from_name=edge.from_path,
                        to_node_type=NodeType.FILE.value,
                        to_name=edge.to_ref,
                        rel_type=RelationshipType.TESTS.value,
                        properties={"confidence": edge.confidence, "provenance": edge.provenance},
                    )
                for edge in extract_script_domain_edges(rel_path, all_rel_paths):
                    await self.graph_client.create_relationship(
                        from_node_type=NodeType.FILE.value,
                        from_name=edge.from_path,
                        to_node_type=NodeType.FILE.value,
                        to_name=edge.to_ref,
                        rel_type=RelationshipType.SCRIPT_DOMAIN.value,
                        properties={"confidence": edge.confidence, "provenance": edge.provenance},
                    )
                await self.graph_client.flush()
            except Exception as e:
                logger.error(f"Neo4j: Error post-processing file {filepath}: {e}")
                self.progress["graph"]["failed"] += 1
                self._failure("graph_post_process", e, rel_path)
                self.graph_client.rows.clear()
            else:
                self.progress["graph"]["processed"] += 1
            await self._publish_progress()
