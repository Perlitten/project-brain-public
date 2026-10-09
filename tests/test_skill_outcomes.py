import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain.memory.skill_outcomes import SkillOutcomeConflict, record_skill_outcome


def setup_session(monkeypatch, *, existing=None, validation_status="pass", scope="/app", is_global=False):
    tid = uuid.uuid4()
    skill = SimpleNamespace(id=1, status="active", repo_scope=scope, is_global=is_global,
                            times_used=0, times_successful=0)
    validation = SimpleNamespace(id=2, status=validation_status, artifact_id=3, exit_code=0)
    results = []
    for scalar in (skill, SimpleNamespace(repo_path="/app"), validation):
        response = MagicMock()
        response.scalar_one_or_none.return_value = scalar
        results.append(response)
    artifacts = MagicMock()
    artifacts.scalars.return_value.all.return_value = [SimpleNamespace(id=3, path_or_uri="test://report")]
    previous = MagicMock()
    previous.scalar_one_or_none.return_value = existing
    results.append(previous)
    results.append(artifacts)
    session = AsyncMock()
    session.execute.side_effect = results
    session.add = MagicMock(side_effect=lambda row: setattr(row, "id", 55))
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    monkeypatch.setattr("brain.memory.skill_outcomes.async_session_factory", factory)
    args = {"skill_id": 1, "task_id": str(tid), "validation_id": 2, "outcome": "success",
            "evidence": {"artifact_ids": [3], "source_refs": ["test://report"]}}
    return skill, session, args


@pytest.mark.asyncio
async def test_success_is_atomic_and_serializes_concurrent_skill_updates(monkeypatch):
    skill, session, args = setup_session(monkeypatch)
    result = await record_skill_outcome(**args)
    assert result["reported_validation"] and not result["idempotent"]
    assert (skill.times_used, skill.times_successful) == (1, 1)
    session.commit.assert_awaited_once()
    assert session.add.call_args.args[0].task_id == uuid.UUID(args["task_id"])
    assert "FOR UPDATE" in str(session.execute.await_args_list[0].args[0])


@pytest.mark.asyncio
async def test_identical_repeat_never_increments_counters(monkeypatch):
    evidence = {"artifact_ids": [3], "source_refs": ["test://report"]}
    prior = SimpleNamespace(id=55, validation_id=2, outcome="success", evidence=evidence)
    skill, session, args = setup_session(monkeypatch, existing=prior)
    result = await record_skill_outcome(**args)
    assert result["idempotent"] and skill.times_used == skill.times_successful == 0
    session.add.assert_not_called()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_payload_conflicts_without_counter_changes(monkeypatch):
    prior = SimpleNamespace(id=55, validation_id=2, outcome="success", evidence={"artifact_ids": [3], "source_refs": ["test://different"]})
    skill, session, args = setup_session(monkeypatch, existing=prior)
    with pytest.raises(SkillOutcomeConflict):
        await record_skill_outcome(**args)
    assert skill.times_used == 0
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope,global_flag", [("/foreign", False), (None, False)])
async def test_foreign_and_unowned_procedures_rejected(monkeypatch, scope, global_flag):
    skill, session, args = setup_session(monkeypatch, scope=scope, is_global=global_flag)
    with pytest.raises(PermissionError):
        await record_skill_outcome(**args)
    assert skill.times_used == 0
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_counts_use_without_success(monkeypatch):
    skill, session, args = setup_session(monkeypatch, validation_status="fail")
    args["outcome"] = "failure"
    await record_skill_outcome(**args)
    assert (skill.times_used, skill.times_successful) == (1, 0)


@pytest.mark.asyncio
async def test_unlinked_evidence_cannot_record_success(monkeypatch):
    skill, session, args = setup_session(monkeypatch)
    args["evidence"]["source_refs"] = ["invented://claim"]
    with pytest.raises(ValueError, match="registered task artifacts"):
        await record_skill_outcome(**args)
    assert skill.times_used == 0
    session.commit.assert_not_awaited()
