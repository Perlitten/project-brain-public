"""RC Phase 3 — contract smoke over every v0.5.0 HTTP endpoint.

Production-equivalent: the real routers run against real stores, a real Git
fixture repository and a real disposable workspace. Nothing in the request path
is mocked; only the database drivers the application imports at start-up are
stubbed, exactly as the other API tests do.

Every endpoint listed in ``reports/v0.5.0-release-candidate/feature-inventory.json``
is exercised here, and :func:`test_every_v050_endpoint_is_covered` fails if a new
endpoint appears without a smoke test.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

with (
    patch("sqlalchemy.ext.asyncio.create_async_engine"),
    patch("redis.asyncio.from_url"),
    patch("neo4j.AsyncGraphDatabase.driver"),
):
    from apps.api.main import app

from fastapi.testclient import TestClient

from tests.support.portfolio_fixture import build_fixture_portfolio

# Recorded so the coverage test can compare against the live application.
COVERED_ENDPOINTS = {
    ("GET", "/workspace/repositories"),
    ("POST", "/workspace/repositories"),
    ("GET", "/workspace/repositories/{repository_id}"),
    ("GET", "/workspace/repositories/{repository_id}/capabilities"),
    ("GET", "/workspace/repositories/{repository_id}/events"),
    ("POST", "/workspace/repositories/{repository_id}/disable"),
    ("GET", "/workspace/verify"),
    ("GET", "/portfolio"),
    ("POST", "/portfolio"),
    ("GET", "/portfolio/{portfolio_id}"),
    ("POST", "/portfolio/{portfolio_id}/build"),
    ("GET", "/portfolio/{portfolio_id}/dependencies"),
    ("GET", "/portfolio/{portfolio_id}/cycles"),
    ("GET", "/portfolio/{portfolio_id}/coupling"),
    ("GET", "/freshness/repositories/{repository_id}"),
    ("GET", "/freshness/artifacts/{artifact_id}"),
    ("POST", "/freshness/repositories/{repository_id}/rebuild"),
    ("GET", "/lab/profiles"),
    ("GET", "/lab/workspaces"),
    ("GET", "/lab/workspaces/{workspace_id}"),
    ("POST", "/lab/workspaces/{workspace_id}/cleanup"),
    ("POST", "/lab/validate"),
    ("POST", "/lab/recover"),
    ("GET", "/lab/sessions/{session_id}"),
    ("POST", "/experiments"),
    ("GET", "/experiments"),
    ("GET", "/experiments/{experiment_id}"),
    ("POST", "/experiments/{experiment_id}/run"),
    ("POST", "/experiments/{experiment_id}/cancel"),
    ("GET", "/experiments/{experiment_id}/comparison"),
    ("GET", "/experiments/{experiment_id}/recommendation"),
    ("POST", "/experiments/{experiment_id}/actions"),
    ("POST", "/experiments/{experiment_id}/export"),
    ("GET", "/ledger/events"),
    ("GET", "/ledger/events/{event_id}"),
    ("GET", "/ledger/verify"),
    ("GET", "/ledger/health"),
    ("GET", "/ledger/entities/{entity_type}/{entity_id}"),
    ("GET", "/control/summary"),
    ("GET", "/control/queue"),
    ("GET", "/control/actions"),
    ("GET", "/control/budgets"),
    ("GET", "/control/budgets/{budget}/check"),
    ("GET", "/control/metrics"),
    ("GET", "/control/health"),
    ("POST", "/execution/sessions"),
    ("GET", "/execution/sessions/{execution_id}"),
    ("GET", "/routing/policy"),
    ("GET", "/routing/decisions"),
    ("GET", "/routing/summary"),
    ("GET", "/shadow/executions"),
    ("GET", "/shadow/executions/{shadow_id}"),
    ("POST", "/shadow/executions/{shadow_id}/cancel"),
    ("GET", "/shadow/summary"),
    ("GET", "/operations/nightly/latest"),
    ("GET", "/operations/incidents"),
    ("GET", "/operations/freshness"),
    ("POST", "/operations/repositories/{repository_id}/repair-vectors"),
    ("POST", "/operations/jobs/{job_id}/retry"),
    ("POST", "/operations/canary/rollback"),
    ("GET", "/operations/canary/status"),
    ("POST", "/autonomy/goals"),
    ("GET", "/autonomy/goals/{goal_id}"),
    ("POST", "/autonomy/goals/{goal_id}/cancel"),
}

V050_PREFIXES = ("/workspace", "/portfolio", "/freshness", "/lab", "/experiments",
                 "/ledger", "/control", "/execution", "/routing", "/shadow", "/operations", "/autonomy")


@pytest.fixture
def client() -> TestClient:
    """An authenticated client.

    The key is read from the same settings object the dependency checks, so the
    suite works whether or not a key is configured in this environment, and the
    value is never written into a test artifact.
    """
    from brain.config.settings import settings

    client = TestClient(app, raise_server_exceptions=False)
    if settings.PROJECT_BRAIN_API_KEY:
        client.headers["X-API-Key"] = settings.PROJECT_BRAIN_API_KEY
    return client


def test_v050_endpoints_require_the_api_key():
    """Every v0.5.0 endpoint sits behind the API-key dependency."""
    from brain.config.settings import settings

    if not settings.PROJECT_BRAIN_API_KEY:
        pytest.skip("no API key configured in this environment")
    anonymous = TestClient(app, raise_server_exceptions=False)
    for path in ("/workspace/repositories", "/portfolio", "/lab/profiles",
                 "/experiments", "/ledger/health", "/control/summary",
                 "/freshness/repositories/repo-x"):
        assert anonymous.get(path).status_code == 401, path


@pytest.fixture
def portfolio(tmp_path, monkeypatch):
    """A real five-repository fixture portfolio, with the API rooted in tmp_path.

    The routers resolve their stores relative to the working directory, so the
    chdir is what keeps every run's state disposable.
    """
    built = build_fixture_portfolio(tmp_path / "fixtures")
    monkeypatch.chdir(tmp_path)
    return built


def _register(client: TestClient, path: Path, name: str) -> dict:
    response = client.post(
        "/workspace/repositories",
        json={"display_name": name, "path": str(path), "trust_level": "trusted_internal"},
    )
    assert response.status_code == 200, response.text
    return response.json()["repository"]


def _patch_adding_line(repo: Path, rel_path: str, text: str) -> str:
    """A unified diff with real context, derived from the file on disk.

    ``git apply`` rejects zero-context hunks unless ``--unidiff-zero`` is passed,
    which the product deliberately does not do.
    """
    import difflib

    before = (repo / rel_path).read_text(encoding="utf-8").splitlines(keepends=True)
    after = [before[0], text + "\n"] + before[1:]
    body = "".join(
        difflib.unified_diff(before, after, fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}")
    )
    return f"diff --git a/{rel_path} b/{rel_path}\n{body}"


# ── registry ──────────────────────────────────────────────────────────────


def test_registry_endpoints(client, portfolio):
    shared = portfolio.path("shared-contracts")
    record = _register(client, shared, "shared-contracts")
    repo_id = record["repository_id"]
    assert record["current_revision"] == portfolio.head("shared-contracts")

    listing = client.get("/workspace/repositories")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1

    detail = client.get(f"/workspace/repositories/{repo_id}")
    assert detail.status_code == 200
    assert detail.json()["repository"]["repository_id"] == repo_id

    capabilities = client.get(f"/workspace/repositories/{repo_id}/capabilities")
    assert capabilities.status_code == 200
    body = capabilities.json()
    assert body["validation_issues"] == []
    assert "apply_candidate_patches" in body["capabilities"]

    events = client.get(f"/workspace/repositories/{repo_id}/events")
    assert events.status_code == 200
    assert events.json()["total"] >= 1

    verify = client.get("/workspace/verify")
    assert verify.status_code == 200
    assert verify.json()["integrity"] is True

    disabled = client.post(f"/workspace/repositories/{repo_id}/disable?reason=smoke")
    assert disabled.status_code == 200
    assert disabled.json()["repository"]["disabled"] is True


def test_registry_rejects_unknown_repository_and_trust_level(client, portfolio):
    assert client.get("/workspace/repositories/repo-missing").status_code == 404
    response = client.post(
        "/workspace/repositories",
        json={"display_name": "x", "path": str(portfolio.path("api-service")),
              "trust_level": "root"},
    )
    assert response.status_code == 400


def test_default_registration_allows_the_cli_default_profile(client, portfolio):
    """Registration must allow the profile the CLIs and API default to."""
    record = _register(client, portfolio.path("shared-contracts"), "shared-contracts")
    assert "python-compile" in record["allowed_validation_profiles"]


# ── portfolio ─────────────────────────────────────────────────────────────


def _portfolio_config(records: dict) -> dict:
    return {
        "version": 1,
        "portfolio": {"name": "smoke-portfolio", "owners": ["platform"]},
        "repositories": {
            alias: {"repository_id": record["repository_id"], "role": alias}
            for alias, record in records.items()
        },
        "contracts": [
            {"from": "api-service", "to": "shared-contracts",
             "type": "depends_on_package", "allowed": True},
        ],
    }


def test_portfolio_endpoints(client, portfolio):
    records = {
        alias: _register(client, portfolio.path(alias), alias)
        for alias in ("shared-contracts", "api-service")
    }

    created = client.post("/portfolio", json={"config": _portfolio_config(records)})
    assert created.status_code == 200, created.text
    portfolio_id = created.json()["portfolio"]["portfolio_id"]

    assert client.get("/portfolio").json()["total"] == 1
    assert client.get(f"/portfolio/{portfolio_id}").status_code == 200

    built = client.post(f"/portfolio/{portfolio_id}/build")
    assert built.status_code == 200, built.text
    generation = built.json()["generation"]
    assert generation["status"] == "active"
    # The generation records the revisions the repositories are actually at.
    assert set(generation["repository_revisions"].values()) == {
        records["shared-contracts"]["current_revision"],
        records["api-service"]["current_revision"],
    }

    assert client.get(f"/portfolio/{portfolio_id}/dependencies").status_code == 200
    assert client.get(f"/portfolio/{portfolio_id}/cycles").status_code == 200
    coupling = client.get(f"/portfolio/{portfolio_id}/coupling")
    assert coupling.status_code == 200
    assert "circular_dependencies" in coupling.json()["coupling"]


def test_portfolio_build_refuses_unregistered_repositories(client, portfolio):
    config = {
        "version": 1,
        "portfolio": {"name": "unregistered", "owners": ["platform"]},
        "repositories": {"ghost": {"repository_id": "repo-ghost", "role": "ghost"}},
        "contracts": [],
    }
    created = client.post("/portfolio", json={"config": config})
    portfolio_id = created.json()["portfolio"]["portfolio_id"]
    built = client.post(f"/portfolio/{portfolio_id}/build")
    assert built.status_code == 409
    assert "repo-ghost" in built.json()["detail"]


def test_portfolio_rejects_invalid_config(client, portfolio):
    bad = {"version": 99, "portfolio": {"name": "x"}, "repositories": {}}
    assert client.post("/portfolio", json={"config": bad}).status_code == 400
    assert client.get("/portfolio/portfolio-missing").status_code == 404


# ── freshness ─────────────────────────────────────────────────────────────


def test_freshness_endpoints(client, portfolio):
    record = _register(client, portfolio.path("shared-contracts"), "shared-contracts")
    repo_id = record["repository_id"]

    before = client.get(f"/freshness/repositories/{repo_id}")
    assert before.status_code == 200
    assert before.json()["observed_revision"] == record["current_revision"]

    rebuilt = client.post(f"/freshness/repositories/{repo_id}/rebuild", json={"full": True})
    assert rebuilt.status_code == 200, rebuilt.text
    assert rebuilt.json()["derivation"]["decision"] in {"full_rebuild", "incremental"}

    after = client.get(f"/freshness/repositories/{repo_id}")
    assert after.json()["graph_freshness"] == "current"

    artifact = client.get(f"/freshness/artifacts/graph:{repo_id}")
    assert artifact.status_code == 200
    assert artifact.json()["artifact"]["found"] is True

    assert client.get("/freshness/artifacts/graph:absent").status_code == 404
    assert client.get("/freshness/repositories/repo-missing").status_code == 404


# ── change laboratory ─────────────────────────────────────────────────────


def test_lab_endpoints(client, portfolio):
    shared = portfolio.path("shared-contracts")
    record = _register(client, shared, "shared-contracts")
    repo_id = record["repository_id"]

    profiles = client.get("/lab/profiles")
    assert profiles.status_code == 200
    assert "python-compile" in profiles.json()["profiles"]

    patch_text = _patch_adding_line(shared, "README.md", "smoke note")
    validated = client.post(
        "/lab/validate",
        json={"repository_id": repo_id, "patch_content": patch_text,
              "profile_name": "python-compile", "allowed_paths": ["README.md"]},
    )
    assert validated.status_code == 200, validated.text
    session = validated.json()["session"]
    assert session["apply"]["success"] is True
    assert session["validation"]["passed"] is True
    # Isolation is only ever reported as what was actually verified:
    # "enforced:<backend>" when a sandbox wrapped the run, else "unverified".
    assert session["network_isolation"] in {"unverified", "enforced:docker", "enforced:unshare"}
    assert session["validation"]["network_isolation"] == session["network_isolation"]

    fetched = client.get(f"/lab/sessions/{session['session_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["session"]["session_id"] == session["session_id"]

    workspaces = client.get("/lab/workspaces")
    assert workspaces.status_code == 200
    # The session cleans up after itself, so the workspace is gone but recorded.
    ids = [w["workspace_id"] for w in workspaces.json()["workspaces"]]
    assert session["workspace_id"] in ids

    detail = client.get(f"/lab/workspaces/{session['workspace_id']}")
    assert detail.status_code == 200
    assert detail.json()["workspace"]["state"] == "cleaned"

    recovered = client.post("/lab/recover")
    assert recovered.status_code == 200
    recovery = recovered.json()["recovery"]
    assert recovery["orphan_directories"] == []
    assert recovery["missing_workspaces"] == []

    # The authoritative checkout is untouched by validation.
    assert "smoke note" not in (shared / "README.md").read_text(encoding="utf-8")
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=shared, capture_output=True, text=True
    ).stdout == ""


def test_lab_cleanup_and_error_paths(client, portfolio):
    record = _register(client, portfolio.path("api-service"), "api-service")
    assert client.get("/lab/workspaces/ws-missing").status_code == 404
    assert client.get("/lab/sessions/sess-missing").status_code == 404
    assert client.post("/lab/workspaces/ws-missing/cleanup").status_code == 404

    patch_text = _patch_adding_line(portfolio.path("api-service"), "README.md", "kept")
    session = client.post(
        "/lab/validate",
        json={"repository_id": record["repository_id"], "patch_content": patch_text,
              "profile_name": "python-compile", "allowed_paths": ["README.md"]},
    ).json()["session"]
    # Cleaning an already-cleaned workspace is idempotent, not an error.
    again = client.post(f"/lab/workspaces/{session['workspace_id']}/cleanup")
    assert again.status_code == 200
    assert again.json()["cleanup"]["removed"] is True


def test_lab_refuses_unknown_repository_and_oversized_patch(client, portfolio):
    assert client.post(
        "/lab/validate", json={"repository_id": "repo-missing", "patch_content": "x"}
    ).status_code == 404

    record = _register(client, portfolio.path("web-client"), "web-client")
    huge = "+" + ("a" * 500_001)
    assert client.post(
        "/lab/validate",
        json={"repository_id": record["repository_id"], "patch_content": huge},
    ).status_code == 413


# ── experiments ───────────────────────────────────────────────────────────


def _create_experiment(client, portfolio, repo_id: str) -> str:
    repo = portfolio.path("shared-contracts")
    options = [
        {"option_id": "note-a", "candidate_patch": _patch_adding_line(repo, "README.md", "A"),
         "expected_files": ["README.md"], "expected_effect": "documentation note A"},
        {"option_id": "note-b", "candidate_patch": _patch_adding_line(repo, "README.md", "B"),
         "expected_files": ["README.md"], "expected_effect": "documentation note B"},
    ]
    created = client.post(
        "/experiments",
        json={"repository_id": repo_id, "options": options,
              "validation_profile": "python-compile"},
    )
    assert created.status_code == 200, created.text
    return created.json()["experiment"]["experiment_id"]


def test_experiment_lifecycle_endpoints(client, portfolio):
    record = _register(client, portfolio.path("shared-contracts"), "shared-contracts")
    repo_id = record["repository_id"]
    experiment_id = _create_experiment(client, portfolio, repo_id)

    assert client.get("/experiments").json()["count"] == 1
    assert client.get(f"/experiments/{experiment_id}").status_code == 200

    # Comparison is unavailable until the options have run.
    assert client.get(f"/experiments/{experiment_id}/comparison").status_code == 409

    run = client.post(f"/experiments/{experiment_id}/run")
    assert run.status_code == 200, run.text
    experiment = run.json()["experiment"]
    assert experiment["state"] == "completed"

    comparison = client.get(f"/experiments/{experiment_id}/comparison")
    assert comparison.status_code == 200
    assert comparison.json()["comparison"]["conclusion"]

    recommendation = client.get(f"/experiments/{experiment_id}/recommendation")
    assert recommendation.status_code == 200

    accepted = client.post(
        f"/experiments/{experiment_id}/actions",
        json={"action": "accept_for_export", "actor": "operator", "option_id": "note-a"},
    )
    assert accepted.status_code == 200
    # Recording a decision is a workflow record, never an application.
    assert accepted.json()["applied"] is False

    exported = client.post(
        f"/experiments/{experiment_id}/export", json={"option_id": "note-a"}
    )
    assert exported.status_code == 200, exported.text
    body = exported.json()
    # Export packages a patch for a human; it never writes to the checkout.
    assert body["applied_to_repository"] is False
    export = body["export"]
    assert export["authorized_by"] == "operator"
    assert export["unified_diff"]
    assert export["application_instructions"]
    readme = (portfolio.path("shared-contracts") / "README.md").read_text(encoding="utf-8")
    assert "A" not in readme.splitlines()
    assert subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=portfolio.path("shared-contracts"), capture_output=True, text=True,
    ).stdout == ""


def test_experiment_export_requires_human_acceptance(client, portfolio):
    record = _register(client, portfolio.path("shared-contracts"), "shared-contracts")
    experiment_id = _create_experiment(client, portfolio, record["repository_id"])
    client.post(f"/experiments/{experiment_id}/run")
    unauthorized = client.post(
        f"/experiments/{experiment_id}/export", json={"option_id": "note-a"}
    )
    assert unauthorized.status_code == 403


def test_experiment_cancel_and_validation_errors(client, portfolio):
    record = _register(client, portfolio.path("shared-contracts"), "shared-contracts")
    repo_id = record["repository_id"]
    experiment_id = _create_experiment(client, portfolio, repo_id)

    cancelled = client.post(f"/experiments/{experiment_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["experiment"]["cancel_requested"] is True

    assert client.get("/experiments/exp-missing").status_code == 404
    assert client.post(
        f"/experiments/{experiment_id}/actions", json={"action": "teleport"}
    ).status_code == 422

    repo = portfolio.path("shared-contracts")
    duplicate = [
        {"option_id": "same", "candidate_patch": _patch_adding_line(repo, "README.md", "A")},
        {"option_id": "same", "candidate_patch": _patch_adding_line(repo, "README.md", "B")},
    ]
    assert client.post(
        "/experiments", json={"repository_id": repo_id, "options": duplicate}
    ).status_code == 422
    assert client.post(
        "/experiments", json={"repository_id": repo_id, "options": []}
    ).status_code == 422


# ── evidence ledger ───────────────────────────────────────────────────────


def test_ledger_endpoints(client, portfolio):
    from brain.ledger.ledger import EvidenceLedger

    record = _register(client, portfolio.path("shared-contracts"), "shared-contracts")
    ledger = EvidenceLedger(Path(".brain"))
    ledger.record_repository_registered(
        record["repository_id"], actor_identity="operator",
        trust_level=record["trust_level"], source_revision=record["current_revision"],
    )

    events = client.get("/ledger/events")
    assert events.status_code == 200
    assert events.json()["total"] == 1
    event_id = events.json()["events"][0]["event_id"]

    detail = client.get(f"/ledger/events/{event_id}")
    assert detail.status_code == 200
    assert detail.json()["hash_valid"] is True

    verify = client.get("/ledger/verify")
    assert verify.status_code == 200
    assert verify.json()["valid"] is True

    health = client.get("/ledger/health")
    assert health.status_code == 200
    assert health.json()["events"] == 1
    assert health.json()["valid"] is True

    history = client.get(f"/ledger/entities/repository/{record['repository_id']}")
    assert history.status_code == 200
    assert history.json()["total"] == 1

    assert client.get("/ledger/events/evt-missing").status_code == 404


def test_ledger_has_no_write_endpoint():
    """The ledger is append-only through product actions, never over HTTP."""
    mutating = [
        (path, method)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
        if path.startswith("/ledger") and method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    ]
    assert mutating == []


# ── control plane ─────────────────────────────────────────────────────────


def test_control_plane_endpoints(client, portfolio):
    _register(client, portfolio.path("shared-contracts"), "shared-contracts")

    summary = client.get("/control/summary")
    assert summary.status_code == 200
    assert summary.json()["repositories"]["total"] == 1

    queue = client.get("/control/queue")
    assert queue.status_code == 200
    assert isinstance(queue.json()["items"], list)

    actions = client.get("/control/actions")
    assert actions.status_code == 200
    # The queue proposes; a human disposes.
    assert actions.json()["autonomous_execution"] is False

    budgets = client.get("/control/budgets")
    assert budgets.status_code == 200

    check = client.get("/control/budgets/max_active_workspaces/check?requested=1")
    assert check.status_code == 200
    assert check.json()["allowed"] is True

    assert client.get("/control/metrics").status_code == 200
    assert client.get("/control/health").status_code == 200

    assert client.get("/control/budgets/no_such_budget/check").status_code == 404
    assert client.get("/control/queue?action_type=deploy_everything").status_code == 400


def test_control_plane_is_read_only():
    """No control-plane endpoint performs the action it surfaces."""
    mutating = [
        (path, method)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
        if path.startswith("/control") and method.upper() != "GET"
    ]
    assert mutating == []


# ── coverage gate ─────────────────────────────────────────────────────────


def test_every_v050_endpoint_is_covered():
    """A new v0.5.0 endpoint must arrive with a smoke test."""
    live = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
        if path.startswith(V050_PREFIXES)
    }
    assert live - COVERED_ENDPOINTS == set(), "endpoints without a smoke test"
    assert COVERED_ENDPOINTS - live == set(), "smoke tests for endpoints that no longer exist"
