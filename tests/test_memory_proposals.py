"""Governed memory (M4): proposal -> independent review -> approved knowledge, plus the personal tier.

Team state only: a non-administrator's decision or rule is a pending proposal that no
retrieval path returns until a Reviewer who is not the author approves it with a
compare-and-swap on the proposal version. Solo state writes approved knowledge directly.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import func, select, update
from typer.testing import CliRunner

from apps.api import auth, local_memory, team_access
from apps.api.routers import core_memory, memory_review, team_me
from apps.cli.commands import decision as decision_cmd
from apps.cli.commands import rules as rules_cmd
from apps.cli.main import cli
from apps.mcp_server import remote_server
from apps.mcp_server import server as stdio_server
from apps.mcp_server.protocol import only_active
from brain.config.settings import settings
from brain.context import context_pack_builder
from brain.context.runtime_context_builder import RuntimeContextBuilder
from brain.database.models import Decision, Learning, MemoryEpisode, MemoryProposal, MemorySkill, Principal, Rule
from brain.memory import (decision_store, learning_retrieval, learning_store, proposals, relevance, rule_store,
                          skill_store)
from brain.memory.learning_store import LearningStore
from brain.team import provisioning
from brain.team.models import TeamActivityEvent, TeamMember
from tests.test_team_foundations import team_db as team_fixture
from tests.test_team_transport import client_for, live_team_state, membership_driven, revoke_everyone

team_db = team_fixture
pytestmark = pytest.mark.team_state

READER, CONTRIBUTOR, REVIEWER, ADMIN = ({"X-API-Key": f"fixture-{i}"} for i in (1, 2, 3, 4))
REPO = "/synthetic/A"


def _no_embeddings():
    raise RuntimeError("no embedding provider in tests")


def review_app(factory, monkeypatch):
    """The memory routes against the fixture database, with the live team-state probe."""
    membership_driven(monkeypatch)
    for module in (auth, team_access, proposals, decision_store, rule_store, learning_store, context_pack_builder,
                   learning_retrieval, relevance, skill_store):
        monkeypatch.setattr(module, "async_session_factory", factory)
    monkeypatch.setattr(team_access, "current_team_state", live_team_state)
    monkeypatch.setattr("apps.api.request_observation.observe_http_request", AsyncMock())
    monkeypatch.setattr("brain.llm.get_embedding_provider", _no_embeddings)
    app = FastAPI()
    app.include_router(core_memory.router)
    app.include_router(memory_review.router)
    app.include_router(team_me.router)
    return app


def decision(title="Use one queue", repo_path=REPO, **extra):
    return {"title": title, "description": "synthetic decision", "repo_path": repo_path, **extra}


def rule(name="No blocking IO", repo_path=REPO, **extra):
    return {"name": name, "description": "synthetic rule", "repo_path": repo_path, **extra}


async def count(factory, model) -> int:
    async with factory() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def proposal_row(factory, proposal_id) -> MemoryProposal:
    async with factory() as session:
        row = await session.get(MemoryProposal, proposal_id)
        assert row is not None
        return row


async def learning_table(factory):
    """memory_learnings has a vector column: the team Postgres fixture role has no pgvector, SQLite does fine."""
    engine = factory.kw["bind"]
    if engine.dialect.name != "sqlite":
        pytest.skip("memory_learnings needs pgvector; the personal tier is covered on SQLite")
    async with engine.begin() as conn:
        for model in (Learning, MemoryEpisode, MemorySkill):  # the activity feed reads every memory table
            await conn.run_sync(lambda sync, table=model.__table__: table.create(sync, checkfirst=True))


@pytest.mark.asyncio
async def test_member_writes_are_draft_isolated_until_an_independent_reviewer_approves(team_db, monkeypatch):
    factory, _, _ = team_db
    app = review_app(factory, monkeypatch)
    async with client_for(app) as client:
        made = await client.post("/decisions", json=decision(), headers=CONTRIBUTOR)
        assert made.status_code == 202 and made.json()["status"] == "proposed"
        proposed_rule = await client.post("/rules", json=rule(), headers=CONTRIBUTOR)
        assert proposed_rule.status_code == 202
        # No retrieval path sees a draft: the canonical tables, the list routes and the context-pack loader.
        assert await count(factory, Decision) == 0 and await count(factory, Rule) == 0
        # A team administrator's listing is unfiltered, so an empty one means no draft leaked into it.
        assert (await client.get("/decisions", headers=ADMIN)).json() == []
        assert (await client.get("/rules", headers=ADMIN)).json() == []
        assert await context_pack_builder._load_active_normative_memory(REPO) == ([], [])

        queue = (await client.get("/memory/proposals", params={"repo_path": REPO}, headers=REVIEWER)).json()
        assert {p["kind"] for p in queue["proposals"]} == {"decision", "rule"}
        for body in (made.json(), proposed_rule.json()):
            approved = await client.post(f"/memory/proposals/{body['proposal_id']}/approve",
                                         json={"expected_version": body["version"], "reason": "looks right"},
                                         headers=REVIEWER)
            assert approved.status_code == 200, approved.text
            assert approved.json()["state"] == "approved" and approved.json()["reviewed_by_principal_id"] == 3

        decisions = (await client.get("/decisions", headers=ADMIN)).json()
        assert [d["title"] for d in decisions] == ["Use one queue"]
        rules, found = await context_pack_builder._load_active_normative_memory(REPO)
        assert len(rules) == 1 and len(found) == 1
    # Provenance: the author stays the contributor, the proposal row keeps who approved it and why.
    async with factory() as session:
        stored = (await session.execute(select(Decision))).scalar_one()
        assert stored.author_principal_id == 2
    row = await proposal_row(factory, made.json()["proposal_id"])
    assert row.decision_reason == "looks right" and row.result_key == str(stored.id) and row.decided_at


@pytest.mark.asyncio
async def test_self_approval_is_refused_and_readers_cannot_propose_or_review(team_db, monkeypatch):
    factory, _, _ = team_db
    app = review_app(factory, monkeypatch)
    async with client_for(app) as client:
        own = (await client.post("/decisions/proposals", json=decision(), headers=REVIEWER)).json()
        refused = await client.post(f"/memory/proposals/{own['proposal_id']}/approve",
                                    json={"expected_version": 1}, headers=REVIEWER)
        assert refused.status_code == 403 and "independent" in refused.json()["detail"].lower()
        assert (await client.post(f"/memory/proposals/{own['proposal_id']}/reject",
                                  json={"expected_version": 1}, headers=REVIEWER)).status_code == 403

        # Reader: no proposals, no queue, no decisions.
        assert (await client.post("/decisions", json=decision("Reader"), headers=READER)).status_code == 403
        assert (await client.post("/decisions/proposals", json=decision("Reader"), headers=READER)).status_code == 403
        assert (await client.post("/rules/proposals", json=rule(), headers=READER)).status_code == 403
        assert (await client.get("/memory/proposals", params={"repo_path": REPO}, headers=READER)).status_code == 403
        assert (await client.post(f"/memory/proposals/{own['proposal_id']}/approve",
                                  json={"expected_version": 1}, headers=READER)).status_code == 403
        # A contributor proposes but is not a reviewer; a member who may not review cannot see the proposal.
        assert (await client.get("/memory/proposals", params={"repo_path": REPO},
                                 headers=CONTRIBUTOR)).status_code == 403
        assert (await client.post(f"/memory/proposals/{own['proposal_id']}/approve",
                                  json={"expected_version": 1}, headers=CONTRIBUTOR)).status_code == 403
        assert (await client.get(f"/memory/proposals/{own['proposal_id']}", headers=CONTRIBUTOR)).status_code == 404
        mine = (await client.get("/memory/proposals", params={"mine": "true"}, headers=REVIEWER)).json()
        assert [p["id"] for p in mine["proposals"]] == [own["proposal_id"]]
        # The author can still take it back.
        withdrawn = await client.post(f"/memory/proposals/{own['proposal_id']}/withdraw",
                                      json={"expected_version": 1}, headers=REVIEWER)
        assert withdrawn.status_code == 200 and withdrawn.json()["state"] == "withdrawn"
    assert await count(factory, Decision) == 0


@pytest.mark.asyncio
async def test_stale_versions_get_409_and_never_publish(team_db, monkeypatch):
    factory, _, _ = team_db
    app = review_app(factory, monkeypatch)
    async with client_for(app) as client:
        first = (await client.post("/decisions", json=decision(), headers=CONTRIBUTOR)).json()
        second = (await client.post("/decisions", json=decision(reason="competing edit"), headers=CONTRIBUTOR)).json()
        assert first["base_version"] == second["base_version"] == 0

        stale = await client.post(f"/memory/proposals/{first['proposal_id']}/approve",
                                  json={"expected_version": 2}, headers=REVIEWER)
        assert stale.status_code == 409, stale.text
        assert (await client.post(f"/memory/proposals/{first['proposal_id']}/approve",
                                  json={"expected_version": 1}, headers=REVIEWER)).status_code == 200
        # Deciding twice is a conflict, not a second write.
        assert (await client.post(f"/memory/proposals/{first['proposal_id']}/reject",
                                  json={"expected_version": 2}, headers=REVIEWER)).status_code == 409
        # The second proposal was based on version 0; the decision is now at version 1.
        outdated = await client.post(f"/memory/proposals/{second['proposal_id']}/approve",
                                     json={"expected_version": 1}, headers=REVIEWER)
        assert outdated.status_code == 409
        assert (await proposal_row(factory, second["proposal_id"])).state == "pending"
        # A proposal against an already stale read fails fast.
        assert (await client.post("/decisions", json=decision(expected_version=0),
                                  headers=CONTRIBUTOR)).status_code == 409
        # An edit to an approved item is itself a proposal, based on the current version.
        edit = (await client.post("/decisions", json=decision(description="revised", expected_version=1),
                                  headers=CONTRIBUTOR)).json()
        assert edit["status"] == "proposed" and edit["base_version"] == 1 and edit["target_key"]
        assert (await client.post(f"/memory/proposals/{edit['proposal_id']}/approve",
                                  json={"expected_version": 1}, headers=REVIEWER)).status_code == 200
    async with factory() as session:
        versions = sorted((await session.execute(select(Decision.version))).scalars().all())
    assert versions[-1] == 2


@pytest.mark.asyncio
async def test_system_global_items_are_approved_by_team_administrators_only(team_db, monkeypatch):
    factory, _, _ = team_db
    app = review_app(factory, monkeypatch)
    async with client_for(app) as client:
        global_item = await client.post("/decisions", json=decision("Global", repo_path=None), headers=CONTRIBUTOR)
        assert global_item.status_code == 202
        body = global_item.json()
        assert (await client.post(f"/memory/proposals/{body['proposal_id']}/approve",
                                  json={"expected_version": 1}, headers=REVIEWER)).status_code == 403
        assert (await client.get("/memory/proposals", params={"scope": "system_global"},
                                 headers=REVIEWER)).status_code == 403
        queue = (await client.get("/memory/proposals", params={"scope": "system_global"}, headers=ADMIN)).json()
        assert [p["id"] for p in queue["proposals"]] == [body["proposal_id"]]
        assert (await client.post(f"/memory/proposals/{body['proposal_id']}/approve",
                                  json={"expected_version": 1}, headers=ADMIN)).status_code == 200
        # A team administrator still writes directly, but never approves their own proposal.
        assert (await client.post("/decisions", json=decision("Admin direct", repo_path=None),
                                  headers=ADMIN)).status_code == 200
        own = (await client.post("/decisions/proposals", json=decision("Admin draft", repo_path=None),
                                 headers=ADMIN)).json()
        assert (await client.post(f"/memory/proposals/{own['proposal_id']}/approve",
                                  json={"expected_version": 1}, headers=ADMIN)).status_code == 403
    async with factory() as session:
        titles = set((await session.execute(select(Decision.title))).scalars().all())
    assert titles == {"Global", "Admin direct"}


@pytest.mark.asyncio
async def test_revoked_author_pending_proposals_are_withdrawn_and_cannot_be_approved(team_db, monkeypatch):
    factory, _, _ = team_db
    app = review_app(factory, monkeypatch)
    async with client_for(app) as client:
        approved = (await client.post("/decisions", json=decision("Kept"), headers=CONTRIBUTOR)).json()
        assert (await client.post(f"/memory/proposals/{approved['proposal_id']}/approve",
                                  json={"expected_version": 1}, headers=REVIEWER)).status_code == 200
        pending = (await client.post("/decisions", json=decision("Pending"), headers=CONTRIBUTOR)).json()
        orphan = (await client.post("/rules", json=rule("Orphan"), headers=CONTRIBUTOR)).json()

    # Membership lost by any route: approval refuses to publish on behalf of a non-member.
    async with factory() as session, session.begin():
        await session.execute(update(TeamMember).where(TeamMember.principal_id == 2)
                              .values(revoked_at=datetime.now(timezone.utc)))
    async with client_for(app) as client:
        refused = await client.post(f"/memory/proposals/{orphan['proposal_id']}/approve",
                                    json={"expected_version": 1}, headers=REVIEWER)
        assert refused.status_code == 409 and "team access" in refused.json()["detail"]

    # brain team revoke withdraws every pending proposal of the author in the same transaction.
    async with factory() as session, session.begin():
        principal = await session.get(Principal, 2)
        await provisioning.revoke_member(session, principal=principal)
    for proposal_id in (pending["proposal_id"], orphan["proposal_id"]):
        row = await proposal_row(factory, proposal_id)
        assert row.state == "withdrawn" and row.decision_reason == proposals.REVOKED_AUTHOR_REASON
    async with client_for(app) as client:
        assert (await client.post(f"/memory/proposals/{pending['proposal_id']}/approve",
                                  json={"expected_version": 2}, headers=REVIEWER)).status_code == 409
    async with factory() as session:
        assert set((await session.execute(select(Decision.title))).scalars().all()) == {"Kept"}
    assert await count(factory, Rule) == 0


@pytest.mark.asyncio
async def test_solo_state_writes_approved_knowledge_directly(team_db, monkeypatch):
    factory, _, _ = team_db
    app = review_app(factory, monkeypatch)
    await revoke_everyone(factory)
    assert await live_team_state() == "solo"
    async with client_for(app) as client:
        made = await client.post("/decisions", json=decision(), headers=CONTRIBUTOR)
        assert made.status_code == 200 and made.json()["status"] != "proposed"
        assert (await client.post("/rules", json=rule(), headers=CONTRIBUTOR)).status_code == 200
    assert await count(factory, Decision) == 1 and await count(factory, Rule) == 1
    assert await count(factory, MemoryProposal) == 0


@pytest.mark.asyncio
async def test_personal_learnings_stay_private_until_a_promotion_is_approved(team_db, monkeypatch):
    factory, _, _ = team_db
    await learning_table(factory)
    app = review_app(factory, monkeypatch)
    note = {"statement": "Prefer the fixture factory", "repo_scope": REPO, "visibility": "personal"}
    async with client_for(app) as client:
        made = await client.post("/learnings", json=note, headers=CONTRIBUTOR)
        assert made.status_code == 200 and made.json()["visibility"] == "personal"
        learning_id = made.json()["learning_id"]
        # Never shared retrieval, never anyone else's personal view.
        assert await LearningStore.list_active_learnings(REPO) == []
        assert (await client.get("/learnings", params={"repo_path": REPO}, headers=REVIEWER)).json() == []
        personal = {"repo_path": REPO, "visibility": "personal"}
        assert [r["id"] for r in (await client.get("/learnings", params=personal,
                                                   headers=CONTRIBUTOR)).json()] == [learning_id]
        assert (await client.get("/learnings", params=personal, headers=REVIEWER)).json() == []
        assert (await client.post(f"/learnings/{learning_id}/promote", headers=REVIEWER)).status_code == 404

        promoted = await client.post(f"/learnings/{learning_id}/promote", headers=CONTRIBUTOR)
        assert promoted.status_code == 202 and promoted.json()["kind"] == "learning"
        assert (await client.post(f"/learnings/{learning_id}/promote", headers=CONTRIBUTOR)).status_code == 409
        assert await LearningStore.list_active_learnings(REPO) == []
        assert (await client.post(f"/memory/proposals/{promoted.json()['proposal_id']}/approve",
                                  json={"expected_version": 1}, headers=REVIEWER)).status_code == 200
        assert [lrng.id for lrng in await LearningStore.list_active_learnings(REPO)] == [learning_id]
        assert (await client.get("/learnings", params=personal, headers=CONTRIBUTOR)).json() == []

        # A Reader may keep personal notes but cannot offer them to shared memory.
        reader_note = await client.post("/learnings", json=note, headers=READER)
        assert reader_note.status_code == 200
        assert (await client.post(f"/learnings/{reader_note.json()['learning_id']}/promote",
                                  headers=READER)).status_code == 403


async def memory_events(factory) -> list[tuple[str, int | None, int | None]]:
    """(kind, actor, repository) of every ``memory.*`` feed row, oldest first."""
    async with factory() as session:
        rows = (await session.execute(select(TeamActivityEvent).where(TeamActivityEvent.kind.like("memory.%"))
                                      .order_by(TeamActivityEvent.id))).scalars().all()
    return [(row.kind, row.actor_principal_id, row.repository_id) for row in rows]


async def pending_for(client, headers) -> int:
    me = await client.get("/team/me", headers=headers)
    assert me.status_code == 200, me.text
    return me.json()["pending_review_count"]


@pytest.mark.asyncio
async def test_review_lifecycle_reaches_the_feed_and_the_pending_count(team_db, monkeypatch):
    factory, _, _ = team_db
    await learning_table(factory)
    app = review_app(factory, monkeypatch)
    async with client_for(app) as client:
        assert [await pending_for(client, h) for h in (READER, CONTRIBUTOR, REVIEWER, ADMIN)] == [0, 0, 0, 0]
        made = (await client.post("/decisions", json=decision(), headers=CONTRIBUTOR)).json()
        doomed = (await client.post("/rules", json=rule(), headers=CONTRIBUTOR)).json()
        # Reviewer of the project and team administrator may decide; the author and a Reader may not.
        assert [await pending_for(client, h) for h in (READER, CONTRIBUTOR, REVIEWER, ADMIN)] == [0, 0, 2, 2]

        assert (await client.post(f"/memory/proposals/{made['proposal_id']}/approve",
                                  json={"expected_version": 1}, headers=REVIEWER)).status_code == 200
        assert (await client.post(f"/memory/proposals/{doomed['proposal_id']}/reject",
                                  json={"expected_version": 1, "reason": "duplicate"},
                                  headers=REVIEWER)).status_code == 200
        assert await pending_for(client, REVIEWER) == 0

        assert await memory_events(factory) == [
            ("memory.proposed", 2, 1), ("memory.proposed", 2, 1),
            ("memory.approved", 3, 1), ("memory.rejected", 3, 1),
        ]
        # The repository's readers see the review trail; the events name the proposal, not its draft.
        feed = (await client.get("/team/activity", params={"kind": "memory"}, headers=READER)).json()
        assert sorted(event["kind"] for event in feed["events"]) == [
            "memory.approved", "memory.proposed", "memory.proposed", "memory.rejected"]
        assert all("No blocking IO" not in event["summary"] for event in feed["events"])
        assert {event["details"]["proposal_id"] for event in feed["events"]} == {made["proposal_id"],
                                                                               doomed["proposal_id"]}

        # A system-global proposal is administrator business: in the admin feed, not in a member's.
        global_item = (await client.post("/decisions", json=decision("Global", repo_path=None),
                                         headers=CONTRIBUTOR)).json()
        assert await pending_for(client, REVIEWER) == 0 and await pending_for(client, ADMIN) == 1
        member_feed = (await client.get("/team/activity", params={"kind": "memory.proposed"},
                                        headers=READER)).json()
        admin_feed = (await client.get("/team/activity", params={"kind": "memory.proposed"},
                                       headers=ADMIN)).json()
        assert global_item["proposal_id"] not in {e["details"]["proposal_id"] for e in member_feed["events"]}
        assert global_item["proposal_id"] in {e["details"]["proposal_id"] for e in admin_feed["events"]}


@pytest.mark.asyncio
async def test_approved_items_record_author_and_approver(team_db, monkeypatch):
    factory, _, _ = team_db
    await learning_table(factory)
    app = review_app(factory, monkeypatch)
    async with client_for(app) as client:
        made = (await client.post("/decisions", json=decision(), headers=CONTRIBUTOR)).json()
        ruled = (await client.post("/rules", json=rule(), headers=CONTRIBUTOR)).json()
        shared = await client.post("/learnings", json={"statement": "Seed with the factory", "repo_scope": REPO,
                                                       "visibility": "shared"}, headers=CONTRIBUTOR)
        assert shared.status_code == 202 and shared.json()["status"] == "proposed"
        for body in (made, ruled, shared.json()):
            approved = await client.post(f"/memory/proposals/{body['proposal_id']}/approve",
                                         json={"expected_version": body["version"]}, headers=REVIEWER)
            assert approved.status_code == 200, approved.text
        # A team administrator writes directly: nobody approved it, so no approver is recorded.
        assert (await client.post("/decisions", json=decision("Admin direct", repo_path="/synthetic/B"), headers=ADMIN)).status_code == 200
    async with factory() as session:
        decisions = {d.title: d for d in (await session.execute(select(Decision))).scalars()}
        stored_rule = (await session.execute(select(Rule))).scalar_one()
        learning = await session.get(Learning, shared.json()["learning_id"])
    reviewed, direct = decisions["Use one queue"], decisions["Admin direct"]
    assert (reviewed.author_principal_id, reviewed.approved_by_principal_id) == (2, 3)
    assert (direct.author_principal_id, direct.approved_by_principal_id) == (4, None)
    assert (stored_rule.author_principal_id, stored_rule.approved_by_principal_id) == (2, 3)
    assert learning.status == "active"
    assert (learning.author_principal_id, learning.approved_by_principal_id) == (2, 3)


@pytest.mark.asyncio
async def test_member_learnings_default_to_personal_and_skills_wait_for_review(team_db, monkeypatch):
    factory, _, _ = team_db
    await learning_table(factory)
    app = review_app(factory, monkeypatch)
    note = {"statement": "Prefer the fixture factory", "repo_scope": REPO}
    skill = {"name": "Seed the fixture", "description": "How tests seed data", "repo_scope": REPO,
             "workflow": [{"step": 1, "action": "use the factory"}]}
    async with client_for(app) as client:
        member = await client.post("/learnings", json=note, headers=CONTRIBUTOR)
        assert member.status_code == 200 and member.json()["visibility"] == "personal"
        # A Reader may keep a personal note but cannot ask for shared memory.
        assert (await client.post("/learnings", json={**note, "visibility": "shared"},
                                  headers=READER)).status_code == 403
        admin = await client.post("/learnings", json={**note, "repo_scope": "/synthetic/B"}, headers=ADMIN)
        assert admin.status_code == 200 and admin.json()["visibility"] == "shared"
        assert await LearningStore.list_active_learnings(REPO) == []
        assert [lrng.id for lrng in await LearningStore.list_active_learnings("/synthetic/B")] == [
            admin.json()["learning_id"]]

        pending = await client.post("/skills", json=skill, headers=CONTRIBUTOR)
        assert pending.status_code == 202 and pending.json()["status"] == "pending_review"
        assert await skill_store.select_skills_for_context("seed the fixture", REPO, max_count=3) == []
        approved = await client.post(f"/skills/{pending.json()['id']}/approve", headers=ADMIN)
        assert approved.status_code == 200 and approved.json()["status"] == "active"
        assert (await client.post("/skills", json={**skill, "name": "Admin skill", "repo_scope": "/synthetic/B"},
                                  headers=ADMIN)).json()["status"] == "created"
    async with factory() as session:
        stored = await session.get(MemorySkill, pending.json()["id"])
    assert (stored.author_principal_id, stored.approved_by_principal_id) == (2, 4)


@pytest.mark.asyncio
async def test_personal_and_draft_memory_never_reach_other_members(team_db, monkeypatch):
    """Regression: ask/context retrieval, the MCP resources and the feed only ever see shared memory."""
    factory, _, _ = team_db
    await learning_table(factory)
    app = review_app(factory, monkeypatch)
    task = "prefer the fixture factory when seeding queue data"

    async def runtime_memory() -> dict:
        # The memory half of every ask and context response (RuntimeContextBuilder.build).
        return await RuntimeContextBuilder._load_memory(task, SimpleNamespace(candidates=[]), {"path": REPO},
                                                        8000, {"missing": []})

    async with client_for(app) as client:
        private = await client.post("/learnings", json={"statement": "Prefer the fixture factory for queue data",
                                                        "repo_scope": REPO}, headers=CONTRIBUTOR)
        assert private.json()["visibility"] == "personal"
        draft = await client.post("/learnings", json={"statement": "Seed queue data with the fixture factory",
                                                      "repo_scope": REPO, "visibility": "shared"},
                                  headers=CONTRIBUTOR)
        assert draft.status_code == 202
        assert (await client.post("/decisions", json=decision("Prefer the fixture factory queue"),
                                  headers=CONTRIBUTOR)).status_code == 202

        assert "fixture factory" not in str(await runtime_memory()).lower()
        assert await learning_retrieval.select_learnings_for_context(task, REPO) == []
        assert await context_pack_builder._load_active_normative_memory(REPO) == ([], [])
        for headers in (READER, REVIEWER, ADMIN):
            # The MCP resources active_rules / active_decisions are these lists through only_active.
            assert only_active((await client.get("/decisions", headers=headers)).json()) == []
            assert only_active((await client.get("/rules", headers=headers)).json()) == []
            # The administrator is not a member of project A: the unscoped (global) list is theirs to read.
            params = {} if headers is ADMIN else {"repo_path": REPO}
            assert (await client.get("/learnings", params=params, headers=headers)).json() == []
            assert (await client.get("/learnings", params={**params, "visibility": "personal"},
                                     headers=headers)).json() == []
            feed = (await client.get("/team/activity", headers=headers)).json()
            assert not any(event["kind"].startswith("learning.") for event in feed["events"])
            assert "fixture factory" not in str(feed["events"]).lower()

        # Positive control: once approved, the same learning reaches every member's retrieval.
        assert (await client.post(f"/memory/proposals/{draft.json()['proposal_id']}/approve",
                                  json={"expected_version": 1}, headers=REVIEWER)).status_code == 200
        found = await learning_retrieval.select_learnings_for_context(task, REPO)
        assert [item["id"] for item in found] == [draft.json()["learning_id"]]
        assert "seed queue data" in str(await runtime_memory()).lower()
        # The personal note is still the author's alone.
        assert private.json()["learning_id"] not in {item["id"] for item in found}


def local_key(monkeypatch, headers) -> None:
    """Run the local surfaces (stdio MCP, CLI) as the credential behind ``headers``."""
    monkeypatch.setattr(settings, "BRAIN_LOCAL_PRINCIPAL_KEY", headers["X-API-Key"] if headers else "")


@pytest.mark.asyncio
async def test_local_surfaces_follow_team_governance_as_the_configured_principal(team_db, monkeypatch):
    factory, _, _ = team_db
    review_app(factory, monkeypatch)

    # A contributor's key: the stdio tools file proposals exactly like POST /decisions and /rules.
    local_key(monkeypatch, CONTRIBUTOR)
    made = await stdio_server.record_decision(title="Use one queue", repo_path=REPO, description="synthetic")
    ruled = await stdio_server.record_rule(name="No blocking IO", repo_path=REPO, description="synthetic")
    assert made["status"] == "proposed" and ruled["status"] == "proposed"
    assert await count(factory, Decision) == 0 and await count(factory, Rule) == 0
    assert (await proposal_row(factory, made["proposal_id"])).author_principal_id == 2
    # Bulk operator commands (rules sync, decision deprecate, import) write directly: refused.
    with pytest.raises(local_memory.LocalPrincipalError, match="team administrator"):
        await local_memory.require_local_administrator("rules sync")

    # A Reader may not propose; a key that does not resolve fails closed instead of acting as the operator.
    local_key(monkeypatch, READER)
    assert (await stdio_server.record_decision(title="Reader", repo_path=REPO))["status"] == "denied"
    local_key(monkeypatch, {"X-API-Key": "pbk_not_a_credential"})
    unknown = await stdio_server.record_rule(name="Unknown", repo_path=REPO)
    assert unknown["status"] == "denied" and "does not resolve" in unknown["error"]
    with pytest.raises(local_memory.LocalPrincipalError):
        await local_memory.require_local_administrator("rules sync")
    # The shared key is not a team identity once the team has members.
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "synthetic-shared-key")
    local_key(monkeypatch, {"X-API-Key": "synthetic-shared-key"})
    shared = await stdio_server.record_decision(title="Shared", repo_path=REPO)
    assert shared["status"] == "denied" and "named credential" in shared["error"]

    # A team administrator's key writes directly, attributed, and may run bulk commands.
    local_key(monkeypatch, ADMIN)
    direct = await stdio_server.record_decision(title="Admin direct", repo_path=None, description="synthetic")
    assert direct["status"] == "success"
    await local_memory.require_local_administrator("rules sync")
    # Attributed to the key's principal: an operator write would be unattributed.
    async with factory() as session:
        stored = (await session.execute(select(Decision))).scalar_one()
    assert (stored.title, stored.author_principal_id) == ("Admin direct", 4)


@pytest.mark.asyncio
async def test_local_principal_in_solo_state_writes_directly(team_db, monkeypatch):
    factory, _, _ = team_db
    review_app(factory, monkeypatch)
    await revoke_everyone(factory)
    local_key(monkeypatch, CONTRIBUTOR)
    made = await stdio_server.record_decision(title="Use one queue", repo_path=REPO, description="synthetic")
    assert made["status"] == "success"
    await local_memory.require_local_administrator("rules sync")
    assert await count(factory, Decision) == 1 and await count(factory, MemoryProposal) == 0
    # No key: the local surfaces are the operator and the governed path is not taken at all.
    local_key(monkeypatch, None)
    assert await local_memory.local_write("decision", {"title": "Operator"}) is None
    assert await local_memory.local_principal() is None


@pytest.mark.asyncio
async def test_member_key_over_remote_and_native_mcp_cannot_bypass_review(team_db, monkeypatch):
    """Remote MCP and native /mcp call the API with the caller's own key: a member's write is a proposal."""
    factory, _, _ = team_db
    app = review_app(factory, monkeypatch)
    monkeypatch.setattr(remote_server, "_asgi_app", app)  # what apps/api/mcp_http.py installs
    caller = {"key": CONTRIBUTOR["X-API-Key"]}
    monkeypatch.setattr(remote_server, "_caller_credential", lambda: caller["key"])

    made = await remote_server.record_decision(title="Use one queue", repo_path=REPO, description="synthetic")
    ruled = await remote_server.record_rule(name="No blocking IO", repo_path=REPO, description="synthetic")
    assert made["status"] == "proposed" and ruled["status"] == "proposed"
    assert await count(factory, Decision) == 0 and await count(factory, Rule) == 0
    assert {(await proposal_row(factory, body["proposal_id"])).author_principal_id for body in (made, ruled)} == {2}

    caller["key"] = READER["X-API-Key"]
    with pytest.raises(ToolError, match="HTTP 403"):
        await remote_server.record_decision(title="Reader", repo_path=REPO)
    caller["key"] = ADMIN["X-API-Key"]
    assert (await remote_server.record_decision(title="Admin direct", repo_path=None))["status"] == "success"
    assert await count(factory, Decision) == 1


def test_cli_writes_as_the_local_principal_and_refuses_bulk_commands(monkeypatch):
    runner = CliRunner()
    operator_add = AsyncMock(side_effect=AssertionError("the operator store must not be reached"))
    monkeypatch.setattr(decision_cmd.DecisionStore, "add_decision", operator_add)
    monkeypatch.setattr(rules_cmd.RuleStore, "add_rule", operator_add, raising=False)
    monkeypatch.setattr(rules_cmd.RuleStore, "record_rule", operator_add, raising=False)
    sync = AsyncMock(side_effect=AssertionError("refused before any write"))
    monkeypatch.setattr(rules_cmd.RuleStore, "sync_rules_from_yaml", sync)
    monkeypatch.setattr(settings, "BRAIN_LOCAL_PRINCIPAL_KEY", "pbk_synthetic_member")
    governed = AsyncMock(return_value={"status": "proposed", "proposal_id": 9, "version": 1})
    monkeypatch.setattr(local_memory, "local_write", governed)

    added = runner.invoke(cli, ["decision", "add", "Use one queue", "--desc", "synthetic"])
    assert added.exit_code == 0, added.output
    assert "proposal #9" in added.output and "not approved knowledge" in added.output
    assert governed.await_args.args[0] == "decision" and governed.await_args.args[1]["title"] == "Use one queue"
    ruled = runner.invoke(cli, ["rules", "add", "No blocking IO", "--repo", REPO])
    assert ruled.exit_code == 0 and governed.await_args.args[0] == "rule"

    monkeypatch.setattr(local_memory, "local_write",
                        AsyncMock(return_value={"status": "denied", "error": "Reader cannot propose"}))
    denied = runner.invoke(cli, ["decision", "add", "Reader"])
    assert denied.exit_code == 1 and "Reader cannot propose" in denied.output
    operator_add.assert_not_awaited()

    monkeypatch.setattr(local_memory, "require_local_administrator",
                        AsyncMock(side_effect=local_memory.LocalPrincipalError("rules sync needs a team administrator")))
    refused = runner.invoke(cli, ["rules", "sync", "rules.yaml"])
    assert refused.exit_code == 1 and "needs a team administrator" in refused.output
    sync.assert_not_awaited()


async def consolidated_g4_proposal(factory, *, evidence_authors, author=None) -> MemoryProposal:
    """A team-state G4 escalation as _persist_cluster files it: a ``proposed`` episode plus its learning proposal."""
    async with factory() as session, session.begin():
        episode = MemoryEpisode(source_event_ids=[1, 2], distilled_summary="Queue workers must be idempotent",
                                topic="infra", status="proposed", confidence=0.9, repo_scope=REPO,
                                gate_reasons=["G4: high severity needs a human"])
        session.add(episode)
        await session.flush()
        evidence = [{"event_id": i, **({"author_principal_id": who} if who else {})}
                    for i, who in enumerate(evidence_authors, start=1)]
        row = await proposals.add_consolidated_learning_proposal(
            session, statement="Queue workers must be idempotent", category="infra", confidence=0.9,
            evidence=evidence, repo_path=REPO, requested_repo_path=REPO, author_principal_id=author,
            episode_id=episode.id, supersedes=[], severity="high", suggested_target="learning",
            evidence_tier="validated", approval_reason="G4: high severity needs a human")
    return row


@pytest.mark.asyncio
async def test_g4_consolidation_proposals_need_a_reviewer_independent_of_the_evidence(team_db, monkeypatch):
    factory, _, _ = team_db
    await learning_table(factory)
    app = review_app(factory, monkeypatch)
    # Unattributed and the Reviewer's own evidence mixed: filed by the system (no author).
    own = await consolidated_g4_proposal(factory, evidence_authors=[3, None])
    assert own.author_principal_id is None
    assert own.payload["gate"] == "G4" and own.payload["approval_reason"].startswith("G4:")
    other = await consolidated_g4_proposal(factory, evidence_authors=[2, None])
    async with client_for(app) as client:
        queue = (await client.get("/memory/proposals", params={"repo_path": REPO}, headers=REVIEWER)).json()
        assert sorted(p["id"] for p in queue["proposals"]) == [own.id, other.id]
        for verb in ("approve", "reject"):
            refused = await client.post(f"/memory/proposals/{own.id}/{verb}", json={"expected_version": 1},
                                        headers=REVIEWER)
            assert refused.status_code == 403 and "your own evidence" in refused.json()["detail"]
        # Someone else's evidence: the project's Reviewer decides it, with the usual compare-and-swap.
        stale = await client.post(f"/memory/proposals/{other.id}/approve", json={"expected_version": 2},
                                  headers=REVIEWER)
        assert stale.status_code == 409, stale.text
        approved = await client.post(f"/memory/proposals/{other.id}/approve",
                                     json={"expected_version": 1, "reason": "reproduced"}, headers=REVIEWER)
        assert approved.status_code == 200, approved.text
    async with factory() as session:
        learning = (await session.execute(select(Learning))).scalar_one()
        promoted = await session.get(MemoryEpisode, int(other.payload["episode_id"]))
        waiting = await session.get(MemoryEpisode, int(own.payload["episode_id"]))
    assert learning.status == "active" and learning.approved_by_principal_id == 3
    assert learning.author_principal_id is None
    assert promoted.status == "promoted" and promoted.promoted_to_learning_id == learning.id
    assert waiting.status == "proposed"

    # The episode is decided in the queue only: the legacy episode endpoint refuses it.
    second = await consolidated_g4_proposal(factory, evidence_authors=[2, 2], author=2)
    from brain.memory.consolidation import EpisodeStateError, approve_episode

    monkeypatch.setattr("brain.memory.consolidation.async_session_factory", factory)
    with pytest.raises(EpisodeStateError, match="review queue"):
        await approve_episode(int(second.payload["episode_id"]), "ok", principal_id=3, team_state="team")


@pytest.mark.asyncio
async def test_new_proposals_notify_reviewers_without_draft_content(team_db, monkeypatch):
    factory, _, _ = team_db
    app = review_app(factory, monkeypatch)
    deliver = AsyncMock(return_value={"status": "sent"})
    monkeypatch.setattr("brain.alerts.telegram.deliver_memory_proposal_notice", deliver)
    async with client_for(app) as client:
        assert (await client.post("/decisions", json=decision("Secret title"), headers=CONTRIBUTOR)).status_code == 202
        deliver.assert_not_awaited()  # off by default
        monkeypatch.setattr(settings, "MEMORY_REVIEW_TELEGRAM_NOTIFY", True)
        made = await client.post("/rules", json=rule("Secret rule"), headers=CONTRIBUTOR)
        assert made.status_code == 202
        # A failing channel never fails the submission.
        deliver.side_effect = RuntimeError("telegram is down")
        assert (await client.post("/decisions", json=decision("Another"), headers=CONTRIBUTOR)).status_code == 202
    first = deliver.await_args_list[0]
    assert first.kwargs == {"proposal_id": made.json()["proposal_id"], "kind": "rule", "repo_path": REPO}
    assert "Secret" not in repr(deliver.await_args_list)
