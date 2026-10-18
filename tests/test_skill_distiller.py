"""L3 → L4 distillation: repeated procedural learnings become pending skills."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from brain.memory import skill_distiller
from brain.memory.skill_distiller import _parse_skill_json, distill_skills
from brain.memory.skill_store import SkillConflictError


def _learning(lid, statement, category="workflow", scope="/repo"):
    return SimpleNamespace(id=lid, statement=statement, category=category, repo_scope=scope, status="active")


_GOOD = json.dumps({
    "name": "Release to VPS",
    "description": "How a release reaches the VPS without touching reports/.",
    "triggers": ["deploy", "release", "server_up"],
    "workflow": [{"step": 1, "action": "tar --exclude=reports"}, {"step": 2, "action": "run server_up.sh"}],
    "confidence": 0.8,
    "procedural": True,
})


def test_parse_skill_json_shapes_and_rejects():
    parsed = _parse_skill_json("noise " + _GOOD + " trailing")
    assert parsed["name"] == "Release to VPS"
    assert parsed["triggers"] == ["deploy", "release", "server_up"]
    assert parsed["workflow"] == [{"step": 1, "action": "tar --exclude=reports"},
                                  {"step": 2, "action": "run server_up.sh"}]
    assert parsed["confidence"] == 0.8
    assert _parse_skill_json("not json") is None
    assert _parse_skill_json(json.dumps({"name": "x", "description": "y", "workflow": ["a"],
                                         "procedural": False})) is None
    assert _parse_skill_json(json.dumps({"name": "x", "description": "y", "workflow": []})) is None
    loose = _parse_skill_json(json.dumps({"name": "x", "description": "y", "workflow": ["do a", "do b"],
                                          "confidence": "bad"}))
    assert loose["workflow"][1] == {"step": 2, "action": "do b"} and loose["confidence"] == 0.5


@pytest.mark.asyncio
async def test_distill_creates_pending_skill_per_repo_group(monkeypatch):
    groups = {"/repo": [_learning(1, "exclude reports/ from tar"), _learning(2, "run server_up.sh"),
                        _learning(3, "api unhealthy during pg_dump is normal", category="repo")]}
    created = []

    async def create_skill(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(id=10, name=kwargs["name"], status=kwargs["status"],
                               source_learning_ids=kwargs["source_learning_ids"])

    monkeypatch.setattr(skill_distiller, "_eligible_groups", AsyncMock(return_value=groups))
    monkeypatch.setattr(skill_distiller, "get_summarizer_provider",
                        lambda: SimpleNamespace(summarize=AsyncMock(return_value=_GOOD)))
    monkeypatch.setattr(skill_distiller.settings, "MEMORY_SKILL_DISTILLATION_REQUIRE_APPROVAL", True)
    with patch("brain.memory.skill_store.create_skill", new=create_skill):
        report = await distill_skills(min_learnings=3)
    assert report["status"] == "ok" and report["groups"] == 1
    assert report["created"] == [{"skill_id": 10, "name": "Release to VPS", "status": "pending",
                                  "repo_scope": "/repo", "source_learning_ids": [1, 2, 3]}]
    assert created[0]["repo_scope"] == "/repo" and created[0]["is_global"] is False
    assert created[0]["status"] == "pending"
    assert created[0]["source_episode_ids"] == []


@pytest.mark.asyncio
async def test_distill_active_without_approval_and_skips_non_procedural(monkeypatch):
    groups = {"/a": [_learning(1, "x"), _learning(2, "y")],
              "/b": [_learning(3, "p"), _learning(4, "q")]}
    answers = {"[1]": json.dumps({"name": "n", "description": "d", "workflow": ["s"], "procedural": False})}

    async def summarize(prompt, **_):
        for key, raw in answers.items():
            if key in prompt:
                return raw
        return _GOOD

    create = AsyncMock(return_value=SimpleNamespace(id=5, name="Release to VPS", status="active",
                                                    source_learning_ids=[3, 4]))
    monkeypatch.setattr(skill_distiller, "_eligible_groups", AsyncMock(return_value=groups))
    monkeypatch.setattr(skill_distiller, "get_summarizer_provider", lambda: SimpleNamespace(summarize=summarize))
    monkeypatch.setattr(skill_distiller.settings, "MEMORY_SKILL_DISTILLATION_REQUIRE_APPROVAL", False)
    with patch("brain.memory.skill_store.create_skill", new=create):
        report = await distill_skills(min_learnings=2)
    assert [c["repo_scope"] for c in report["created"]] == ["/b"]
    assert create.await_args.kwargs["status"] == "active"
    assert report["skipped"] == [{"repo_scope": "/a", "reason": "not procedural or unparsable"}]


@pytest.mark.asyncio
async def test_distill_name_conflict_merges_learnings_into_existing_skill(monkeypatch):
    groups = {"/repo": [_learning(7, "x"), _learning(8, "y")]}
    merge = AsyncMock(return_value=True)
    monkeypatch.setattr(skill_distiller, "_eligible_groups", AsyncMock(return_value=groups))
    monkeypatch.setattr(skill_distiller, "_attach_learnings_to_existing", merge)
    monkeypatch.setattr(skill_distiller, "get_summarizer_provider",
                        lambda: SimpleNamespace(summarize=AsyncMock(return_value=_GOOD)))
    with patch("brain.memory.skill_store.create_skill", new=AsyncMock(side_effect=SkillConflictError("dup"))):
        report = await distill_skills(min_learnings=2)
    assert report["created"] == []
    assert report["skipped"][0]["reason"] == "skill 'Release to VPS' exists"
    assert report["skipped"][0]["merged"] is True
    merge.assert_awaited_once_with("Release to VPS", [7, 8])


@pytest.mark.asyncio
async def test_distill_no_groups_makes_no_llm_call(monkeypatch):
    monkeypatch.setattr(skill_distiller, "_eligible_groups", AsyncMock(return_value={}))

    def no_llm():
        raise AssertionError("summarizer must not be built")

    monkeypatch.setattr(skill_distiller, "get_summarizer_provider", no_llm)
    report = await distill_skills()
    assert report == {"status": "ok", "groups": 0, "created": [], "skipped": []}
    # Default threshold comes from settings and is never below 2.
    monkeypatch.setattr(skill_distiller.settings, "MEMORY_SKILL_DISTILLATION_MIN_LEARNINGS", 1)
    eligible = AsyncMock(return_value={})
    monkeypatch.setattr(skill_distiller, "_eligible_groups", eligible)
    await distill_skills()
    eligible.assert_awaited_with(2)


def _authored(lid, author, evidence_authors=()):
    learning = _learning(lid, f"step {lid}")
    learning.author_principal_id = author
    learning.evidence = [{"event_id": lid * 10 + i, "author_principal_id": a} for i, a in enumerate(evidence_authors)]
    return learning


@pytest.mark.asyncio
@pytest.mark.parametrize("team_state, sources, expected_status, expected_author", [
    # Solo state is unchanged: approval off means active, whoever wrote the learnings.
    ("solo", [_authored(1, 11), _authored(2, 11)], "active", 11),
    # Team state: one person's learnings wait for a reviewer, authored by that person.
    ("team", [_authored(1, 11), _authored(2, 11, (11, 11))], "pending", 11),
    # Unattributed learnings in a team never auto-activate either.
    ("team", [_authored(1, None), _authored(2, None)], "pending", None),
    # Two distinct principals (author or evidence) meet the team floor.
    ("team", [_authored(1, 11), _authored(2, 11, (12,))], "active", None),
])
async def test_distill_team_state_gates_on_distinct_principals(monkeypatch, team_state, sources,
                                                               expected_status, expected_author):
    created = []

    async def create_skill(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(id=10, name=kwargs["name"], status=kwargs["status"],
                               source_learning_ids=kwargs["source_learning_ids"])

    monkeypatch.setattr(skill_distiller, "_eligible_groups", AsyncMock(return_value={"/repo": sources}))
    monkeypatch.setattr(skill_distiller, "get_summarizer_provider",
                        lambda: SimpleNamespace(summarize=AsyncMock(return_value=_GOOD)))
    monkeypatch.setattr(skill_distiller.settings, "MEMORY_SKILL_DISTILLATION_REQUIRE_APPROVAL", False)
    monkeypatch.setattr(skill_distiller.settings, "MEMORY_CONSOLIDATION_MIN_DISTINCT_PRINCIPALS_TEAM", 0)
    with patch("brain.memory.skill_store.create_skill", new=create_skill):
        report = await distill_skills(min_learnings=2, team_state=team_state)
    assert created[0]["status"] == expected_status
    assert report["created"][0]["status"] == expected_status
    assert created[0]["author_principal_id"] == expected_author


@pytest.mark.asyncio
async def test_distill_resolves_team_state_when_not_given(monkeypatch):
    created = []

    async def create_skill(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(id=10, name=kwargs["name"], status=kwargs["status"],
                               source_learning_ids=kwargs["source_learning_ids"])

    monkeypatch.setattr(skill_distiller, "_eligible_groups",
                        AsyncMock(return_value={"/repo": [_authored(1, 11), _authored(2, 11)]}))
    monkeypatch.setattr(skill_distiller, "get_summarizer_provider",
                        lambda: SimpleNamespace(summarize=AsyncMock(return_value=_GOOD)))
    monkeypatch.setattr(skill_distiller.settings, "MEMORY_SKILL_DISTILLATION_REQUIRE_APPROVAL", False)
    monkeypatch.setattr("brain.memory.consolidation._memory_team_state", AsyncMock(return_value="team"))
    with patch("brain.memory.skill_store.create_skill", new=create_skill):
        await distill_skills(min_learnings=2)
    assert created[0]["status"] == "pending"
