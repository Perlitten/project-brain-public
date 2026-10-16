"""GET /api/web-session: who a dashboard credential belongs to (per-member web identity).

The web UI asks this before every request it serves in team state, so the
answer must follow revocation immediately and refuse anyone who is not an
active verified human member."""
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import update

from apps.api import auth, team_access
from apps.api.routers import web_session
from brain.config.settings import settings
from brain.database.models import ApiCredential
from brain.team.models import TeamMember
from tests.test_team_foundations import team_db as team_fixture
from tests.test_team_transport import (
    LEGACY, ADMIN, CONTRIBUTOR, OUTSIDER, READER, add_outsider_and_unattached_repo, live_team_state,
    membership_driven, revoke_everyone,
)

team_db = team_fixture
pytestmark = pytest.mark.team_state
NOW = datetime.now(timezone.utc)


def session_app(factory, monkeypatch):
    for module in (auth, team_access, web_session):
        monkeypatch.setattr(module, "async_session_factory", factory)
    monkeypatch.setattr(team_access, "current_team_state", live_team_state)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "fixture-legacy")
    app = FastAPI()
    app.include_router(web_session.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")


@pytest.mark.asyncio
async def test_team_member_and_admin_identity(team_db, monkeypatch):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    async with session_app(factory, monkeypatch) as client:
        admin = (await client.get("/api/web-session", headers=ADMIN)).json()
        reader = (await client.get("/api/web-session", headers=READER)).json()
    assert admin["mode"] == "team" and admin["principal"] == {"id": 4, "name": "human-4", "kind": "human"}
    assert admin["team_admin"] is True and admin["member"]["is_admin"] is True
    assert reader["principal"]["name"] == "human-1"
    assert reader["team_admin"] is False and reader["member"]["is_admin"] is False


@pytest.mark.asyncio
async def test_team_refuses_shared_key_outsiders_and_missing_keys(team_db, monkeypatch):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    await add_outsider_and_unattached_repo(factory)
    async with session_app(factory, monkeypatch) as client:
        shared = await client.get("/api/web-session", headers=LEGACY)
        outsider = await client.get("/api/web-session", headers=OUTSIDER)
        anonymous = await client.get("/api/web-session")
    assert shared.status_code == 403 and "Named credential required" in shared.json()["detail"]
    assert outsider.status_code == 403 and "Dashboard sign-in denied" in outsider.json()["detail"]
    assert anonymous.status_code == 401


@pytest.mark.asyncio
async def test_revocation_applies_on_the_next_request(team_db, monkeypatch):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    async with session_app(factory, monkeypatch) as client:
        assert (await client.get("/api/web-session", headers=READER)).status_code == 200
        assert (await client.get("/api/web-session", headers=CONTRIBUTOR)).status_code == 200
        async with factory() as session, session.begin():
            await session.execute(update(ApiCredential).where(ApiCredential.id == 1).values(revoked_at=NOW))
            await session.execute(update(TeamMember).where(TeamMember.principal_id == 2).values(revoked_at=NOW))
        revoked_key = await client.get("/api/web-session", headers=READER)
        revoked_member = await client.get("/api/web-session", headers=CONTRIBUTOR)
    assert revoked_key.status_code == 401
    assert revoked_member.status_code == 403


@pytest.mark.asyncio
async def test_admin_flag_without_team_admin_scope_is_not_offered_administration(team_db, monkeypatch):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    async with factory() as session, session.begin():
        await session.execute(update(ApiCredential).where(ApiCredential.id == 4).values(scopes=["core:read"]))
    async with session_app(factory, monkeypatch) as client:
        body = (await client.get("/api/web-session", headers=ADMIN)).json()
    assert body["member"]["is_admin"] is True and body["team_admin"] is False


@pytest.mark.asyncio
async def test_solo_state_answers_for_the_shared_key(team_db, monkeypatch):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    await revoke_everyone(factory)
    async with session_app(factory, monkeypatch) as client:
        response = await client.get("/api/web-session", headers=LEGACY)
    assert response.status_code == 200
    assert response.json() == {"mode": "solo", "principal": {"id": 0, "name": response.json()["principal"]["name"],
                                                            "kind": response.json()["principal"]["kind"]},
                               "team_admin": True, "member": None}
