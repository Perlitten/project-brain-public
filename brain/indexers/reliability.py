"""Bounded indexing work, repository exclusion, and embedding attribution."""

import asyncio
import hashlib
import subprocess
from contextlib import asynccontextmanager

from sqlalchemy import text

from brain.config.settings import settings
from brain.database.session import async_engine
from brain.workers.runtime import check_job_lease


def git_source_is_clean(path, files) -> bool:
    """Require indexed Git files to belong to an unchanged committed tree."""
    command = ["git", "-c", f"safe.directory={path}", "-C", str(path)]
    probe = subprocess.run([*command, "rev-parse", "--is-inside-work-tree"], capture_output=True)
    if probe.returncode:
        return not (path / ".git").exists()  # File snapshots have no Git tree.
    status = subprocess.run([*command, "status", "--porcelain", "--untracked-files=all"], capture_output=True)
    tracked = subprocess.run([*command, "ls-files", "-z"], capture_output=True)
    if status.returncode or tracked.returncode or status.stdout:
        return False
    tracked_paths = set(tracked.stdout.decode("utf-8", errors="surrogateescape").split("\0"))
    return all(file.relative_to(path).as_posix() in tracked_paths for file in files)


@asynccontextmanager
async def repository_index_lock(path: str):
    # Session advisory locks survive short transactions, but disappear on a
    # worker crash. One dedicated connection prevents pool reuse of the lock.
    key = int.from_bytes(hashlib.sha256(path.encode()).digest()[:8], "big", signed=True)
    async with async_engine.connect() as connection:
        locked = (await connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})).scalar()
        await connection.commit()
        if not locked:
            raise RuntimeError("Repository is already being indexed or repaired")
        try:
            yield
        finally:
            try:
                await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                await connection.commit()
            except BaseException:
                # Never return a still-locked session to the connection pool.
                await connection.invalidate()
                raise


async def bounded_map(items, operation, concurrency: int) -> None:
    """Create only N tasks, including cancellation of every child on failure."""
    iterator = iter(items)

    async def consume():
        for item in iterator:
            await check_job_lease()
            await operation(item)

    tasks = [asyncio.create_task(consume()) for _ in range(max(1, concurrency))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def embed_chunks(provider, chunks: list[dict], semaphore, record_failure) -> list[list[float]]:
    """Batch with single-chunk fallback; never shift vectors after partial output."""
    vectors: list[list[float]] = [[] for _ in chunks]
    size = min(settings.INDEX_EMBEDDING_BATCH_SIZE, getattr(provider, "MAX_BATCH_SIZE", 64))
    for start in range(0, len(chunks), max(1, size)):
        batch = chunks[start:start + size]
        try:
            async with semaphore:
                result = await provider.embed_batch([chunk["content"] for chunk in batch], input_type="passage")
            if len(result) != len(batch):
                raise ValueError("Embedding batch cardinality mismatch")
        except Exception as exc:
            record_failure("embedding_batch", exc, None)
            result = []
            for chunk in batch:
                try:
                    async with semaphore:
                        vector = await provider.embed(chunk["content"], input_type="passage")
                    if not vector:
                        raise ValueError("Empty embedding")
                except Exception as item_exc:
                    record_failure("embedding", item_exc, chunk["chunk_index"])
                    vector = []
                result.append(vector)
        for offset, vector in enumerate(result):
            if not vector:
                record_failure("embedding", ValueError("Empty embedding"), batch[offset]["chunk_index"])
            vectors[start + offset] = vector
    return vectors
