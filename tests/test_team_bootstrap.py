"""First-admin activation proves a saved human credential before changing sign-in."""
from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select, update

from apps.api.auth import require_principal
from apps.api.routers import team
from brain.auth.principals import ResolvedPrincipal, hash_credential
from brain.config.settings import settings
from brain.database.models import ApiCredential, Principal
from brain.team.authorization import team_state
from brain.team.models import Team, TeamMember
from tests.test_team_foundations import team_db as team_fixture

team_db = team_fixture
pytestmark = [pytest.mark.team_state, pytest.mark.asyncio]
OWNER = ResolvedPrincipal(id=0, name="legacy-api-key", kind="service")
ADMIN_SCOPES = ["team:admin", "team:read", "principals:write", "setup:write"]
SAVED = "synthetic-saved-admin-credential"


@pytest_asyncio.fixture
async def bootstrap(team_db, monkeypatch):
    factory, _, _ = team_db
    monkeypatch.setattr(settings, "BRAIN_TEAM_MODE", False)
    async with factory() as session, session.begin():
        # An operator-dissolved install is solo; merely revoking members deliberately is not.
        await session.execute(update(TeamMember).values(revoked_at=datetime(2020, 1, 1, tzinfo=timezone.utc)))
        await session.execute(update(Team).values(dissolved_at=datetime.now(timezone.utc)))
        session.add(Principal(id=5, name="new-admin", kind="human"))
        await session.flush()
        session.add(ApiCredential(id=5, principal_id=5, key_hash=hash_credential(SAVED), scopes=ADMIN_SCOPES))
    monkeypatch.setattr(team, "async_session_factory", factory)
    monkeypatch.setattr("apps.api.team_access.async_session_factory", factory)
    app = FastAPI()
    app.include_router(team.router)
    app.dependency_overrides[require_principal] = lambda: OWNER
    yield app, factory


async def activate(app, credential=SAVED, **extra):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://brain") as client:
        return await client.post("/team/projects/bootstrap", json={"team_name": "synthetic team", "credential": credential, **extra})


async def test_only_final_activation_changes_sign_in_and_saved_key_remains_live(bootstrap):
    app, factory = bootstrap
    async with factory() as session:
        assert await team_state(session) == "solo"
        assert await session.get(TeamMember, 5) is None
    response = await activate(app)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert SAVED not in response.text
    async with factory() as session:
        member = await session.get(TeamMember, 5)
        assert member.is_admin and member.verified_at and member.revoked_at is None
        assert await team_state(session) == "team"
        credential = await session.get(ApiCredential, 5)
        assert credential.revoked_at is None and credential.key_hash == hash_credential(SAVED)
    retry = await activate(app)
    assert retry.status_code in {401, 403, 409}, "retired shared sign-in must not create another administrator on retry"
    async with factory() as session:
        assert len((await session.scalars(select(TeamMember).where(TeamMember.revoked_at.is_(None)))).all()) == 1


@pytest.mark.parametrize("defect", ["unknown", "revoked", "expired", "disabled", "service", "unscoped"])
async def test_invalid_or_nonhuman_saved_key_does_not_retire_shared_sign_in(bootstrap, defect):
    app, factory = bootstrap
    key = SAVED
    async with factory() as session, session.begin():
        credential = await session.get(ApiCredential, 5)
        principal = await session.get(Principal, 5)
        if defect == "unknown":
            key = "synthetic-unknown-credential"
        elif defect == "revoked":
            credential.revoked_at = datetime.now(timezone.utc)
        elif defect == "expired":
            credential.expires_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        elif defect == "disabled":
            principal.disabled_at = datetime.now(timezone.utc)
        elif defect == "service":
            principal.kind = "service"
        else:
            credential.scopes = ["core:read"]
    response = await activate(app, key)
    assert response.status_code == 403
    async with factory() as session:
        assert await team_state(session) == "solo"
        assert await session.get(TeamMember, 5) is None


@pytest.mark.parametrize("caller", [
    ResolvedPrincipal(id=-1, name="dev-open", kind="service"),
    ResolvedPrincipal(id=1, name="integration", kind="service", scopes=frozenset({"principals:write", "setup:write"})),
])
async def test_anonymous_and_restricted_integrations_cannot_enable_a_team(bootstrap, caller):
    app, factory = bootstrap
    app.dependency_overrides[require_principal] = lambda: caller
    response = await activate(app)
    assert response.status_code == 403
    async with factory() as session:
        assert await team_state(session) == "solo"
        assert await session.get(TeamMember, 5) is None
