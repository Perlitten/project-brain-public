"""Decision/rule compare-and-swap and proposal approval under real concurrency (team memory load e2e findings).

Every writer is held right after it read the canonical row until all writers have read it (or a short timeout):
that is the lost-update window. Without a row lock several writers then pass the version check and the history
gets duplicate versions; with it the writers queue on the row and every loser sees the new version (409).

The race tests need real row locks and a unique-index wait, so they run only against the shared PostgreSQL team
database (TEST_TEAM_POSTGRES_URL, the ``team_db`` fixture of test_team_foundations). The revoked member's personal
tier needs ``memory_learnings`` (a pgvector column), so that test runs on SQLite only.
"""
import asyncio
import os

import pytest
from sqlalchemy import select

from brain.database.migrations import _ensure_decision_title_uniqueness
from brain.database.models import Decision, Learning, MemoryProposal, Principal, Rule
from brain.memory import decision_store, proposals, rule_store
from brain.memory.decision_store import DecisionStore
from brain.memory.learning_store import LearningStore
from brain.memory.provenance import MemoryVersionConflict
from brain.memory.rule_store import RuleStore
from brain.team import provisioning
from brain.team.activity import ActivityQuery, ActivityScope, list_activity
from tests.test_memory_proposals import ADMIN, learning_table, review_app
from tests.test_team_foundations import team_db as team_fixture
from tests.test_team_transport import client_for

team_db = team_fixture
REPO = "/synthetic/A"
WRITERS = 6
needs_postgres = pytest.mark.skipif(not os.getenv("TEST_TEAM_POSTGRES_URL"),
                                    reason="requires a disposable PostgreSQL team database")


class ReadBarrier:
    """Holds each writer after its read until ``parties`` writers have read, or ``timeout`` seconds passed."""

    def __init__(self, parties: int, timeout: float = 0.3):
        self.parties, self.timeout, self.reads = parties, timeout, 0
        self.everyone_read = asyncio.Event()

    async def wait(self) -> None:
        self.reads += 1
        if self.reads >= self.parties:
            self.everyone_read.set()
        try:
            await asyncio.wait_for(self.everyone_read.wait(), self.timeout)
        except asyncio.TimeoutError:
            pass  # serialized by a row lock: the others cannot read until this writer commits


def hold_after_read(monkeypatch, module, name: str, parties: int) -> None:
    """Wrap the store's canonical-row lookup so every concurrent writer reads before any of them writes."""
    real = getattr(module, name)
    barrier = ReadBarrier(parties)

    async def held(*args, **kwargs):
        found = await real(*args, **kwargs)
        await barrier.wait()
        return found

    monkeypatch.setattr(module, name, held)


def use_factory(monkeypatch, factory) -> None:
    for module in (decision_store, rule_store, proposals):
        monkeypatch.setattr(module, "async_session_factory", factory)


async def outcomes(calls):
    """``"ok"`` or the exception of each concurrent call; anything but a version conflict fails the test."""
    results = await asyncio.gather(*calls, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, (MemoryVersionConflict,
                                                                          proposals.ProposalStateError)):
            raise result
    return results


async def production_title_index(factory) -> None:
    """The unique canonical-title index production gets from the migrations (the fixture uses create_all)."""
    async with factory() as session, session.begin():
        await _ensure_decision_title_uniqueness(await session.connection())


def winners(results) -> list:
    return [result for result in results if not isinstance(result, BaseException)]


async def decision_chain(factory, title: str) -> tuple[Decision, list[Decision]]:
    """(canonical row, history rows superseded by it) of one decision."""
    async with factory() as session:
        rows = list((await session.execute(select(Decision).where(Decision.title == title)
                                           .order_by(Decision.id))).scalars().all())
    canonical = [row for row in rows if row.superseded_by_id is None]
    assert len(canonical) == 1, [(row.id, row.version, row.superseded_by_id) for row in rows]
    history = [row for row in rows if row.superseded_by_id is not None]
    assert all(row.superseded_by_id == canonical[0].id for row in history)
    return canonical[0], history


def assert_linear(canonical, history) -> None:
    """One history row per earlier version, each reachable from the canonical row through ``supersedes_id``."""
    assert sorted(row.version for row in history) == list(range(1, canonical.version))
    by_id = {row.id: row for row in history}
    reachable, cursor = [], canonical.supersedes_id
    while cursor is not None:
        reachable.append(cursor)
        cursor = by_id[cursor].supersedes_id
    assert sorted(reachable) == sorted(by_id), "history rows unreachable from the canonical row"


# --------------------------------------------------------------------------- decisions


@pytest.mark.asyncio
@needs_postgres
async def test_concurrent_decision_cas_has_exactly_one_winner_per_expected_version(team_db, monkeypatch):
    factory, _, _ = team_db
    use_factory(monkeypatch, factory)
    await DecisionStore.record_decision(title="Queue choice", repo_path=REPO, description="v1",
                                        author_principal_id=2)
    hold_after_read(monkeypatch, decision_store, "find_canonical_decision", WRITERS)
    for expected in (1, 2, 3):
        results = await outcomes(DecisionStore.record_decision(
            title="Queue choice", repo_path=REPO, description=f"round {expected} writer {index}",
            author_principal_id=2, expected_version=expected) for index in range(WRITERS))
        won = winners(results)
        assert len(won) == 1, f"round {expected}: {len(won)} writers passed the same expected_version"
        assert won[0].version == expected + 1
        for lost in results:
            if isinstance(lost, MemoryVersionConflict):
                assert (lost.expected_version, lost.actual_version) == (expected, expected + 1)
        canonical, history = await decision_chain(factory, "Queue choice")
        assert canonical.version == expected + 1
        assert_linear(canonical, history)
        # The acknowledged write is the one that is canonical: no confirmed write vanished.
        winner_index = [i for i, result in enumerate(results) if not isinstance(result, BaseException)][0]
        assert canonical.description == f"round {expected} writer {winner_index}"


@pytest.mark.asyncio
@needs_postgres
async def test_concurrent_blind_decision_writes_serialize_into_one_chain(team_db, monkeypatch):
    factory, _, _ = team_db
    use_factory(monkeypatch, factory)
    await DecisionStore.record_decision(title="Blind", repo_path=REPO, description="seed", author_principal_id=2)
    hold_after_read(monkeypatch, decision_store, "find_canonical_decision", WRITERS)
    results = await outcomes(DecisionStore.record_decision(
        title="Blind", repo_path=REPO, description=f"blind writer {index}", author_principal_id=2)
        for index in range(WRITERS))
    assert len(winners(results)) == WRITERS  # no expected_version: every edit lands, one after the other
    canonical, history = await decision_chain(factory, "Blind")
    assert canonical.version == WRITERS + 1
    assert_linear(canonical, history)
    contents = [canonical.description, *(row.description for row in history)]
    assert sorted(contents) == sorted(["seed", *(f"blind writer {index}" for index in range(WRITERS))])


@pytest.mark.asyncio
@needs_postgres
async def test_concurrent_decision_creation_is_one_create_and_conflicts(team_db, monkeypatch):
    factory, _, _ = team_db
    use_factory(monkeypatch, factory)
    await production_title_index(factory)
    hold_after_read(monkeypatch, decision_store, "find_canonical_decision", 4)
    results = await outcomes(DecisionStore.record_decision(
        title="Fresh", repo_path=REPO, description=f"creator {index}", author_principal_id=2,
        expected_version=None if index % 2 else 0) for index in range(4))
    assert [result.created for result in winners(results)] == [True]
    lost = [result for result in results if isinstance(result, BaseException)]
    assert len(lost) == 3 and all(isinstance(result, MemoryVersionConflict) for result in lost)
    assert all((result.expected_version, result.actual_version) == (0, 1) for result in lost)
    canonical, history = await decision_chain(factory, "Fresh")
    assert canonical.version == 1 and history == []


# --------------------------------------------------------------------------- rules


@pytest.mark.asyncio
@needs_postgres
async def test_concurrent_rule_cas_has_exactly_one_winner(team_db, monkeypatch):
    factory, _, _ = team_db
    use_factory(monkeypatch, factory)
    await RuleStore.record_rule(name="No blocking IO", repo_path=REPO, description="v1", author_principal_id=2)
    hold_after_read(monkeypatch, rule_store, "_find_rule", WRITERS)
    for expected in (1, 2):
        results = await outcomes(RuleStore.record_rule(
            name="No blocking IO", repo_path=REPO, description=f"round {expected} writer {index}",
            author_principal_id=2, expected_version=expected) for index in range(WRITERS))
        assert len(winners(results)) == 1
        assert all(isinstance(result, MemoryVersionConflict) and result.actual_version == expected + 1
                   for result in results if isinstance(result, BaseException))
    async with factory() as session:
        rows = list((await session.execute(select(Rule))).scalars().all())
    canonical = [row for row in rows if row.superseded_by_id is None]
    assert len(canonical) == 1 and canonical[0].version == 3
    assert sorted(row.version for row in rows if row.superseded_by_id) == [1, 2]


@pytest.mark.asyncio
@needs_postgres
async def test_concurrent_rule_creation_is_one_create_and_conflicts(team_db, monkeypatch):
    factory, _, _ = team_db
    use_factory(monkeypatch, factory)
    hold_after_read(monkeypatch, rule_store, "_find_rule", 4)
    results = await outcomes(RuleStore.record_rule(
        name="Fresh rule", repo_path=REPO, description=f"creator {index}", author_principal_id=2)
        for index in range(4))
    assert [result.created for result in winners(results)] == [True]
    assert all(isinstance(result, MemoryVersionConflict) and (result.expected_version, result.actual_version) == (0, 1)
               for result in results if isinstance(result, BaseException))


@pytest.mark.asyncio
@needs_postgres
async def test_rule_cas_losers_get_409_over_http(team_db, monkeypatch):
    factory, _, _ = team_db
    app = review_app(factory, monkeypatch)
    await production_title_index(factory)
    # Global items: a team administrator writes them directly (a member's write would become a proposal).
    rule = {"name": "Http rule", "description": "v1", "repo_path": None}
    async with client_for(app) as client:
        assert (await client.post("/rules", json=rule, headers=ADMIN)).status_code == 200
        hold_after_read(monkeypatch, rule_store, "_find_rule", 4)
        replies = await asyncio.gather(*(client.post("/rules", json={**rule, "description": f"edit {index}",
                                                                     "expected_version": 1}, headers=ADMIN)
                                         for index in range(4)))
        decisions = await asyncio.gather(*(client.post("/decisions", json={
            "title": "Http decision", "description": f"create {index}", "repo_path": None,
            "expected_version": 0}, headers=ADMIN) for index in range(4)))
    for batch in (replies, decisions):
        assert sorted(reply.status_code for reply in batch) == [200, 409, 409, 409], [r.text for r in batch]
        assert all("re-read it and retry" in reply.json()["detail"] for reply in batch if reply.status_code == 409)


# --------------------------------------------------------------------------- proposal approval


async def member_proposals(count: int, base_version: int) -> list[MemoryProposal]:
    return [await proposals.create_proposal("decision", title="Reviewed", repo_path=REPO,
                                            payload={"description": f"proposal {index}"}, author_principal_id=2,
                                            expected_version=base_version) for index in range(count)]


@pytest.mark.asyncio
@needs_postgres
async def test_concurrent_approvals_on_one_base_version_publish_exactly_once(team_db, monkeypatch):
    factory, _, _ = team_db
    use_factory(monkeypatch, factory)
    await DecisionStore.record_decision(title="Reviewed", repo_path=REPO, description="v1", caller_role="admin",
                                        author_principal_id=4)
    rows = await member_proposals(4, base_version=1)
    hold_after_read(monkeypatch, decision_store, "find_canonical_decision", 4)
    results = await outcomes(proposals.approve_proposal(row.id, expected_version=row.version,
                                                        reviewer_principal_id=3 + index % 2)
                             for index, row in enumerate(rows))
    approved = winners(results)
    assert len(approved) == 1 and approved[0].result_version == 2
    assert all(isinstance(result, MemoryVersionConflict) for result in results if isinstance(result, BaseException))
    async with factory() as session:
        states = sorted((await session.execute(select(MemoryProposal.state))).scalars().all())
    assert states == ["approved", "pending", "pending", "pending"]  # losers roll back whole: still pending
    canonical, history = await decision_chain(factory, "Reviewed")
    assert canonical.version == 2 and canonical.approved_by_principal_id == approved[0].reviewed_by_principal_id
    assert_linear(canonical, history)


@pytest.mark.asyncio
@needs_postgres
async def test_concurrent_decisions_of_one_proposal_have_one_winner(team_db, monkeypatch):
    factory, _, _ = team_db
    use_factory(monkeypatch, factory)
    [row] = await member_proposals(1, base_version=0)
    calls = [proposals.approve_proposal(row.id, expected_version=1, reviewer_principal_id=3),
             proposals.approve_proposal(row.id, expected_version=1, reviewer_principal_id=4),
             proposals.reject_proposal(row.id, expected_version=1, reviewer_principal_id=3),
             proposals.reject_proposal(row.id, expected_version=1, reviewer_principal_id=4)]
    results = await outcomes(calls)
    assert len(winners(results)) == 1
    assert all(isinstance(result, proposals.ProposalStateError) for result in results
               if isinstance(result, BaseException))
    async with factory() as session:
        count = len((await session.execute(select(Decision))).scalars().all())
    assert count == (1 if winners(results)[0].state == "approved" else 0)


# --------------------------------------------------------------------------- revoked member's personal tier


@pytest.mark.asyncio
async def test_revocation_retires_the_members_personal_learnings(team_db, monkeypatch):
    factory, _, _ = team_db
    await learning_table(factory)
    review_app(factory, monkeypatch)  # points the memory stores at the fixture database
    mine = [await LearningStore.add_learning(statement=f"private note {index}", repo_scope=REPO,
                                             status=proposals.PERSONAL_STATUS, author_principal_id=2)
            for index in range(3)]
    other = await LearningStore.add_learning(statement="someone else's note", repo_scope=REPO,
                                             status=proposals.PERSONAL_STATUS, author_principal_id=3)
    shared = await LearningStore.add_learning(statement="approved knowledge", repo_scope=REPO,
                                              author_principal_id=2)
    pending = await proposals.propose_learning(mine[0], 2)

    async with factory() as session, session.begin():
        await provisioning.revoke_member(session, principal=await session.get(Principal, 2))

    async with factory() as session:
        status = dict((await session.execute(select(Learning.id, Learning.status))).all())
    # Kept for the audit trail, but out of the personal tier: nobody reads, promotes or approves them.
    assert [status[learning_id] for learning_id in mine] == [proposals.RETIRED_PERSONAL_STATUS] * 3
    assert status[other] == proposals.PERSONAL_STATUS and status[shared] == "active"
    assert await proposals.personal_learnings(2) == []
    with pytest.raises(proposals.ProposalNotFound):
        await proposals.propose_learning(mine[1], 2)
    with pytest.raises(proposals.ProposalStateError):  # withdrawn with the membership
        await proposals.approve_proposal(pending.id, expected_version=pending.version, reviewer_principal_id=3)
    assert [row.id for row in await LearningStore.list_active_learnings(REPO)] == [shared]
    # Retired notes stay out of the team activity feed, like personal ones.
    async with factory() as session:
        feed = await list_activity(session, ActivityScope(administrator=True, repository_ids=None, project_ids=None,
                                                          unscoped=True), ActivityQuery(kinds=frozenset({"learning"})))
    titles = " ".join(str(event) for event in feed["events"])
    assert "approved knowledge" in titles
    assert "private note" not in titles and "someone else's note" not in titles
