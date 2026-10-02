"""Durable (Postgres) job fencing — lease claim/assert/renew/release logic.

The session is faked: the lease row store is a plain dict, so these tests
exercise the compare-and-set state machine without a database. The real
serialization guarantee (FOR UPDATE inside the caller's transaction) is
exercised by integration on CI where Postgres is available.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brain.database.models import WorkerJobLease
from brain.workers.db_fencing import (
    DbFence,
    assert_current_db_fence,
    assert_db_fence,
    claim_db_lease,
    current_job_fence,
    release_db_lease,
    renew_db_lease,
)
from brain.workers.errors import StaleWorkerFencingError


def _row(job_id="job-1", token="tok-a", minutes_from_now=5, worker="w1"):
    return WorkerJobLease(
        job_id=job_id,
        fencing_token=token,
        worker_id=worker,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now),
        updated_at=datetime.now(timezone.utc),
    )


class _FakeResult:
    def __init__(self, row=None, rowcount=0):
        self._row = row
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._row


class _FakeSession:
    """Single-job session: execute() returns the stored row for select,

    applies deletes, and records adds into the shared store."""

    def __init__(self, store: dict, job_id: str = "job-1"):
        self.store = store
        self.job_id = job_id

    def add(self, obj):
        self.store[obj.job_id] = obj

    async def execute(self, stmt):
        from sqlalchemy.sql.dml import Delete

        if isinstance(stmt, Delete):
            row = self.store.get(self.job_id)
            if row is not None and row.fencing_token == self._delete_token(stmt):
                del self.store[self.job_id]
                return _FakeResult(rowcount=1)
            return _FakeResult(rowcount=0)
        return _FakeResult(row=self.store.get(self.job_id))

    @staticmethod
    def _delete_token(stmt) -> str:
        for criterion in stmt.whereclause.get_children():
            if "fencing_token" in str(criterion):
                return criterion.right.value
        return ""


def _session(store, job_id="job-1"):
    return _FakeSession(store, job_id)


@pytest.mark.asyncio
async def test_claim_inserts_lease_when_absent():
    store = {}
    ok = await claim_db_lease(
        _session(store), job_id="job-1", fencing_token="tok-a", worker_id="w1", ttl_seconds=60
    )
    assert ok is True
    assert store["job-1"].fencing_token == "tok-a"


@pytest.mark.asyncio
async def test_claim_refused_when_held_unexpired_by_other_token():
    store = {"job-1": _row(token="tok-a")}
    ok = await claim_db_lease(
        _session(store), job_id="job-1", fencing_token="tok-b", worker_id="w2", ttl_seconds=60
    )
    assert ok is False
    assert store["job-1"].fencing_token == "tok-a"


@pytest.mark.asyncio
async def test_claim_takeover_when_lease_expired():
    store = {"job-1": _row(token="tok-a", minutes_from_now=-1)}
    ok = await claim_db_lease(
        _session(store), job_id="job-1", fencing_token="tok-b", worker_id="w2", ttl_seconds=60
    )
    assert ok is True
    assert store["job-1"].fencing_token == "tok-b"
    assert store["job-1"].worker_id == "w2"


@pytest.mark.asyncio
async def test_claim_renews_when_same_token():
    store = {"job-1": _row(token="tok-a", minutes_from_now=1)}
    ok = await claim_db_lease(
        _session(store), job_id="job-1", fencing_token="tok-a", worker_id="w2", ttl_seconds=120
    )
    assert ok is True
    assert store["job-1"].worker_id == "w2"


@pytest.mark.asyncio
async def test_assert_passes_for_owner_unexpired():
    store = {"job-1": _row(token="tok-a")}
    await assert_db_fence(_session(store), job_id="job-1", fencing_token="tok-a")


@pytest.mark.asyncio
async def test_assert_fails_without_lease_row():
    with pytest.raises(StaleWorkerFencingError):
        await assert_db_fence(_session({}), job_id="job-1", fencing_token="tok-a")


@pytest.mark.asyncio
async def test_assert_fails_on_token_mismatch():
    store = {"job-1": _row(token="tok-a")}
    with pytest.raises(StaleWorkerFencingError):
        await assert_db_fence(_session(store), job_id="job-1", fencing_token="tok-b")


@pytest.mark.asyncio
async def test_assert_fails_when_lease_expired():
    store = {"job-1": _row(token="tok-a", minutes_from_now=-1)}
    with pytest.raises(StaleWorkerFencingError):
        await assert_db_fence(_session(store), job_id="job-1", fencing_token="tok-a")


@pytest.mark.asyncio
async def test_renew_extends_expiry_for_owner():
    store = {"job-1": _row(token="tok-a", minutes_from_now=1)}
    before = store["job-1"].expires_at
    ok = await renew_db_lease(
        _session(store), job_id="job-1", fencing_token="tok-a", ttl_seconds=300
    )
    assert ok is True
    assert store["job-1"].expires_at > before


@pytest.mark.asyncio
async def test_renew_refused_for_other_token_or_missing_row():
    store = {"job-1": _row(token="tok-a")}
    assert await renew_db_lease(
        _session(store), job_id="job-1", fencing_token="tok-b", ttl_seconds=60
    ) is False
    assert await renew_db_lease(
        _session({}), job_id="job-1", fencing_token="tok-a", ttl_seconds=60
    ) is False


@pytest.mark.asyncio
async def test_release_deletes_only_for_owner():
    store = {"job-1": _row(token="tok-a")}
    assert await release_db_lease(
        _session(store), job_id="job-1", fencing_token="tok-b"
    ) is False
    assert "job-1" in store
    assert await release_db_lease(
        _session(store), job_id="job-1", fencing_token="tok-a"
    ) is True
    assert "job-1" not in store


@pytest.mark.asyncio
async def test_current_fence_noop_when_unset():
    # No fence installed -> any session is fine.
    await assert_current_db_fence(_session({}))


@pytest.mark.asyncio
async def test_current_fence_asserts_installed_token():
    store = {"job-1": _row(token="tok-a")}
    token = current_job_fence.set(DbFence(job_id="job-1", fencing_token="tok-a"))
    try:
        await assert_current_db_fence(_session(store))
        store["job-1"].fencing_token = "tok-b"
        with pytest.raises(StaleWorkerFencingError):
            await assert_current_db_fence(_session(store))
    finally:
        current_job_fence.reset(token)


def test_stale_worker_error_is_permanent():
    # Fencing loss must never be retried under the same token.
    from brain.workers.errors import PermanentJobError

    assert issubclass(StaleWorkerFencingError, PermanentJobError)
