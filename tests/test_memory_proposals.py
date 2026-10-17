"""Governed memory (M4): proposal -> independent review -> approved knowledge, plus the personal tier.

Team state only: a non-administrator's decision or rule is a pending proposal that no
retrieval path returns until a Reviewer who is not the author approves it with a
compare-and-swap on the proposal version. Solo state writes approved knowledge directly.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update

from apps.api import auth, team_access
from apps.api.routers import core_memory, memory_review
from brain.context import context_pack_builder
from brain.database.models import Decision, Learning, MemoryProposal, Principal, Rule
from brain.memory import decision_store, learning_store, proposals, rule_store
from brain.memory.learning_store import LearningStore
from brain.team import provisioning
from brain.team.models import TeamMember
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
    for module in (auth, team_access, proposals, decision_store, rule_store, learning_store, context_pack_builder):
        monkeypatch.setattr(module, "async_session_factory", factory)
    monkeypatch.setattr(team_access, "current_team_state", live_team_state)
    monkeypatch.setattr("apps.api.request_observation.observe_http_request", AsyncMock())
    monkeypatch.setattr("brain.llm.get_embedding_provider", _no_embeddings)
    app = FastAPI()
    app.include_router(core_memory.router)
    app.include_router(memory_review.router)
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
        await conn.run_sync(lambda sync: Learning.__table__.create(sync, checkfirst=True))


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
        assert stale.status_code == 409
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
