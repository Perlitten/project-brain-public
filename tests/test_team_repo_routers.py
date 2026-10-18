"""Team-state authorization on the workspace-registry routers (freshness, ledger,
workspace, operations, lab, experiments) and the global surfaces (portfolio,
improvement, quality). Solo state stays a team of one."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import update

from apps.api import auth, team_access
from apps.api.routers import (
    experiments,
    freshness,
    improvement,
    lab,
    ledger,
    operations,
    portfolio,
    quality,
    workspace,
)
from brain.auth.principals import ResolvedPrincipal
from brain.config.settings import settings
from brain.database.models import ApiCredential
from brain.team.authorization import Authority
from brain.workspace.models import RepositoryRecord
from tests.test_team_foundations import team_db as team_fixture
from tests.test_team_transport import live_team_state, membership_driven, revoke_everyone

team_db = team_fixture
pytestmark = pytest.mark.team_state

LEGACY = {"X-API-Key": "fixture-legacy"}
READER, CONTRIBUTOR, ADMIN = ({"X-API-Key": f"fixture-{i}"} for i in (1, 2, 4))
DENIED = "Repository access denied"
ADMIN_REQUIRED = "Team administrator required"


def _record(repository_id: str, root: str) -> RepositoryRecord:
    # No capabilities: once authorization passes, a write stops at a distinguishable capability 403.
    return RepositoryRecord(
        repository_id=repository_id, display_name=repository_id, canonical_root=root,
        normalized_root_identity=root.lower(), repository_type="fixture", default_branch="main",
        current_revision="", trust_level="fixture", organization="", owner="",
    )


RECORDS = {"repo-a": _record("repo-a", "/synthetic/A"), "repo-b": _record("repo-b", "/synthetic/B")}


class FakeRegistry:
    """Stands in for the registry reader: registry ids repo-a/repo-b point at the indexed roots of projects A/B."""

    def __init__(self, *_args, **_kwargs):
        pass

    def get(self, repository_id):
        return RECORDS.get(repository_id)

    def list_all(self, *_args, **_kwargs):
        return list(RECORDS.values())

    def verify_integrity(self):
        return True, "ok"


def repo_app(factory, monkeypatch, tmp_path):
    """Every touched router over an isolated working directory and the live team-state probe."""
    monkeypatch.chdir(tmp_path)  # file-backed stores (.brain/...) start empty and stay out of the checkout
    for module in (auth, team_access):
        monkeypatch.setattr(module, "async_session_factory", factory)
    monkeypatch.setattr(team_access, "current_team_state", live_team_state)
    monkeypatch.setattr("apps.api.request_observation.observe_http_request", AsyncMock())
    for module in (freshness, ledger, workspace, operations, lab, experiments):
        monkeypatch.setattr(module, "workspace_registry", AsyncMock(return_value=FakeRegistry()))
    generations = MagicMock()
    generations.get_active_generation_id.return_value = None
    generations.list_generations.return_value = []
    monkeypatch.setattr(freshness, "GraphGenerationManager", lambda *_: generations)
    monkeypatch.setattr(freshness.FreshnessTracker, "resolve_revision", staticmethod(lambda _path: ""))
    nightly = MagicMock()
    nightly.repair_vectors.return_value = SimpleNamespace(model_dump=lambda: {"status": "queued"})
    monkeypatch.setattr(operations, "mgr", nightly)
    auditor = MagicMock()
    auditor.audit_safety_mutations.return_value = (True, 0, 0, [])
    monkeypatch.setattr(improvement, "safety_auditor", auditor)
    bundles = MagicMock()
    bundles.get_alias.return_value = None
    monkeypatch.setattr(improvement, "registry", bundles)
    monkeypatch.setattr(quality, "load_quality_evidence", lambda: {"status": "success"})
    app = FastAPI()
    for module in (freshness, ledger, workspace, operations, lab, experiments, portfolio, improvement, quality):
        app.include_router(module.router)
    return app


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")


def validate_body(repository_id):
    return {"repository_id": repository_id, "patch_content": "print('synthetic')\n"}


def experiment_body(repository_id):
    return {"repository_id": repository_id, "options": []}


@pytest.mark.asyncio
async def test_team_experiment_acceptance_requires_an_independent_initiator(monkeypatch):
    experiment = SimpleNamespace(repository_id="repo-a", creation_actor="principal:3")
    manager = MagicMock()
    manager.get.return_value = experiment
    monkeypatch.setattr(experiments, "_manager", lambda: manager)
    monkeypatch.setattr(experiments, "_authorize", AsyncMock(
        return_value=Authority(actor_id=3, initiator_id=3, initiator_credential_id=3, allowed=frozenset())
    ))
    principal = ResolvedPrincipal(3, "reviewer", "human", credential_id=3, scopes=frozenset())

    with pytest.raises(HTTPException, match="Independent experiment acceptance") as refused:
        await experiments.record_action(
            "experiment-1",
            experiments.HumanActionRequest(action="accept_for_export", actor="forged-reviewer"),
            principal=principal,
        )

    assert refused.value.status_code == 403
    manager.record_human_action.assert_not_called()


# Scoped reads a project-A reader may make on repo-a and must be refused on repo-b, one per registry router.
SCOPED_READS = (
    ("/freshness/repositories/{repo}", {}),
    ("/ledger/events", {"repository_id": "{repo}"}),
    ("/workspace/repositories/{repo}", {}),
    ("/workspace/repositories/{repo}/capabilities", {}),
    ("/experiments", {"repository_id": "{repo}"}),
)

# Global or cross-repository routes: a team administrator once members exist. Routes that are global
# by definition declare team_administrator_access(); an unfiltered call to a filterable list is the
# unscoped repository_authority surface. Both rest on brain.team.authorization.team_administrator.
UNSCOPED = (
    ("GET", "/ledger/events", DENIED),
    ("GET", "/ledger/verify", ADMIN_REQUIRED),
    ("GET", "/workspace/repositories", ADMIN_REQUIRED),
    ("GET", "/workspace/verify", ADMIN_REQUIRED),
    ("GET", "/operations/canary/status", ADMIN_REQUIRED),
    ("GET", "/lab/workspaces", ADMIN_REQUIRED),
    ("GET", "/experiments", DENIED),
    ("GET", "/portfolio", ADMIN_REQUIRED),
    ("GET", "/improvement/status", ADMIN_REQUIRED),
)


def _fill(path, params, repo):
    return path.format(repo=repo), {k: v.format(repo=repo) for k, v in params.items()}


@pytest.mark.asyncio
async def test_team_reader_reads_own_repository_and_is_refused_on_other_project(team_db, monkeypatch, tmp_path):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    app = repo_app(factory, monkeypatch, tmp_path)
    async with client_for(app) as client:
        for path, params in SCOPED_READS:
            own_path, own_params = _fill(path, params, "repo-a")
            own = await client.get(own_path, params=own_params, headers=READER)
            assert own.status_code == 200, (own_path, own.text)
            other_path, other_params = _fill(path, params, "repo-b")
            other = await client.get(other_path, params=other_params, headers=READER)
            assert other.status_code == 403 and DENIED in other.json()["detail"], (other_path, other.text)
        # A registry id that is unknown, or whose root is not indexed, is unattached: team administrator only.
        assert (await client.get("/freshness/repositories/repo-zzz", headers=READER)).status_code == 403
        assert (await client.get("/freshness/repositories/repo-zzz", headers=ADMIN)).status_code == 404
        # Repository-path filter on the ledger goes through the indexed path.
        assert (await client.get("/ledger/events", params={"repository_path": "/synthetic/A"},
                                 headers=READER)).status_code == 200
        assert (await client.get("/ledger/events", params={"repository_path": "/synthetic/B"},
                                 headers=READER)).status_code == 403


@pytest.mark.asyncio
async def test_team_writes_need_run_request_on_the_repository(team_db, monkeypatch, tmp_path):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    app = repo_app(factory, monkeypatch, tmp_path)
    writes = (
        ("/freshness/repositories/{repo}/rebuild", lambda repo: {}),
        ("/lab/validate", validate_body),
        ("/experiments", experiment_body),
        ("/operations/repositories/{repo}/repair-vectors", None),
    )
    async with client_for(app) as client:
        for path, body in writes:
            for repo in ("repo-a", "repo-b"):
                url = path.format(repo=repo)
                kwargs = {"json": body(repo)} if body else {}
                # A reader holds read only: refused even on its own project's repository.
                reader = await client.post(url, headers=READER, **kwargs)
                assert reader.status_code == 403 and DENIED in reader.json()["detail"], (url, reader.text)
            # The contributor of A is refused on B ...
            url_b = path.format(repo="repo-b")
            other = await client.post(url_b, headers=CONTRIBUTOR, **({"json": body("repo-b")} if body else {}))
            assert other.status_code == 403 and DENIED in other.json()["detail"], (url_b, other.text)
            # ... and on A gets past authorization to the registry's own capability check (or runs).
            url_a = path.format(repo="repo-a")
            own = await client.post(url_a, headers=CONTRIBUTOR, **({"json": body("repo-a")} if body else {}))
            assert DENIED not in own.text, (url_a, own.text)
            assert own.status_code == 200 or "lacks capability" in own.json()["detail"], (url_a, own.text)


@pytest.mark.asyncio
async def test_team_unscoped_routes_need_a_team_administrator(team_db, monkeypatch, tmp_path):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    app = repo_app(factory, monkeypatch, tmp_path)
    async with client_for(app) as client:
        for method, path, detail in UNSCOPED:
            for headers in (READER, CONTRIBUTOR):
                refused = await client.request(method, path, headers=headers)
                assert refused.status_code == 403 and detail in refused.json()["detail"], (path, refused.text)
            allowed = await client.request(method, path, headers=ADMIN)
            assert allowed.status_code == 200, (path, allowed.text)
        # Naming one's own repository in the query string does not open a global route.
        for path in ("/portfolio", "/improvement/status", "/workspace/repositories", "/operations/canary/status"):
            response = await client.get(path, params={"repo_path": "/synthetic/A", "repository_id": "1"},
                                        headers=READER)
            assert response.status_code == 403, (path, response.text)
        # Registry administration and lab recovery are administrator-only too.
        assert (await client.post("/workspace/repositories/repo-a/disable", headers=CONTRIBUTOR)).status_code == 403
        assert (await client.post("/lab/recover", headers=CONTRIBUTOR)).status_code == 403
        # Intentionally open: the build's reference benchmark and the static lab profiles.
        assert (await client.get("/api/web/quality", headers=READER)).status_code == 200
        assert (await client.get("/lab/profiles", headers=READER)).status_code == 200


@pytest.mark.asyncio
async def test_team_administrator_flag_without_team_admin_scope_is_refused(team_db, monkeypatch, tmp_path):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    # Principal 4 stays flagged is_admin, but its credential no longer carries team:admin.
    async with factory() as session, session.begin():
        await session.execute(update(ApiCredential).where(ApiCredential.id == 4).values(
            scopes=["ledger:read", "workspace:read", "operations:read", "lab:read", "experiments:read",
                    "portfolio:read", "improvement:read", "team:read"]))
    app = repo_app(factory, monkeypatch, tmp_path)
    async with client_for(app) as client:
        for method, path, detail in UNSCOPED:
            response = await client.request(method, path, headers=ADMIN)
            assert response.status_code == 403 and detail in response.json()["detail"], (path, response.text)
        # With team:read its contributor role on project B still opens B's repository.
        assert (await client.get("/workspace/repositories/repo-b", headers=ADMIN)).status_code == 200


@pytest.mark.asyncio
async def test_team_state_refuses_the_shared_key_and_dev_open(team_db, monkeypatch, tmp_path):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "fixture-legacy")
    app = repo_app(factory, monkeypatch, tmp_path)
    async with client_for(app) as client:
        probes = [("GET", *_fill(path, params, "repo-a")) for path, params in SCOPED_READS]
        probes += [(method, path, {}) for method, path, _ in UNSCOPED]
        for method, path, params in probes:
            response = await client.request(method, path, params=params, headers=LEGACY)
            assert response.status_code == 403, (path, response.text)
            assert "Named credential required" in response.json()["detail"], (path, response.text)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    async with client_for(app) as client:
        assert (await client.get("/freshness/repositories/repo-a")).status_code == 401
        assert (await client.get("/portfolio")).status_code == 401


@pytest.mark.asyncio
async def test_solo_state_is_unchanged(team_db, monkeypatch, tmp_path):
    factory, _, _ = team_db
    membership_driven(monkeypatch)
    await revoke_everyone(factory)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "fixture-legacy")
    app = repo_app(factory, monkeypatch, tmp_path)
    async with client_for(app) as client:
        for headers in (LEGACY, READER):
            for repo in ("repo-a", "repo-b"):
                for path, params in SCOPED_READS:
                    url, query = _fill(path, params, repo)
                    response = await client.get(url, params=query, headers=headers)
                    assert response.status_code == 200, (url, response.text)
                rebuild = await client.post(f"/freshness/repositories/{repo}/rebuild", json={}, headers=headers)
                assert rebuild.status_code == 403 and "lacks capability" in rebuild.json()["detail"]
            for method, path, _ in UNSCOPED:
                response = await client.request(method, path, headers=headers)
                assert response.status_code == 200, (path, response.text)
            # Unknown ids keep their plain 404 instead of an authorization refusal.
            assert (await client.get("/freshness/repositories/repo-zzz", headers=headers)).status_code == 404
    monkeypatch.setattr(settings, "PROJECT_BRAIN_API_KEY", "")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    async with client_for(app) as client:
        assert (await client.get("/freshness/repositories/repo-a")).status_code == 200
        assert (await client.get("/portfolio")).status_code == 200
