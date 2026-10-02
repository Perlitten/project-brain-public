"""Repository lookup and index cleanup helpers."""

from pathlib import Path
from typing import Any, Optional, Union, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult

from brain.database.models import Embedding, File, FileCard, FileChunk, Repository
from brain.database.session import async_session_factory
from brain.workers.db_fencing import assert_current_db_fence


async def get_repository_by_path(repo_path: Union[str, Path]) -> Optional[Repository]:
    """Return the Repository row for an absolute repo path, if indexed."""
    raw_path = str(repo_path)
    path_obj = Path(raw_path)
    candidates = {raw_path, raw_path.replace("\\", "/"), path_obj.as_posix()}
    resolved_path = path_obj.resolve()
    candidates.update({resolved_path.as_posix(), str(resolved_path)})
    async with async_session_factory() as session:
        stmt = select(Repository).where(Repository.path.in_(list(candidates)))
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def list_all_repositories() -> list[Repository]:
    """Return all registered Repository rows from PostgreSQL."""
    async with async_session_factory() as session:
        result = await session.execute(select(Repository).order_by(Repository.id))
        return list(result.scalars().all())


async def require_repository_by_path(repo_path: Union[str, Path]) -> Repository:
    """Resolve an indexed repository or fail with an actionable identity error.

    Graph operations are never allowed to silently fall back to an unscoped
    client.  Keeping the lookup in one helper gives API, MCP, CLI and workers the
    same repository-identity contract.
    """
    repository = await get_repository_by_path(repo_path)
    if repository is None:
        raise LookupError(f"Repository is not indexed: {repo_path}")
    return repository


async def clear_repository_index(repo_id: int) -> int:
    """Delete all indexed files, chunks, symbols, and chunk embeddings for a repo."""
    async with async_session_factory() as session:
        async with session.begin():
            await assert_current_db_fence(session)
            chunk_ids_stmt = (
                select(FileChunk.id)
                .join(File, FileChunk.file_id == File.id)
                .where(File.repository_id == repo_id)
            )
            chunk_ids = list((await session.execute(chunk_ids_stmt)).scalars().all())

            if chunk_ids:
                await session.execute(
                    delete(Embedding).where(
                        Embedding.entity_type == "file_chunk",
                        Embedding.entity_id.in_(chunk_ids),
                    )
                )

            # File-card embeddings: FileCard.embedding_id is ON DELETE SET NULL,
            # so the File cascade would orphan these. Delete them explicitly.
            card_ids = list((await session.execute(
                select(FileCard.id)
                .join(File, FileCard.file_id == File.id)
                .where(File.repository_id == repo_id)
            )).scalars().all())
            if card_ids:
                await session.execute(
                    delete(Embedding).where(
                        Embedding.entity_type == "file_card",
                        Embedding.entity_id.in_(card_ids),
                    )
                )

            delete_files = delete(File).where(File.repository_id == repo_id)
            result = await session.execute(delete_files)
            return cast(CursorResult[Any], result).rowcount or 0


async def purge_repository_graph(repository_id: int) -> None:
    """Remove all Neo4j nodes scoped to a repository."""
    from brain.graph.graph_client import GraphClient

    client = GraphClient(repository_id=repository_id)
    await client.purge_repository()
