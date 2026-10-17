"""``/team/me`` and ``/team/activity``: identity in solo and team state, and a feed whose
scope is decided by the central team-access helpers before any event is read."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import update

from apps.api import auth, team_access
from apps.api.routers import jobs, team, team_me
from brain.auth.principals import hash_credential
from brain.config.settings import settings
from brain.database.models import ApiCredential, Decision, Learning, MemoryEpisode, MemorySkill, Rule
from brain.team import provisioning
from brain.team.activity import record_event
from brain.team.authorization import team_state
from brain.team.models import TeamActivityEvent, TeamInvitation, TeamMember
from tests.test_team_foundations import team_db as team_fixture

team_db = team_fixture
pytestmark = pytest.mark.team_state

MEMORY_TABLES = [Decision.__table__, Rule.__table__, Learning.__table__, MemoryEpisode.__table__,
                 MemorySkill.__table__]
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
NO_TEAM_READ = {"X-API-Key": "fixture-core-only"}
READER, CONTRIBUTOR, ADMIN = ({"X-API-Key": f"fixture-{i}"} for i in (1, 2, 4))


# Same helpers as tests/test_team_transport.py, which also needs the native MCP client.
def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")


async def live_team_state() -> str:
    async with team_access.async_session_factory() as session:
        return await team_state(session)


async def revoke_everyone(factory):
    """Revoke every member and dissolve the team: the operator's explicit return to solo."""
    async with factory() as session, session.begin():
        await session.execute(update(TeamMember).values(revoked_at=datetime.now(timezone.utc)))
        await provisioning.dissolve_team(session)


@pytest_asyncio.fixture
async def activity_db(team_db):
    factory, a, b = team_db
    engine = factory.kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Decision.metadata.create_all(sync, tables=MEMORY_TABLES))
    async with factory() as session, session.begin():
        # Principal 1 again, with a credential that lacks team:read.
        session.add(ApiCredential(id=11, principal_id=1, key_hash=hash_credential("fixture-core-only"),
                                  scopes=["core:read"]))
    yield factory, a, b
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Decision.metadata.drop_all(sync, tables=MEMORY_TABLES))


def activity_app(factory, monkeypatch):
    monkeypatch.setattr(settings, "BRAIN_TEAM_MODE", False)  # team state follows live membership
    for module in (auth, team, team_access):
        monkeypatch.setattr(module, "async_session_factory", factory)
    monkeypatch.setattr(team_access, "current_team_state", live_team_state)
    monkeypatch.setattr("apps.api.request_observation.observe_http_request", AsyncMock())
    queue = AsyncMock()
    queue.enqueue = AsyncMock(return_value="job-synthetic")
    monkeypatch.setattr(jobs, "_queue", lambda *_: queue)
    app = FastAPI()
    for router in (team.router, team_me.router, jobs.router):
        app.include_router(router)
    return app


async def seed_memory(factory):
    """Global decision, one decision and one learning in each repository, an episode in A."""
    async with factory() as session, session.begin():
        session.add_all([
            Decision(id=1, title="global policy", repo_path=None, status="active", version=1,
                     author_principal_id=4, updated_by_principal_id=4, created_at=T0, updated_at=T0),
            Decision(id=2, title="decision in A", repo_path="/synthetic/A", status="active", version=1,
                     author_principal_id=2, updated_by_principal_id=2, created_at=T0 + timedelta(minutes=1),
                     updated_at=T0 + timedelta(minutes=1)),
            Decision(id=3, title="decision in B", repo_path="/synthetic/B", status="active", version=1,
                     author_principal_id=4, updated_by_principal_id=4, created_at=T0 + timedelta(minutes=2),
                     updated_at=T0 + timedelta(minutes=2)),
            Learning(id=1, statement="learning in A", repo_scope="/synthetic/A", author_principal_id=2,
                     updated_by_principal_id=2, created_at=T0, updated_at=T0),
            Learning(id=2, statement="learning in B", repo_scope="/synthetic/B", author_principal_id=4,
                     updated_by_principal_id=4, created_at=T0, updated_at=T0),
            Learning(id=3, statement="repository-less learning", repo_scope=None, author_principal_id=4,
                     updated_by_principal_id=4, created_at=T0, updated_at=T0),
            MemoryEpisode(id=1, distilled_summary="episode in A", repo_scope="/synthetic/A",
                          author_principal_id=2, updated_by_principal_id=2, created_at=T0, updated_at=T0),
        ])
        for repository_id in (1, 2):
            await record_event(session, kind="job.queued", audience="repository", actor_principal_id=4,
                               summary="Queued reindex job", repository_id=repository_id,
                               details={"job_id": f"job-{repository_id}", "job_type": "reindex"})


def kinds_and_titles(page):
    return {(event["kind"], event["summary"]) for event in page["events"]}


@pytest.mark.asyncio
async def test_me_in_solo_reports_the_owner_as_administrator(activity_db, monkeypatch):
    factory, _, _ = activity_db
    app = activity_app(factory, monkeypatch)
    await revoke_everyone(factory)
    await seed_memory(factory)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "fixture-legacy")
    async with client_for(app) as client:
        me = (await client.get("/team/me", headers=READER)).json()
        assert me["team_state"] == "solo" and me["is_team_admin"] is True
        assert me["principal"] == {"id": 1, "name": "human-1", "kind": "human", "credential_id": 1}
        assert me["access"]["repositories"] == "all" and me["scopes"] == ["*"]
        legacy = await client.get("/team/me", headers={"X-API-Key": "fixture-legacy"})
        assert legacy.status_code == 200 and legacy.json()["is_team_admin"] is True
        assert legacy.json()["projects"] == []
        # A team of one reads the whole log, including repository-less memory.
        feed = (await client.get("/team/activity", headers=READER)).json()
        summaries = {event["summary"] for event in feed["events"]}
        assert {"Decision recorded: decision in B (v1)", "Learning recorded: repository-less learning",
                "Episode recorded: episode in A"} <= summaries
        # /team/me needs no team scope; the feed needs team:read.
        assert (await client.get("/team/me", headers=NO_TEAM_READ)).json()["can_read_activity"] is False
        assert (await client.get("/team/activity", headers=NO_TEAM_READ)).status_code == 403


@pytest.mark.asyncio
async def test_me_lists_project_roles_permissions_and_repositories(activity_db, monkeypatch):
    factory, a, b = activity_db
    app = activity_app(factory, monkeypatch)
    async with client_for(app) as client:
        reader = (await client.get("/team/me", headers=READER)).json()
        admin = (await client.get("/team/me", headers=ADMIN)).json()
        core_only = (await client.get("/team/me", headers=NO_TEAM_READ)).json()
    assert reader["team_state"] == "team" and reader["is_team_admin"] is False
    assert reader["team_membership"]["verified"] is True and reader["team_membership"]["is_admin"] is False
    assert reader["access"]["repositories"] == "projects"
    [project] = reader["projects"]
    assert project["id"] == str(a) and project["role"] == "reader"
    assert project["effective_permissions"] == ["read"]
    assert project["repositories"] == [{"id": 1, "name": "A", "path": "/synthetic/A"}]
    assert admin["is_team_admin"] is True and admin["access"]["repositories"] == "all"
    assert [p["id"] for p in admin["projects"]] == [str(b)]
    assert core_only["scopes"] == ["core:read"] and core_only["effective_scopes"] == ["core:read"]


@pytest.mark.asyncio
async def test_member_sees_only_readable_projects_and_global_decisions(activity_db, monkeypatch):
    factory, a, b = activity_db
    app = activity_app(factory, monkeypatch)
    await seed_memory(factory)
    async with client_for(app) as client:
        feed = (await client.get("/team/activity", headers=READER)).json()
        seen = kinds_and_titles(feed)
        assert ("decision.created", "Decision recorded: global policy (v1)") in seen
        assert ("decision.created", "Decision recorded: decision in A (v1)") in seen
        assert ("learning.created", "Learning recorded: learning in A") in seen
        assert {e["details"].get("job_id") for e in feed["events"] if e["kind"] == "job.queued"} == {"job-1"}
        summaries = {summary for _, summary in seen}
        assert not {"Decision recorded: decision in B (v1)", "Learning recorded: learning in B",
                    "Learning recorded: repository-less learning", "Episode recorded: episode in A"} & summaries
        assert all(e["repository_id"] in (None, 1) for e in feed["events"])
        a_event = next(e for e in feed["events"] if e["summary"] == "Decision recorded: decision in A (v1)")
        assert a_event["repository_id"] == 1 and a_event["project_id"] == str(a)
        assert a_event["actor"] == {"principal_id": 2, "name": "human-2"}

        # Filters the member cannot read are refused, not silently emptied.
        assert (await client.get("/team/activity", params={"project_id": str(b)}, headers=READER)).status_code == 403
        assert (await client.get("/team/activity", params={"repository_id": 2}, headers=READER)).status_code == 403
        scoped = (await client.get("/team/activity", params={"project_id": str(a)}, headers=READER)).json()
        assert "Decision recorded: global policy (v1)" not in {e["summary"] for e in scoped["events"]}
        assert all(e["repository_id"] == 1 for e in scoped["events"])
        by_actor = (await client.get("/team/activity", params={"actor_id": 2, "kind": "decision"},
                                     headers=READER)).json()
        assert [e["summary"] for e in by_actor["events"]] == ["Decision recorded: decision in A (v1)"]
        assert (await client.get("/team/activity", params={"kind": "nonsense"}, headers=READER)).status_code == 422

        admin = kinds_and_titles((await client.get("/team/activity", headers=ADMIN)).json())
        assert ("decision.created", "Decision recorded: decision in B (v1)") in admin
        assert ("episode.created", "Episode recorded: episode in A") in admin


@pytest.mark.asyncio
async def test_membership_and_invitation_events_are_for_administrators_only(activity_db, monkeypatch):
    factory, a, _ = activity_db
    app = activity_app(factory, monkeypatch)
    async with factory() as session, session.begin():
        await provisioning.assign_project_member(session, project_id=a, principal_id=3, role="contributor",
                                                 actor_id=4)
        await provisioning.revoke_project_member(session, project_id=a, principal_id=2, actor_id=4)
        session.add(TeamInvitation(team_id=1, token_hash="0" * 64, principal_name="carol", project_ids=[str(a)],
                                   role="reader", expires_at=T0 + timedelta(days=7), invited_by="principal:4",
                                   invited_by_principal_id=4, created_at=T0))
    async with client_for(app) as client:
        for kind in ("membership", "project_role", "invitation"):
            response = await client.get("/team/activity", params={"kind": kind}, headers=READER)
            assert response.status_code == 200 and response.json()["events"] == []
        member_view = (await client.get("/team/activity", params={"project_id": str(a)}, headers=READER)).json()
        assert not [e for e in member_view["events"] if e["kind"].split(".")[0] in {"project_role", "invitation"}]

        admin = (await client.get("/team/activity", params={"kind": ["project_role", "invitation"]},
                                  headers=ADMIN)).json()
        by_kind = {event["kind"]: event for event in admin["events"]}
        changed = by_kind["project_role.changed"]
        assert changed["actor"]["principal_id"] == 4 and changed["subject"]["name"] == "human-3"
        assert changed["details"]["previous_role"] == "reviewer" and changed["project_id"] == str(a)
        assert by_kind["project_role.revoked"]["subject"]["principal_id"] == 2
        assert by_kind["invitation.created"]["summary"] == "Invitation issued for carol"


@pytest.mark.asyncio
async def test_membership_changes_are_recorded_by_provisioning(activity_db, monkeypatch):
    factory, _, _ = activity_db
    app = activity_app(factory, monkeypatch)
    async with client_for(app) as client:
        assert (await client.delete("/team/projects/members/3", headers=ADMIN)).status_code == 200
        feed = (await client.get("/team/activity", params={"kind": "membership"}, headers=ADMIN)).json()
    [revoked] = feed["events"]
    assert revoked["kind"] == "membership.revoked" and revoked["actor"]["principal_id"] == 4
    assert revoked["subject"] == {"type": "membership", "principal_id": 3, "name": "human-3"}
    async with factory() as session:
        rows = (await session.execute(TeamActivityEvent.__table__.select())).all()
    assert {row.audience for row in rows} == {"admin"}


@pytest.mark.asyncio
async def test_queued_jobs_name_their_initiator_and_follow_repository_access(activity_db, monkeypatch):
    factory, _, _ = activity_db
    app = activity_app(factory, monkeypatch)
    async with client_for(app) as client:
        queued = await client.post("/jobs/reindex", json={"repo_path": "/synthetic/A"}, headers=CONTRIBUTOR)
        assert queued.status_code == 200
        reader = (await client.get("/team/activity", params={"kind": "job"}, headers=READER)).json()
        assert (await client.post("/jobs/reindex", json={"repo_path": "/synthetic/B"}, headers=ADMIN)).status_code == 200
        reader_after = (await client.get("/team/activity", params={"kind": "job"}, headers=READER)).json()
    [job] = reader["events"]
    assert job["kind"] == "job.queued" and job["actor"] == {"principal_id": 2, "name": "human-2"}
    assert job["repository_id"] == 1 and job["details"]["job_type"] == "reindex"
    assert job["details"]["job_id"] == "job-synthetic"
    assert len(reader_after["events"]) == 1  # the job in B stays out of the reader's feed


@pytest.mark.asyncio
async def test_decision_versions_follow_the_supersede_chain(activity_db, monkeypatch):
    factory, _, _ = activity_db
    app = activity_app(factory, monkeypatch)
    edited = T0 + timedelta(hours=1)
    async with factory() as session, session.begin():
        # Canonical row 10 holds v2 by principal 2; history row 11 keeps v1 by principal 1, written at the edit.
        session.add_all([
            Decision(id=10, title="canonical", repo_path="/synthetic/A", status="active", version=2,
                     supersedes_id=11, author_principal_id=1, updated_by_principal_id=2,
                     created_at=T0, updated_at=edited),
            Decision(id=11, title="canonical", repo_path="/synthetic/A", status="superseded", version=1,
                     superseded_by_id=10, author_principal_id=1, updated_by_principal_id=1,
                     created_at=T0, updated_at=edited),
        ])
    async with client_for(app) as client:
        feed = (await client.get("/team/activity", params={"kind": "decision"}, headers=READER)).json()
    assert [(e["kind"], e["actor"]["principal_id"], e["subject"]["id"], e["subject"]["version"])
            for e in feed["events"]] == [("decision.updated", 2, "10", 2), ("decision.created", 1, "10", 1)]
    assert feed["events"][0]["occurred_at"].startswith("2026-10-01T13:00")


@pytest.mark.asyncio
async def test_activity_pages_newest_first(activity_db, monkeypatch):
    factory, _, _ = activity_db
    app = activity_app(factory, monkeypatch)
    async with factory() as session, session.begin():
        session.add_all([Decision(id=20 + i, title=f"global {i}", repo_path=None, status="active", version=1,
                                  author_principal_id=4, created_at=T0 + timedelta(minutes=i),
                                  updated_at=T0 + timedelta(minutes=i)) for i in range(5)])
    titles = []
    async with client_for(app) as client:
        offset = 0
        while offset is not None:
            page = (await client.get("/team/activity", params={"kind": "decision", "limit": 2, "offset": offset},
                                     headers=READER)).json()
            assert len(page["events"]) <= 2 and page["has_more"] == (page["next_offset"] is not None)
            titles += [event["subject"]["title"] for event in page["events"]]
            offset = page["next_offset"]
        since = (await client.get("/team/activity", params={"kind": "decision",
                                                            "since": (T0 + timedelta(minutes=3)).isoformat()},
                                  headers=READER)).json()
    assert titles == [f"global {i}" for i in reversed(range(5))]
    assert [event["subject"]["title"] for event in since["events"]] == ["global 4", "global 3"]
