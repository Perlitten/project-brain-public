"""Resumable Postgres-to-GPU late-interaction index synchronization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select

from brain.config.settings import settings
from brain.database.models import File, FileChunk
from brain.database.session import async_session_factory
from brain.late_interaction.client import (
    LateInteractionDocument,
    get_late_interaction_client,
)


@dataclass
class RemoteLateInteractionSyncResult:
    repository_id: int
    dry_run: bool
    examined: int = 0
    accepted: int = 0
    unchanged: int = 0
    deleted: int = 0
    skipped_empty: int = 0
    failed_batches: int = 0
    model_revision: str = ""
    index_revision: str = ""
    source_identity_digest: str = ""
    remote_identity_digest: str = ""
    verified_documents: int = 0
    verification_mode: str = ""
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.failed_batches == 0

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "passed": self.passed}


@dataclass(frozen=True)
class _RemoteInventorySnapshot:
    identities: dict[int, tuple[str, str]]
    model_revision: str = ""
    index_revision: str = ""
    status: str = "ready"
    reason: str = ""


async def _database_batch(
    repository_id: int,
    *,
    after_chunk_id: int,
    limit: int,
) -> list[tuple[int, str, str]]:
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(FileChunk.id, File.path, FileChunk.content)
                .join(File, FileChunk.file_id == File.id)
                .where(
                    File.repository_id == repository_id,
                    FileChunk.id > after_chunk_id,
                )
                .order_by(FileChunk.id)
                .limit(limit)
            )
        ).all()
    return [(int(chunk_id), str(path), str(content or "")) for chunk_id, path, content in rows]


async def _database_documents_by_ids(
    repository_id: int,
    chunk_ids: list[int],
) -> list[tuple[int, str, str]]:
    if not chunk_ids:
        return []
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(FileChunk.id, File.path, FileChunk.content)
                .join(File, FileChunk.file_id == File.id)
                .where(
                    File.repository_id == repository_id,
                    FileChunk.id.in_(chunk_ids),
                )
                .order_by(FileChunk.id)
            )
        ).all()
    return [(int(chunk_id), str(path), str(content or "")) for chunk_id, path, content in rows]


async def _database_inventory_snapshot(
    repository_id: int,
    *,
    page_size: int,
) -> dict[int, tuple[str, str]]:
    identities: dict[int, tuple[str, str]] = {}
    after_chunk_id = 0
    while True:
        rows = await _database_batch(
            repository_id,
            after_chunk_id=after_chunk_id,
            limit=page_size,
        )
        if not rows:
            return identities
        for chunk_id, path, content in rows:
            if not content:
                continue
            identities[chunk_id] = (
                path,
                hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest(),
            )
        after_chunk_id = rows[-1][0]


def _identity_digest(identities: dict[int, tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for chunk_id, (path, content_hash) in sorted(identities.items()):
        digest.update(
            json.dumps(
                [chunk_id, path, content_hash.lower()],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


async def _remote_inventory_snapshot(
    client,
    repository_id: int,
) -> _RemoteInventorySnapshot:
    identities: dict[int, tuple[str, str]] = {}
    after_chunk_id = 0
    model_revision = ""
    stable_index_revision: str | None = None

    while True:
        page = await client.list_documents(
            repository_id,
            after_chunk_id=after_chunk_id,
            limit=1000,
        )
        model_revision = page.model_revision or model_revision
        if page.status != "ready":
            return _RemoteInventorySnapshot(
                identities={},
                model_revision=model_revision,
                index_revision=page.index_revision or stable_index_revision or "",
                status=page.status,
                reason=page.reason or page.status,
            )
        if stable_index_revision is None:
            stable_index_revision = page.index_revision
        elif page.index_revision != stable_index_revision:
            return _RemoteInventorySnapshot(
                identities={},
                model_revision=model_revision,
                index_revision=page.index_revision,
                status="revision_mismatch",
                reason="remote_inventory_revision_changed",
            )
        identities.update({entry.chunk_id: (entry.path, entry.content_hash.lower()) for entry in page.entries})
        if page.next_after_chunk_id is None:
            return _RemoteInventorySnapshot(
                identities=identities,
                model_revision=model_revision,
                index_revision=stable_index_revision or "",
            )
        if page.next_after_chunk_id <= after_chunk_id:
            return _RemoteInventorySnapshot(
                identities={},
                model_revision=model_revision,
                index_revision=stable_index_revision or "",
                status="failed_open",
                reason="remote_inventory_cursor_stalled",
            )
        after_chunk_id = page.next_after_chunk_id


def _apply_inventory_snapshot(
    result: RemoteLateInteractionSyncResult,
    snapshot: _RemoteInventorySnapshot,
) -> bool:
    result.model_revision = snapshot.model_revision or result.model_revision
    result.index_revision = snapshot.index_revision or result.index_revision
    if snapshot.status == "ready":
        return True
    result.failed_batches += 1
    result.reason = snapshot.reason or snapshot.status
    return False


def _finalization_matches_exact_inventory(
    finalized,
    *,
    repository_id: int,
    model_revision: str,
    index_revision: str,
    identity_digest: str,
    document_count: int,
) -> bool:
    """Accept only a finalization response that matches the proven corpus.

    A maintenance sync can legitimately replace the configured approved
    identity.  The shared client still applies that stale read-traffic approval
    to the finalize response and reports ``inventory_mismatch`` even though the
    raw service finalized the newly proven identity.  Treat only that narrow
    stale-approval result as successful; every returned corpus field must match
    the exact Postgres/remote inventory comparison completed immediately above.
    """
    if finalized.status not in {"ready", "inventory_mismatch"}:
        return False
    if (
        finalized.status == "inventory_mismatch"
        and finalized.reason
        not in {"identity_digest_mismatch", "document_count_mismatch"}
    ):
        return False
    return bool(
        finalized.repository_id == repository_id
        and finalized.model_revision == model_revision
        and finalized.index_revision == index_revision
        and finalized.identity_digest.lower() == identity_digest.lower()
        and finalized.document_count == document_count
    )


def _prune_has_active_local_traffic() -> bool:
    return bool(
        settings.LATE_INTERACTION_ENABLED
        or settings.LATE_INTERACTION_DUAL_WRITE_ENABLED
        or settings.LATE_INTERACTION_SHADOW_ENABLED
        or settings.LATE_INTERACTION_RERANK_ENABLED
        or settings.LATE_INTERACTION_CANARY_PERCENT > 0
    )


async def _upsert_document_batches(
    *,
    client,
    repository_id: int,
    documents: list[LateInteractionDocument],
    batch_size: int,
    result: RemoteLateInteractionSyncResult,
    remote_identities: dict[int, tuple[str, str]],
    prefiltered_unchanged_ids: set[int] | None = None,
) -> bool:
    for offset in range(0, len(documents), batch_size):
        mutation_batch = documents[offset : offset + batch_size]
        mutation = await client.upsert_documents(repository_id, mutation_batch)
        result.model_revision = mutation.model_revision or result.model_revision
        result.index_revision = mutation.index_revision or result.index_revision
        if mutation.status not in {"updated", "unchanged"}:
            result.failed_batches += 1
            result.reason = mutation.reason or mutation.status
            return False
        if prefiltered_unchanged_ids is not None:
            repaired_prefiltered = {
                document.chunk_id for document in mutation_batch if document.chunk_id in prefiltered_unchanged_ids
            }
            result.unchanged -= len(repaired_prefiltered)
            prefiltered_unchanged_ids.difference_update(repaired_prefiltered)
        result.accepted += mutation.accepted
        result.unchanged += mutation.unchanged
        remote_identities.update(
            {
                document.chunk_id: (
                    document.path,
                    document.content_hash,
                )
                for document in mutation_batch
            }
        )
    return True


async def sync_remote_late_interaction(
    repository_id: int,
    *,
    batch_size: int | None = None,
    max_chunks: int | None = None,
    dry_run: bool = True,
    prune: bool = False,
    client=None,
) -> RemoteLateInteractionSyncResult:
    """Synchronize authoritative chunks into the co-located matrix store.

    Re-running is cheap and resumable: the service compares SHA-256 hashes
    before encoding and reports unchanged documents. Pruning is permitted only
    after an unbounded full scan, preventing a partial sync from deleting valid
    matrices. Bounded writes prove only their scanned subset; unbounded writes
    require exact equality between the complete source and remote inventories.
    """
    if repository_id < 1:
        raise ValueError("repository_id must be positive")
    effective_batch_size = batch_size or settings.LATE_INTERACTION_REMOTE_SYNC_BATCH_SIZE
    if not 1 <= effective_batch_size <= settings.LATE_INTERACTION_REMOTE_MAX_DOCUMENTS:
        raise ValueError("batch_size must be between 1 and LATE_INTERACTION_REMOTE_MAX_DOCUMENTS")
    if max_chunks is not None and max_chunks < 1:
        raise ValueError("max_chunks must be positive when provided")
    if prune and (dry_run or max_chunks is not None):
        raise ValueError("prune requires --write and a complete unbounded scan")
    if prune and _prune_has_active_local_traffic():
        raise ValueError("prune requires local dual-write and active late-interaction traffic settings to be disabled")

    result = RemoteLateInteractionSyncResult(
        repository_id=repository_id,
        dry_run=dry_run,
    )
    active_client = client or get_late_interaction_client()
    current_chunk_ids: set[int] = set()
    current_identities: dict[int, tuple[str, str]] = {}
    prefiltered_unchanged_ids: set[int] = set()
    remote_identities: dict[int, tuple[str, str]] = {}
    after_chunk_id = 0

    if not dry_run:
        initial_inventory = await _remote_inventory_snapshot(
            active_client,
            repository_id,
        )
        if not _apply_inventory_snapshot(result, initial_inventory):
            return result
        remote_identities.update(initial_inventory.identities)

    source_page_size = max(
        effective_batch_size,
        settings.LATE_INTERACTION_REMOTE_SOURCE_PAGE_SIZE,
    )
    while True:
        remaining = source_page_size if max_chunks is None else min(source_page_size, max_chunks - result.examined)
        if remaining <= 0:
            break
        rows = await _database_batch(
            repository_id,
            after_chunk_id=after_chunk_id,
            limit=remaining,
        )
        if not rows:
            break
        after_chunk_id = rows[-1][0]
        result.examined += len(rows)
        eligible_rows = [(chunk_id, path, content) for chunk_id, path, content in rows if content]
        result.skipped_empty += len(rows) - len(eligible_rows)
        current_chunk_ids.update(chunk_id for chunk_id, _path, _content in eligible_rows)
        if dry_run:
            continue
        if not eligible_rows:
            continue

        documents = [
            LateInteractionDocument(
                chunk_id=chunk_id,
                path=path,
                content_hash=hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest(),
                text=content,
            )
            for chunk_id, path, content in eligible_rows
        ]
        current_identities.update(
            {
                document.chunk_id: (
                    document.path,
                    document.content_hash,
                )
                for document in documents
            }
        )
        for offset in range(0, len(documents), effective_batch_size):
            mutation_batch = documents[offset : offset + effective_batch_size]
            changed_documents: list[LateInteractionDocument] = []
            unchanged_ids: set[int] = set()
            for document in mutation_batch:
                if remote_identities.get(document.chunk_id) == (
                    document.path,
                    document.content_hash,
                ):
                    unchanged_ids.add(document.chunk_id)
                else:
                    changed_documents.append(document)
            result.unchanged += len(unchanged_ids)
            prefiltered_unchanged_ids.update(unchanged_ids)
            if not changed_documents:
                continue
            if not await _upsert_document_batches(
                client=active_client,
                repository_id=repository_id,
                documents=changed_documents,
                batch_size=effective_batch_size,
                result=result,
                remote_identities=remote_identities,
            ):
                break
        if result.failed_batches:
            break

    if dry_run or not result.passed:
        return result

    final_inventory = await _remote_inventory_snapshot(
        active_client,
        repository_id,
    )
    if not _apply_inventory_snapshot(result, final_inventory):
        return result
    remote_identities = final_inventory.identities

    repair_chunk_ids = sorted(
        chunk_id for chunk_id, identity in current_identities.items() if remote_identities.get(chunk_id) != identity
    )
    for offset in range(0, len(repair_chunk_ids), effective_batch_size):
        candidate_ids = repair_chunk_ids[offset : offset + effective_batch_size]
        rows = await _database_documents_by_ids(repository_id, candidate_ids)
        repair_documents = [
            LateInteractionDocument(
                chunk_id=chunk_id,
                path=path,
                content_hash=hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest(),
                text=content,
            )
            for chunk_id, path, content in rows
            if content
        ]
        if not repair_documents:
            continue
        if not await _upsert_document_batches(
            client=active_client,
            repository_id=repository_id,
            documents=repair_documents,
            batch_size=effective_batch_size,
            result=result,
            remote_identities=remote_identities,
            prefiltered_unchanged_ids=prefiltered_unchanged_ids,
        ):
            return result

    if prune:
        stale_chunk_ids = sorted(
            chunk_id for chunk_id in final_inventory.identities if chunk_id not in current_chunk_ids
        )

        for offset in range(0, len(stale_chunk_ids), 500):
            candidate_ids = stale_chunk_ids[offset : offset + 500]
            live_rows = await _database_documents_by_ids(repository_id, candidate_ids)
            authoritative_live_ids = {chunk_id for chunk_id, _path, content in live_rows if content}
            delete_ids = [chunk_id for chunk_id in candidate_ids if chunk_id not in authoritative_live_ids]
            if not delete_ids:
                continue
            mutation = await active_client.delete_documents(
                repository_id,
                delete_ids,
            )
            result.model_revision = mutation.model_revision or result.model_revision
            result.index_revision = mutation.index_revision or result.index_revision
            if mutation.status not in {"updated", "unchanged"}:
                result.failed_batches += 1
                result.reason = mutation.reason or mutation.status
                return result
            result.deleted += mutation.deleted

    if max_chunks is None:
        authoritative = await _database_inventory_snapshot(
            repository_id,
            page_size=source_page_size,
        )
        verification_mode = "exact"
    else:
        authoritative = dict(current_identities)
        verification_mode = "bounded_subset"
    verified_remote = await _remote_inventory_snapshot(
        active_client,
        repository_id,
    )
    if not _apply_inventory_snapshot(result, verified_remote):
        return result
    if verification_mode == "exact":
        verified_remote_identities = verified_remote.identities
    else:
        verified_remote_identities = {
            chunk_id: verified_remote.identities[chunk_id]
            for chunk_id in authoritative
            if chunk_id in verified_remote.identities
        }
    result.source_identity_digest = _identity_digest(authoritative)
    result.remote_identity_digest = _identity_digest(verified_remote_identities)
    result.verified_documents = len(authoritative)
    result.verification_mode = verification_mode
    if verified_remote_identities != authoritative:
        result.failed_batches += 1
        result.reason = "final_cross_store_inventory_mismatch"
        return result

    # The canonical digest is O(N), so the service deliberately does not
    # recalculate it after every mutation batch. A complete sync finalizes it
    # exactly once after cross-store equality has been proven.
    if verification_mode == "exact":
        finalize_index = getattr(active_client, "finalize_index", None)
        if callable(finalize_index):
            finalized = await finalize_index(repository_id)
            result.model_revision = finalized.model_revision or result.model_revision
            result.index_revision = finalized.index_revision or result.index_revision
            if not _finalization_matches_exact_inventory(
                finalized,
                repository_id=repository_id,
                model_revision=verified_remote.model_revision,
                index_revision=verified_remote.index_revision,
                identity_digest=result.remote_identity_digest,
                document_count=len(authoritative),
            ):
                result.failed_batches += 1
                result.reason = (
                    finalized.reason
                    or (
                        "finalized_identity_digest_mismatch"
                        if finalized.identity_digest != result.remote_identity_digest
                        else finalized.status
                    )
                )
    return result


__all__ = [
    "RemoteLateInteractionSyncResult",
    "sync_remote_late_interaction",
]
