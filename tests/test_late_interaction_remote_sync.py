from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from brain.config.settings import settings
from brain.late_interaction import remote_sync
from brain.late_interaction.client import (
    LateInteractionIndexEntry,
    LateInteractionIndexPage,
    LateInteractionIndexStatus,
    LateInteractionMutationResult,
)


MODEL_REVISION = "59633c2e31717b3502343ff566bee9fda3261943"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _page(
    *,
    entries: tuple[LateInteractionIndexEntry, ...] = (),
    index_revision: str = "r1",
    next_after_chunk_id: int | None = None,
) -> LateInteractionIndexPage:
    return LateInteractionIndexPage(
        status="ready",
        repository_id=7,
        entries=entries,
        model_revision=MODEL_REVISION,
        index_revision=index_revision,
        next_after_chunk_id=next_after_chunk_id,
    )


def _client(
    *,
    list_documents: AsyncMock,
    upsert_documents: AsyncMock | None = None,
    delete_documents: AsyncMock | None = None,
    finalize_index: AsyncMock | None = None,
) -> SimpleNamespace:
    values = {
        "list_documents": list_documents,
        "upsert_documents": upsert_documents or AsyncMock(),
        "delete_documents": delete_documents or AsyncMock(),
    }
    if finalize_index is not None:
        values["finalize_index"] = finalize_index
    return SimpleNamespace(
        **values,
    )


def _disable_local_traffic(monkeypatch) -> None:
    monkeypatch.setattr(settings, "LATE_INTERACTION_ENABLED", False)
    monkeypatch.setattr(settings, "LATE_INTERACTION_DUAL_WRITE_ENABLED", False)
    monkeypatch.setattr(settings, "LATE_INTERACTION_SHADOW_ENABLED", False)
    monkeypatch.setattr(settings, "LATE_INTERACTION_RERANK_ENABLED", False)
    monkeypatch.setattr(settings, "LATE_INTERACTION_CANARY_PERCENT", 0.0)


@pytest.mark.asyncio
async def test_remote_inventory_rejects_revision_drift_between_pages(monkeypatch):
    database_batch = AsyncMock()
    monkeypatch.setattr(remote_sync, "_database_batch", database_batch)
    list_documents = AsyncMock(
        side_effect=[
            _page(
                entries=(LateInteractionIndexEntry(1, "a.py", _hash("alpha")),),
                index_revision="r1",
                next_after_chunk_id=1,
            ),
            _page(
                entries=(LateInteractionIndexEntry(2, "b.py", _hash("beta")),),
                index_revision="r2",
            ),
        ]
    )
    client = _client(list_documents=list_documents)

    result = await remote_sync.sync_remote_late_interaction(
        7,
        dry_run=False,
        client=client,
    )

    assert result.failed_batches == 1
    assert result.reason == "remote_inventory_revision_changed"
    assert result.index_revision == "r2"
    database_batch.assert_not_awaited()
    client.upsert_documents.assert_not_awaited()
    client.delete_documents.assert_not_awaited()


@pytest.mark.asyncio
async def test_final_reconciliation_repairs_prefiltered_remote_deletion(monkeypatch):
    monkeypatch.setattr(
        remote_sync,
        "_database_batch",
        AsyncMock(
            side_effect=[
                [(1, "a.py", "alpha")],
                [],
                [(1, "a.py", "alpha")],
                [],
            ]
        ),
    )
    database_documents_by_ids = AsyncMock(return_value=[(1, "a.py", "alpha")])
    monkeypatch.setattr(
        remote_sync,
        "_database_documents_by_ids",
        database_documents_by_ids,
    )
    list_documents = AsyncMock(
        side_effect=[
            _page(
                entries=(LateInteractionIndexEntry(1, "a.py", _hash("alpha")),),
                index_revision="r1",
            ),
            _page(index_revision="r2"),
            _page(
                entries=(LateInteractionIndexEntry(1, "a.py", _hash("alpha")),),
                index_revision="r3",
            ),
        ]
    )
    upsert_documents = AsyncMock(
        return_value=LateInteractionMutationResult(
            status="updated",
            accepted=1,
            unchanged=0,
            model_revision=MODEL_REVISION,
            index_revision="r3",
        )
    )
    client = _client(
        list_documents=list_documents,
        upsert_documents=upsert_documents,
    )

    result = await remote_sync.sync_remote_late_interaction(
        7,
        batch_size=1,
        dry_run=False,
        client=client,
    )

    assert result.passed
    assert result.examined == 1
    assert result.accepted == 1
    assert result.unchanged == 0
    assert result.index_revision == "r3"
    database_documents_by_ids.assert_awaited_once_with(7, [1])
    upsert_documents.assert_awaited_once()
    sent = upsert_documents.await_args.args[1]
    assert [document.chunk_id for document in sent] == [1]


@pytest.mark.asyncio
async def test_bounded_write_verifies_only_scanned_subset(monkeypatch):
    database_batch = AsyncMock(return_value=[(1, "a.py", "alpha")])
    monkeypatch.setattr(remote_sync, "_database_batch", database_batch)
    database_inventory_snapshot = AsyncMock()
    monkeypatch.setattr(
        remote_sync,
        "_database_inventory_snapshot",
        database_inventory_snapshot,
    )
    remote_extra = LateInteractionIndexEntry(99, "unscanned.py", "f" * 64)
    current = LateInteractionIndexEntry(1, "a.py", _hash("alpha"))
    list_documents = AsyncMock(
        side_effect=[
            _page(entries=(remote_extra,), index_revision="r1"),
            _page(entries=(current, remote_extra), index_revision="r2"),
            _page(entries=(current, remote_extra), index_revision="r2"),
        ]
    )
    upsert_documents = AsyncMock(
        return_value=LateInteractionMutationResult(
            status="updated",
            accepted=1,
            unchanged=0,
            model_revision=MODEL_REVISION,
            index_revision="r2",
        )
    )
    client = _client(
        list_documents=list_documents,
        upsert_documents=upsert_documents,
    )

    result = await remote_sync.sync_remote_late_interaction(
        7,
        batch_size=1,
        max_chunks=1,
        dry_run=False,
        client=client,
    )

    expected_digest = remote_sync._identity_digest({1: ("a.py", _hash("alpha"))})
    assert result.passed
    assert result.verification_mode == "bounded_subset"
    assert result.verified_documents == 1
    assert result.source_identity_digest == expected_digest
    assert result.remote_identity_digest == expected_digest
    database_inventory_snapshot.assert_not_awaited()
    database_batch.assert_awaited_once_with(
        7,
        after_chunk_id=0,
        limit=1,
    )


@pytest.mark.asyncio
async def test_bounded_write_detects_scanned_document_lost_before_proof(
    monkeypatch,
):
    monkeypatch.setattr(
        remote_sync,
        "_database_batch",
        AsyncMock(return_value=[(1, "a.py", "alpha")]),
    )
    current = LateInteractionIndexEntry(1, "a.py", _hash("alpha"))
    list_documents = AsyncMock(
        side_effect=[
            _page(entries=(current,), index_revision="r1"),
            _page(entries=(current,), index_revision="r1"),
            _page(index_revision="r2"),
        ]
    )
    client = _client(list_documents=list_documents)

    result = await remote_sync.sync_remote_late_interaction(
        7,
        max_chunks=1,
        dry_run=False,
        client=client,
    )

    assert not result.passed
    assert result.reason == "final_cross_store_inventory_mismatch"
    assert result.verification_mode == "bounded_subset"
    assert result.verified_documents == 1
    assert result.remote_identity_digest == remote_sync._identity_digest({})


@pytest.mark.asyncio
async def test_unbounded_write_requires_exact_cross_store_inventory(monkeypatch):
    monkeypatch.setattr(
        remote_sync,
        "_database_batch",
        AsyncMock(side_effect=[[(1, "a.py", "alpha")], []]),
    )
    authoritative = {1: ("a.py", _hash("alpha"))}
    database_inventory_snapshot = AsyncMock(return_value=authoritative)
    monkeypatch.setattr(
        remote_sync,
        "_database_inventory_snapshot",
        database_inventory_snapshot,
    )
    current = LateInteractionIndexEntry(1, "a.py", _hash("alpha"))
    remote_extra = LateInteractionIndexEntry(99, "stale.py", "f" * 64)
    list_documents = AsyncMock(
        side_effect=[
            _page(entries=(current, remote_extra), index_revision="r1"),
            _page(entries=(current, remote_extra), index_revision="r1"),
            _page(entries=(current, remote_extra), index_revision="r1"),
        ]
    )
    client = _client(list_documents=list_documents)

    result = await remote_sync.sync_remote_late_interaction(
        7,
        dry_run=False,
        client=client,
    )

    assert not result.passed
    assert result.reason == "final_cross_store_inventory_mismatch"
    assert result.verification_mode == "exact"
    assert result.verified_documents == 1
    assert result.source_identity_digest == remote_sync._identity_digest(authoritative)
    assert result.remote_identity_digest == remote_sync._identity_digest(
        {
            1: ("a.py", _hash("alpha")),
            99: ("stale.py", "f" * 64),
        }
    )
    database_inventory_snapshot.assert_awaited_once()


@pytest.mark.asyncio
async def test_exact_sync_finalizes_identity_once_after_equality(monkeypatch):
    monkeypatch.setattr(
        remote_sync,
        "_database_batch",
        AsyncMock(side_effect=[[(1, "a.py", "alpha")], []]),
    )
    authoritative = {1: ("a.py", _hash("alpha"))}
    monkeypatch.setattr(
        remote_sync,
        "_database_inventory_snapshot",
        AsyncMock(return_value=authoritative),
    )
    current = LateInteractionIndexEntry(1, "a.py", _hash("alpha"))
    digest = remote_sync._identity_digest(authoritative)
    finalize_index = AsyncMock(
        return_value=LateInteractionIndexStatus(
            status="ready",
            repository_id=7,
            model_revision=MODEL_REVISION,
            index_revision="r1",
            identity_digest=digest,
            document_count=1,
        )
    )
    client = _client(
        list_documents=AsyncMock(
            side_effect=[
                _page(entries=(current,), index_revision="r1"),
                _page(entries=(current,), index_revision="r1"),
                _page(entries=(current,), index_revision="r1"),
            ]
        ),
        finalize_index=finalize_index,
    )

    result = await remote_sync.sync_remote_late_interaction(
        7,
        dry_run=False,
        client=client,
    )

    assert result.passed
    assert result.remote_identity_digest == digest
    finalize_index.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_exact_sync_accepts_stale_approval_after_exact_finalization(
    monkeypatch,
):
    monkeypatch.setattr(
        remote_sync,
        "_database_batch",
        AsyncMock(side_effect=[[(1, "a.py", "alpha")], []]),
    )
    authoritative = {1: ("a.py", _hash("alpha"))}
    monkeypatch.setattr(
        remote_sync,
        "_database_inventory_snapshot",
        AsyncMock(return_value=authoritative),
    )
    current = LateInteractionIndexEntry(1, "a.py", _hash("alpha"))
    digest = remote_sync._identity_digest(authoritative)
    finalize_index = AsyncMock(
        return_value=LateInteractionIndexStatus(
            status="inventory_mismatch",
            repository_id=7,
            model_revision=MODEL_REVISION,
            index_revision="r2",
            identity_digest=digest,
            document_count=1,
            reason="identity_digest_mismatch",
        )
    )
    client = _client(
        list_documents=AsyncMock(
            side_effect=[
                _page(entries=(current,), index_revision="r2"),
                _page(entries=(current,), index_revision="r2"),
                _page(entries=(current,), index_revision="r2"),
            ]
        ),
        finalize_index=finalize_index,
    )

    result = await remote_sync.sync_remote_late_interaction(
        7,
        dry_run=False,
        client=client,
    )

    assert result.passed
    assert result.remote_identity_digest == digest
    finalize_index.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_exact_sync_rejects_stale_approval_without_exact_finalized_identity(
    monkeypatch,
):
    monkeypatch.setattr(
        remote_sync,
        "_database_batch",
        AsyncMock(side_effect=[[(1, "a.py", "alpha")], []]),
    )
    authoritative = {1: ("a.py", _hash("alpha"))}
    monkeypatch.setattr(
        remote_sync,
        "_database_inventory_snapshot",
        AsyncMock(return_value=authoritative),
    )
    current = LateInteractionIndexEntry(1, "a.py", _hash("alpha"))
    finalize_index = AsyncMock(
        return_value=LateInteractionIndexStatus(
            status="inventory_mismatch",
            repository_id=7,
            model_revision=MODEL_REVISION,
            index_revision="r2",
            identity_digest="f" * 64,
            document_count=1,
            reason="identity_digest_mismatch",
        )
    )
    client = _client(
        list_documents=AsyncMock(
            side_effect=[
                _page(entries=(current,), index_revision="r2"),
                _page(entries=(current,), index_revision="r2"),
                _page(entries=(current,), index_revision="r2"),
            ]
        ),
        finalize_index=finalize_index,
    )

    result = await remote_sync.sync_remote_late_interaction(
        7,
        dry_run=False,
        client=client,
    )

    assert not result.passed
    assert result.reason == "identity_digest_mismatch"
    finalize_index.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_prune_rechecks_postgres_and_preserves_concurrent_live_chunk(
    monkeypatch,
):
    _disable_local_traffic(monkeypatch)
    monkeypatch.setattr(
        remote_sync,
        "_database_batch",
        AsyncMock(
            side_effect=[
                [],
                [(99, "new.py", "concurrently created")],
                [],
            ]
        ),
    )
    database_documents_by_ids = AsyncMock(return_value=[(99, "new.py", "concurrently created")])
    monkeypatch.setattr(
        remote_sync,
        "_database_documents_by_ids",
        database_documents_by_ids,
    )
    stale_entry = LateInteractionIndexEntry(99, "stale.py", "f" * 64)
    list_documents = AsyncMock(
        side_effect=[
            _page(entries=(stale_entry,), index_revision="r1"),
            _page(entries=(stale_entry,), index_revision="r1"),
            _page(entries=(stale_entry,), index_revision="r1"),
        ]
    )
    client = _client(list_documents=list_documents)

    result = await remote_sync.sync_remote_late_interaction(
        7,
        dry_run=False,
        prune=True,
        client=client,
    )

    assert not result.passed
    assert result.reason == "final_cross_store_inventory_mismatch"
    assert result.deleted == 0
    database_documents_by_ids.assert_awaited_once_with(7, [99])
    client.delete_documents.assert_not_awaited()


@pytest.mark.parametrize(
    ("setting_name", "active_value"),
    [
        ("LATE_INTERACTION_ENABLED", True),
        ("LATE_INTERACTION_DUAL_WRITE_ENABLED", True),
        ("LATE_INTERACTION_SHADOW_ENABLED", True),
        ("LATE_INTERACTION_RERANK_ENABLED", True),
        ("LATE_INTERACTION_CANARY_PERCENT", 1.0),
    ],
)
@pytest.mark.asyncio
async def test_prune_refuses_active_local_traffic(
    monkeypatch,
    setting_name,
    active_value,
):
    _disable_local_traffic(monkeypatch)
    monkeypatch.setattr(settings, setting_name, active_value)
    client = _client(list_documents=AsyncMock())

    with pytest.raises(ValueError, match="active late-interaction traffic"):
        await remote_sync.sync_remote_late_interaction(
            7,
            dry_run=False,
            prune=True,
            client=client,
        )

    client.list_documents.assert_not_awaited()
    client.upsert_documents.assert_not_awaited()
    client.delete_documents.assert_not_awaited()
