"""Provenance and conflict safety of team memory: every memory object names its
author and last editor, decisions and rules supersede instead of overwrite,
``expected_version`` is compare-and-swap, and ``repo_scope`` folds case on any OS.

Runs on a disposable sqlite database; no Postgres, no API app import.
"""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from brain.database.models import Decision, Learning, MemorySkill, Rule
from brain.memory import decision_store, learning_store, rule_store, skill_store
from brain.memory.decision_store import DecisionStore
from brain.memory.learning_store import LearningStore
from brain.memory.provenance import (
    MemoryVersionConflict,
    MemoryWriteResult,
    SelfApprovalError,
    check_expected_version,
    distinct_principals,
    sole_principal,
)
from brain.memory.repo_scope import normalize_repo_scope
from brain.memory.rule_store import RuleStore

ALICE, BOB = 11, 12


@pytest_asyncio.fixture
async def memory_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'provenance.sqlite3'}")
    async with engine.begin() as connection:
        for model in (Rule, Decision, Learning, MemorySkill):
            await connection.run_sync(lambda conn, m=model: m.__table__.create(conn))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    for module in (rule_store, decision_store, learning_store, skill_store):
        monkeypatch.setattr(module, "async_session_factory", factory)
    try:
        yield factory
    finally:
        await engine.dispose()


# --- repo_scope -----------------------------------------------------------------

@pytest.mark.parametrize("raw", ["system_global", "SYSTEM_GLOBAL", "System_Global", "  system_global  "])
def test_system_global_scope_resolves_to_none_in_any_case(raw):
    assert normalize_repo_scope(raw) is None


# --- provenance primitives ------------------------------------------------------

def test_expected_version_is_compare_and_swap():
    check_expected_version("decision", "x", None, 7)        # no expectation: always passes
    check_expected_version("decision", "x", 0, None)        # must not exist yet
    check_expected_version("decision", "x", 3, 3)
    with pytest.raises(MemoryVersionConflict) as conflict:
        check_expected_version("decision", "x", 2, 3)
    assert conflict.value.expected_version == 2 and conflict.value.actual_version == 3
    with pytest.raises(MemoryVersionConflict):
        check_expected_version("rule", "r", 0, 1)


def test_distinct_and_sole_principals_skip_unknown_authors():
    evidence = [{"event_id": 1, "author_principal_id": ALICE}, {"event_id": 2}, {"event_id": 3, "author_principal_id": "11"}]
    assert distinct_principals(evidence) == {ALICE}
    assert sole_principal(evidence) == ALICE
    assert sole_principal(evidence + [{"author_principal_id": BOB}]) is None
    assert sole_principal([{"event_id": 1}]) is None
    assert MemoryWriteResult(id=1, version=2, created=False, changed=True, superseded_id=9).as_dict() == {
        "version": 2, "created": False, "changed": True, "superseded_id": 9,
    }


# --- decisions ------------------------------------------------------------------

async def _decisions(factory):
    async with factory() as session:
        return list((await session.execute(select(Decision).order_by(Decision.id))).scalars())


@pytest.mark.asyncio
async def test_decision_change_supersedes_instead_of_overwriting(memory_db):
    first = await DecisionStore.record_decision(
        "Use pgvector", repo_path="/repos/a", description="v1", author_principal_id=ALICE,
    )
    assert first.created and first.version == 1

    again = await DecisionStore.record_decision(
        "Use pgvector", repo_path="/repos/a", description="v1", author_principal_id=BOB,
    )
    assert not again.created and not again.changed and again.version == 1

    second = await DecisionStore.record_decision(
        "use pgvector", repo_path="/repos/a", description="v2", author_principal_id=BOB,
    )
    assert second.id == first.id and second.version == 2 and second.changed and second.superseded_id

    rows = {row.id: row for row in await _decisions(memory_db)}
    canonical, history = rows[first.id], rows[second.superseded_id]
    assert canonical.description == "v2" and canonical.version == 2 and canonical.superseded_by_id is None
    assert canonical.author_principal_id == ALICE and canonical.updated_by_principal_id == BOB
    assert canonical.supersedes_id == history.id
    assert history.description == "v1" and history.status == "superseded" and history.version == 1
    assert history.superseded_by_id == canonical.id and history.author_principal_id == ALICE
    assert canonical.created_at is not None and canonical.updated_at is not None


@pytest.mark.asyncio
async def test_decision_expected_version_conflict_leaves_the_row_untouched(memory_db):
    created = await DecisionStore.record_decision("Ship weekly", repo_path="/repos/a", description="v1",
                                                  author_principal_id=ALICE, expected_version=0)
    assert created.version == 1
    with pytest.raises(MemoryVersionConflict):
        await DecisionStore.record_decision("Ship weekly", repo_path="/repos/a", description="v1",
                                            author_principal_id=BOB, expected_version=0)
    with pytest.raises(MemoryVersionConflict) as conflict:
        await DecisionStore.record_decision("Ship weekly", repo_path="/repos/a", description="stale edit",
                                            author_principal_id=BOB, expected_version=3)
    assert conflict.value.actual_version == 1
    rows = await _decisions(memory_db)
    assert [(r.description, r.version) for r in rows] == [("v1", 1)]

    ok = await DecisionStore.record_decision("Ship weekly", repo_path="/repos/a", description="v2",
                                             author_principal_id=BOB, expected_version=1)
    assert ok.version == 2
    assert await DecisionStore.add_decision("Ship weekly", repo_path="/repos/a", description="v2") == ok.id


@pytest.mark.asyncio
async def test_deprecate_decision_records_who_did_it(memory_db):
    created = await DecisionStore.record_decision("Retire X", repo_path="/repos/a", author_principal_id=ALICE)
    assert await DecisionStore.deprecate_decision(created.id, principal_id=BOB)
    (row,) = await _decisions(memory_db)
    assert row.status == "deprecated" and row.updated_by_principal_id == BOB


# --- rules ----------------------------------------------------------------------

async def _rules(factory):
    async with factory() as session:
        return list((await session.execute(select(Rule).order_by(Rule.id))).scalars())


@pytest.mark.asyncio
async def test_rule_change_supersedes_and_keeps_history(memory_db):
    first = await RuleStore.record_rule("No print", repo_path="/repos/a", description="v1",
                                        severity="low", author_principal_id=ALICE)
    assert first.created and first.version == 1
    same = await RuleStore.record_rule("No print", repo_path="/repos/a", description="v1",
                                       severity="low", author_principal_id=BOB)
    assert same.version == 1 and not same.changed

    second = await RuleStore.record_rule("No print", repo_path="/repos/a", description="v2",
                                         severity="high", author_principal_id=BOB)
    assert second.id == first.id and second.version == 2 and second.superseded_id

    rows = {row.id: row for row in await _rules(memory_db)}
    canonical, history = rows[first.id], rows[second.superseded_id]
    assert canonical.description == "v2" and canonical.severity == "high" and canonical.version == 2
    assert canonical.author_principal_id == ALICE and canonical.updated_by_principal_id == BOB
    assert history.description == "v1" and history.status == "superseded"
    assert history.superseded_by_id == canonical.id and history.version == 1
    # History never masquerades as the live rule.
    active = await RuleStore.list_active_rules("/repos/a")
    assert [r.id for r in active] == [canonical.id]


@pytest.mark.asyncio
async def test_rule_expected_version_conflict(memory_db):
    await RuleStore.record_rule("Pin deps", repo_path="/repos/a", description="v1", author_principal_id=ALICE)
    with pytest.raises(MemoryVersionConflict):
        await RuleStore.record_rule("Pin deps", repo_path="/repos/a", description="v2",
                                    author_principal_id=BOB, expected_version=0)
    assert [r.description for r in await _rules(memory_db)] == ["v1"]
    ok = await RuleStore.record_rule("Pin deps", repo_path="/repos/a", description="v2",
                                     author_principal_id=BOB, expected_version=1)
    assert ok.version == 2


@pytest.mark.asyncio
async def test_yaml_sync_versions_changed_rules_without_a_principal(memory_db, tmp_path):
    yaml_path = tmp_path / "rules.yaml"
    yaml_path.write_text("rules:\n  - id: r1\n    name: R1\n    description: one\n", encoding="utf-8")
    await RuleStore.sync_rules_from_yaml(str(yaml_path))
    yaml_path.write_text("rules:\n  - id: r1\n    name: R1\n    description: two\n", encoding="utf-8")
    await RuleStore.sync_rules_from_yaml(str(yaml_path))
    rows = await _rules(memory_db)
    live = [r for r in rows if r.superseded_by_id is None]
    assert len(rows) == 2 and len(live) == 1
    assert live[0].description == "two" and live[0].version == 2 and live[0].updated_by_principal_id is None


# --- learnings and skills -------------------------------------------------------

@pytest.mark.asyncio
async def test_learning_records_author_and_updates(memory_db):
    learning_id = await LearningStore.add_learning(
        statement="Workers need 2 GB", category="infra", confidence=0.7, repo_scope="/repos/a",
        author_principal_id=ALICE,
    )
    newer_id = await LearningStore.add_learning(
        statement="Workers need 4 GB", category="infra", confidence=0.8, repo_scope="/repos/a",
        author_principal_id=BOB,
    )
    await LearningStore.supersede(learning_id, newer_id, updated_by_principal_id=BOB)
    await LearningStore.reject(newer_id, updated_by_principal_id=ALICE)
    async with memory_db() as session:
        rows = {row.id: row for row in (await session.execute(select(Learning))).scalars()}
    old, new = rows[learning_id], rows[newer_id]
    assert old.author_principal_id == ALICE and old.updated_by_principal_id == BOB
    assert old.status == "superseded" and old.superseded_by == newer_id
    assert new.author_principal_id == BOB and new.updated_by_principal_id == ALICE and new.status == "rejected"
    assert old.created_at is not None and old.updated_at is not None


@pytest.mark.asyncio
async def test_skill_author_and_self_approval_rule(memory_db):
    skill = await skill_store.create_skill(
        name="Release to VPS", description="scp + server_up", triggers=["deploy"], workflow=["tar", "scp"],
        source_episode_ids=[], source_learning_ids=[], confidence=0.6, repo_scope="/repos/a",
        status="pending", author_principal_id=ALICE,
    )
    assert skill.author_principal_id == ALICE and skill.updated_by_principal_id == ALICE

    with pytest.raises(SelfApprovalError):
        await skill_store.decide_skill(skill.id, approve=True, principal_id=ALICE, team_state="team")
    async with memory_db() as session:
        assert (await session.get(MemorySkill, skill.id)).status == "pending"

    decided = await skill_store.decide_skill(skill.id, approve=True, principal_id=BOB, team_state="team")
    assert decided["status"] == "active"
    async with memory_db() as session:
        row = await session.get(MemorySkill, skill.id)
    assert row.updated_by_principal_id == BOB and row.author_principal_id == ALICE

    solo = await skill_store.create_skill(
        name="Solo skill", description="d", triggers=[], workflow=[], source_episode_ids=[],
        source_learning_ids=[], confidence=0.5, repo_scope=None, status="pending", author_principal_id=ALICE,
    )
    # A team of one approves its own work.
    assert (await skill_store.decide_skill(solo.id, approve=True, principal_id=ALICE, team_state="solo"))["status"] == "active"


# --- harness events carry the author into the evidence --------------------------

def test_evidence_ref_copies_the_author_from_the_event_payload():
    """L1 events carry ``author_principal_id`` in their payload (HarnessStore.add_event
    stamps it there); consolidation copies it into the evidence refs so episodes,
    learnings and G2 can attribute the claim."""
    import inspect

    from brain.memory.consolidation import _evidence_ref
    from brain.memory.harness_store import HarnessStore

    assert "author_principal_id" in inspect.signature(HarnessStore.add_event).parameters

    stamped = SimpleNamespace(id=1, task_id=7, classification="learning",
                              payload_json={"summary": "s", "author_principal_id": ALICE})
    ref = _evidence_ref(stamped)
    assert ref["event_id"] == 1 and ref["author_principal_id"] == ALICE

    anonymous = SimpleNamespace(id=2, task_id=7, classification="learning", payload_json={"summary": "s"})
    assert "author_principal_id" not in _evidence_ref(anonymous)
    assert _evidence_ref(SimpleNamespace(id=3, task_id=7, classification="learning", payload_json=None))["event_id"] == 3
