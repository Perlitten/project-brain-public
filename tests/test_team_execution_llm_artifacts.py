"""M2 leftovers: LLM provider consent and budget, and bounded execution artifacts."""
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from brain.config.settings import settings
from brain.execution.bounded import SubprocessBackend, WorkingCopy, build_patch, read_confined, valid_report_path
from brain.team import execution, execution_llm
from brain.team.authorization import TeamConflict, TeamDenied
from brain.team.execution import ExecutionDenied
from brain.team.models import AgentRun, AgentRunExecutionArtifact, TeamActivityEvent, TeamProject
from brain.team.provisioning import TeamNotFound
from tests.test_team_execution import (  # noqa: F401  (pytest fixtures)
    SECRET_VALUE, TOOLS, HookBackend, _all_rows_text, _git, caller, enabled, grant, make_run, payload, request,
    source_repo, team_db)
from brain.team.store import request_run

PY = sys.executable
PROVIDERS = {"openai": {"hosts": ["api.openai.com"], "models": ["gpt-fixture", "gpt-other"]},
             "local": {"hosts": ["llm.internal.example"]}}
ARTIFACT_TOOLS = {
    **TOOLS,
    # Writes a test report (with the secret in it) and prints a known line.
    "report": {"argv": [PY, "-c", "import os,pathlib; p=pathlib.Path('reports'); p.mkdir(exist_ok=True); "
                                  "(p/'junit.xml').write_text('<testsuite tests=\"1\">' + "
                                  "os.environ.get('BRAIN_SECRET_TOKEN_FILE', '') + '</testsuite>'); print('report ok')"],
               "report": "reports/junit.xml"},
    "noisy": {"argv": [PY, "-c", "print('x' * 6000); print('END-OF-LOG')"]},
    "edit": {"argv": [PY, "-c", "import pathlib; pathlib.Path('README.md').write_text('synthetic\\nchanged\\n'); "
                                "pathlib.Path('added.txt').write_text('new line without newline')"]},
}


def llm(max_tokens=500, provider="openai", model="gpt-fixture", **extra):
    return {"provider": provider, "model": model, "max_tokens": max_tokens, **extra}


@pytest.fixture
def providers(monkeypatch, enabled):
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_LLM_PROVIDERS", json.dumps(PROVIDERS))
    return enabled


async def consent(factory, project, provider="openai", model="*"):
    async with factory() as session, session.begin():
        return await execution.consent_llm(session, caller(4), project, provider, model)


async def budget(factory, project, **limits):
    async with factory() as session, session.begin():
        return await execution.set_llm_budget(session, caller(4), project, {"period": "month", **limits})


# --------------------------------------------------------------------------- LLM consent and budget


def test_llm_declaration_and_provider_config_are_validated():
    assert execution_llm.normalize_llm(None) is None
    assert execution_llm.normalize_llm(llm(10, max_cost_cents=0)) == llm(10, max_cost_cents=0)
    for bad, message in ((llm(0), "positive max_tokens"), (llm(model="*"), "one model"),
                         (dict(llm(), extra=1), "keys are"), (llm(provider="Open AI"), "lowercase"),
                         (llm(max_cost_cents=-1), "non-negative")):
        with pytest.raises(ValueError, match=message):
            execution_llm.normalize_llm(bad)
    parsed = execution_llm.parse_providers(json.dumps(PROVIDERS))
    assert parsed["openai"].hosts == {"api.openai.com"} and parsed["local"].models == frozenset()
    for raw in ("[]", "{", json.dumps({"x": {"hosts": []}}), json.dumps({"x": {"hosts": ["a"], "other": 1}})):
        with pytest.raises(ValueError):
            execution_llm.parse_providers(raw)
    # A well-known LLM API host counts even when no provider declares it; ports are ignored.
    assert execution_llm.llm_hosts(["api.anthropic.com:443", "pypi.org", "llm.internal.example"],
                                   execution_llm.parse_providers(json.dumps(PROVIDERS))) == {
        "api.anthropic.com:443": set(), "llm.internal.example": {"local"}}


@pytest.mark.asyncio
async def test_team_llm_use_needs_consent_and_budget_and_is_reserved_per_member_and_project(team_db, source_repo,
                                                                                           providers):
    factory, a, _ = team_db
    await grant(factory, a, 2)
    await grant(factory, a, 3)
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    with pytest.raises(ExecutionDenied, match="needs a positive max_tokens"):
        await request(factory, a, task_id, run_id, payload("hello", llm=llm(0)))
    with pytest.raises(ExecutionDenied, match="not configured on this Brain: other"):
        await request(factory, a, task_id, run_id, payload("hello", llm=llm(provider="other")))
    with pytest.raises(ExecutionDenied, match="model not allowed"):
        await request(factory, a, task_id, run_id, payload("hello", llm=llm(model="gpt-unlisted")))
    with pytest.raises(ExecutionDenied, match="No team administrator consent"):
        await request(factory, a, task_id, run_id, payload("hello", llm=llm()))

    async with factory() as session, session.begin():
        with pytest.raises(TeamDenied):  # consent is a team administrator's decision
            await execution.consent_llm(session, caller(2), a, "openai")
        with pytest.raises(TeamConflict, match="not configured"):
            await execution.consent_llm(session, caller(4), a, "other")
        with pytest.raises(TeamConflict, match="model not allowed"):
            await execution.consent_llm(session, caller(4), a, "openai", "gpt-unlisted")
    recorded = await consent(factory, a, model="gpt-fixture")
    assert recorded["active"] and recorded["consented_by"] == "principal:4" and recorded["version"] == 1
    with pytest.raises(ExecutionDenied, match="no LLM budget"):  # fail closed in team state
        await request(factory, a, task_id, run_id, payload("hello", llm=llm()))
    # Consent names one model; another model of the same provider is still refused.
    await budget(factory, a, max_tokens=1000, per_member_max_tokens=600)
    with pytest.raises(ExecutionDenied, match="No team administrator consent"):
        await request(factory, a, task_id, run_id, payload("hello", llm=llm(model="gpt-other")))

    state = await request(factory, a, task_id, run_id, payload("hello", llm=llm(500)))
    assert state["llm"] == llm(500) and state["state"] == "requested"
    task_2, run_2, _ = await make_run(factory, a, source_repo[1])
    with pytest.raises(ExecutionDenied, match="budget exceeded for this member: 500 of 600 tokens"):
        await request(factory, a, task_2, run_2, payload("hello", llm=llm(200)))
    task_3, run_3, _ = await make_run(factory, a, source_repo[1], who=3)
    await request(factory, a, task_3, run_3, payload("hello", llm=llm(400)), who=3)
    task_4, run_4, _ = await make_run(factory, a, source_repo[1], who=3)
    with pytest.raises(ExecutionDenied, match="budget exceeded for this project: 900 of 1000 tokens"):
        await request(factory, a, task_4, run_4, payload("hello", llm=llm(150)), who=3)

    async with factory() as session:
        report = await execution.llm_budget(session, caller(1), a)  # any member may read it
    assert report["usage"]["tokens"] == 900 and report["usage"]["executions"] == 2
    assert report["state"] == "team" and report["consent_required"] and report["budget"]["max_tokens"] == 1000
    # A cost limit needs a declared cost ceiling.
    await budget(factory, a, max_tokens=5000, max_cost_cents=100)
    with pytest.raises(ExecutionDenied, match="declare max_cost_cents"):
        await request(factory, a, task_4, run_4, payload("hello", llm=llm(150)), who=3)
    with pytest.raises(ExecutionDenied, match="LLM cost budget exceeded"):
        await request(factory, a, task_4, run_4, payload("hello", llm=llm(150, max_cost_cents=101)), who=3)
    await request(factory, a, task_4, run_4, payload("hello", llm=llm(150, max_cost_cents=100)), who=3)

    async with factory() as session:
        kinds = (await session.execute(select(TeamActivityEvent.kind).where(
            TeamActivityEvent.kind.like("execution.llm%")).order_by(TeamActivityEvent.id))).scalars().all()
    assert kinds == ["execution.llm_consented", "execution.llm_budget_set", "execution.llm_budget_set"]


@pytest.mark.asyncio
async def test_a_cancelled_unstarted_execution_releases_its_reservation(team_db, source_repo, providers):
    factory, a, _ = team_db
    await grant(factory, a)
    await consent(factory, a)
    await budget(factory, a, max_tokens=1000)
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await request(factory, a, task_id, run_id, payload("hello", llm=llm(800)))
    task_2, run_2, _ = await make_run(factory, a, source_repo[1])
    with pytest.raises(ExecutionDenied, match="800 of 1000"):
        await request(factory, a, task_2, run_2, payload("hello", llm=llm(800)))
    async with factory() as session, session.begin():
        await execution.cancel_execution(session, caller(2), a, task_id, run_id)
    assert (await execution.execute_run(run_id, factory=factory))["state"] == "cancelled"
    assert (await request(factory, a, task_2, run_2, payload("hello", llm=llm(800))))["state"] == "requested"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["consent", "budget"])
async def test_revoked_consent_or_lowered_budget_stops_a_running_execution(team_db, source_repo, providers, change):
    factory, a, _ = team_db
    await grant(factory, a)
    await consent(factory, a)
    await budget(factory, a, max_tokens=1000)
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await request(factory, a, task_id, run_id, payload({"tool": "touch", "args": ["first.txt"]},
                                                       {"tool": "touch", "args": ["second.txt"]}, llm=llm(500)))

    async def revoke():
        async with factory() as session, session.begin():
            if change == "consent":
                await execution.revoke_llm_consent(session, caller(4), a, "openai")
            else:
                await execution.set_llm_budget(session, caller(4), a, {"max_tokens": 100})

    state = await execution.execute_run(run_id, factory=factory, backend=HookBackend(hook_after=0, hook=revoke))
    assert state["state"] == "revoked", state["events"]
    reason = state["events"][-1]["payload"]["reason"]
    assert ("No team administrator consent" in reason) if change == "consent" else ("budget exceeded" in reason)
    assert list(providers.rglob("first.txt")) and not list(providers.rglob("second.txt"))


@pytest.mark.asyncio
async def test_egress_to_an_llm_host_needs_the_matching_declaration(team_db, source_repo, providers, monkeypatch):
    factory, a, _ = team_db
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_EGRESS", "api.openai.com,api.anthropic.com,pypi.org")
    await grant(factory, a)
    await consent(factory, a)
    await budget(factory, a, max_tokens=1000)
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    enforcing = SubprocessBackend()
    object.__setattr__(enforcing, "enforces_egress", True)
    with pytest.raises(ExecutionDenied, match="needs a declared LLM provider: api.openai.com"):
        await request(factory, a, task_id, run_id, payload("hello", egress=["api.openai.com"]), backend=enforcing)
    # A well-known LLM host no provider declares cannot be reached under another provider's declaration.
    with pytest.raises(ExecutionDenied, match="belongs to another LLM provider: api.anthropic.com"):
        await request(factory, a, task_id, run_id, payload("hello", egress=["api.anthropic.com"], llm=llm()),
                      backend=enforcing)
    state = await request(factory, a, task_id, run_id, payload("hello", egress=["api.openai.com", "pypi.org"],
                                                                llm=llm()), backend=enforcing)
    assert state["egress"] == ["api.openai.com", "pypi.org"] and state["llm"]["provider"] == "openai"


@pytest.mark.asyncio
async def test_child_runs_narrow_to_the_parent_llm_declaration(team_db, source_repo, providers):
    factory, a, _ = team_db
    await grant(factory, a)
    await consent(factory, a)
    await budget(factory, a, max_tokens=10000)
    task_id, parent_id, _ = await make_run(factory, a, source_repo[1])
    async with factory() as session:
        row = await session.get(AgentRun, parent_id)
    from brain.auth.principals import ResolvedPrincipal
    parent_caller = ResolvedPrincipal(row.principal_id, "run", "run", scopes=frozenset({"team:read", "team:run.request"}),
                                      credential_id=row.credential_id)

    async def child(body):
        async with factory() as session, session.begin():
            created = await request_run(session, parent_caller, a, task_id, dict(
                branch=f"brain/child-{uuid.uuid4().hex[:6]}", permissions=["read"],
                idempotency_key=f"child-{uuid.uuid4().hex}"))
        async with factory() as session, session.begin():
            return await execution.request_execution(session, parent_caller, a, task_id, uuid.UUID(created["id"]),
                                                     body)

    await request(factory, a, task_id, parent_id, payload("hello", llm=llm(500, max_cost_cents=50)))
    with pytest.raises(ExecutionDenied, match="exceeds the parent run's declaration"):
        await child(payload("hello", llm=llm(501, max_cost_cents=50)))
    with pytest.raises(ExecutionDenied, match="max_cost_cents exceeds the parent"):
        await child(payload("hello", llm=llm(100)))
    with pytest.raises(ExecutionDenied, match="a parent run declares no such LLM use"):
        await child(payload("hello", llm=llm(100, model="gpt-other", max_cost_cents=1)))
    assert (await child(payload("hello", llm=llm(100, max_cost_cents=10))))["llm"]["max_tokens"] == 100
    # A parent without an LLM declaration allows none to its children.
    task_2, parent_2, _ = await make_run(factory, a, source_repo[1])
    await request(factory, a, task_2, parent_2, payload("hello"))
    assert execution_llm.narrow(execution_llm.LlmCeiling(), None) == execution_llm.LlmCeiling(True, None)


@pytest.mark.asyncio
async def test_solo_owner_consents_implicitly_and_an_optional_budget_still_applies(team_db, source_repo, providers,
                                                                                  monkeypatch):
    factory, a, _ = team_db
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])

    async def solo(_session, _team_id=1):
        return "solo"

    monkeypatch.setattr(execution_llm, "team_state", solo)
    policy = execution.operator_policy()
    async with factory() as session, session.begin():
        project = await session.get(TeamProject, a)
        run = await session.get(AgentRun, run_id)
        # No consent row and no budget: the solo owner's implicit consent, no limit.
        await execution_llm.check(session, project=project, run=run, llm=llm(10 ** 6), egress=[],
                                  ceiling=execution_llm.LlmCeiling(), providers=policy.llm_providers)
        report = await execution_llm.budget_report(session, a)
        assert report["state"] == "solo" and not report["consent_required"] and report["budget"] is None
        # Provider configuration and egress ownership still apply.
        with pytest.raises(execution_llm.LlmDenied, match="not configured"):
            await execution_llm.check(session, project=project, run=run, llm=llm(provider="other"), egress=[],
                                      ceiling=execution_llm.LlmCeiling(), providers=policy.llm_providers)
        await execution_llm.set_budget(session, a, "operator:test", max_tokens=100)
        with pytest.raises(execution_llm.LlmDenied, match="budget exceeded for this project"):
            await execution_llm.check(session, project=project, run=run, llm=llm(101), egress=[],
                                      ceiling=execution_llm.LlmCeiling(), providers=policy.llm_providers)


@pytest.mark.asyncio
async def test_consent_and_budget_administration_round_trips(team_db, providers):
    factory, a, _ = team_db
    await consent(factory, a)
    assert (await consent(factory, a))["version"] == 1  # renewing a live consent changes nothing
    async with factory() as session, session.begin():
        revoked = await execution.revoke_llm_consent(session, caller(4), a, "openai")
        assert not revoked["active"] and revoked["revoked_by"] == "principal:4" and revoked["version"] == 2
        with pytest.raises(TeamNotFound):
            await execution.revoke_llm_consent(session, caller(4), a, "openai", "gpt-fixture")
    assert (await consent(factory, a))["version"] == 3
    async with factory() as session, session.begin():
        listed = await execution.list_llm_consents(session, caller(1), a)
        assert [(row["provider"], row["model"], row["active"]) for row in listed] == [("openai", "*", True)]
        with pytest.raises(TeamConflict, match="non-negative"):
            await execution.set_llm_budget(session, caller(4), a, {"max_tokens": -1})
        with pytest.raises(TeamConflict, match="period"):
            await execution.set_llm_budget(session, caller(4), a, {"period": "week"})
        with pytest.raises(TeamDenied):
            await execution.clear_llm_budget(session, caller(2), a)
    set_ = await budget(factory, a, max_tokens=10, per_member_max_cost_cents=5)
    assert set_["budget"]["per_member_max_cost_cents"] == 5 and set_["budget"]["version"] == 1
    async with factory() as session, session.begin():
        cleared = await execution.clear_llm_budget(session, caller(4), a)
    assert cleared["budget"] is None and cleared["budget_required"]


# --------------------------------------------------------------------------- artifacts


@pytest.fixture
def artifact_tools(monkeypatch, enabled):
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_TOOLS", json.dumps(ARTIFACT_TOOLS))
    return enabled


async def _artifacts(factory, project, task_id, run_id, who=1):
    async with factory() as session:
        listed = await execution.list_artifacts(session, caller(who), project, task_id, run_id)
        return {item["name"]: (item, (await execution.get_artifact(
            session, caller(who), project, task_id, run_id, uuid.UUID(item["id"])))[1]) for item in listed}


@pytest.mark.asyncio
async def test_execution_stores_redacted_log_report_and_patch_artifacts(team_db, source_repo, artifact_tools,
                                                                       tmp_path):
    factory, a, _ = team_db
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    await request(factory, a, task_id, run_id, payload(
        {"tool": "leak", "secrets": ["token"]}, {"tool": "report", "secrets": ["token"]}, "edit"))
    state = await execution.execute_run(run_id, factory=factory)
    assert state["state"] == "completed", state["events"]
    names = [item["name"] for item in state["artifacts"]]
    assert sorted(names) == ["final.patch", "step-000.log", "step-001-report.xml", "step-001.log", "step-002.log"]
    assert all("content" not in item for item in state["artifacts"])  # the detail lists metadata only
    artifacts = await _artifacts(factory, a, task_id, run_id)
    log_meta, log = artifacts["step-000.log"]
    assert log_meta["kind"] == "log" and log_meta["step"] == 0 and b"[REDACTED]" in log
    report_meta, report = artifacts["step-001-report.xml"]
    assert report_meta["kind"] == "test-report" and report_meta["content_type"] == "application/xml"
    assert report.startswith(b"<testsuite") and not report_meta["truncated"]
    patch_meta, patch = artifacts["final.patch"]
    assert patch_meta["kind"] == "patch" and patch_meta["step"] is None and not patch_meta["truncated"]
    assert b"+changed" in patch and b"b/added.txt" in patch and b"\\ No newline at end of file" in patch
    # The patch applies to the task base.
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(source_repo[0]), str(clone)], check=True)
    _git(clone, "checkout", "-q", source_repo[1])
    (tmp_path / "final.patch").write_bytes(patch)
    _git(clone, "apply", "--check", str(tmp_path / "final.patch"))
    for meta, content in artifacts.values():
        import hashlib
        assert meta["bytes"] == len(content) and meta["sha256"] == hashlib.sha256(content).hexdigest()
    assert SECRET_VALUE not in await _all_rows_text(factory)


@pytest.mark.asyncio
async def test_artifacts_are_capped_per_artifact_and_per_run(team_db, source_repo, artifact_tools, monkeypatch):
    factory, a, _ = team_db
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_ARTIFACT_MAX_BYTES", 1024)
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_ARTIFACTS_MAX_BYTES", 1500)
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    await request(factory, a, task_id, run_id, payload("noisy", "noisy", "noisy"))
    state = await execution.execute_run(run_id, factory=factory)
    assert state["state"] == "completed", state["events"]
    artifacts = await _artifacts(factory, a, task_id, run_id)
    first_meta, first = artifacts["step-000.log"]
    # The tail of a log is kept: that is where failures are.
    assert first_meta["truncated"] and first_meta["bytes"] == 1024 and first_meta["original_bytes"] > 6000
    assert b"END-OF-LOG" in first
    second_meta, _ = artifacts["step-001.log"]
    assert second_meta["truncated"] and second_meta["bytes"] == 1500 - 1024
    assert "step-002.log" not in artifacts  # the run's cap is spent


@pytest.mark.asyncio
async def test_a_failed_step_keeps_its_log_artifact(team_db, source_repo, artifact_tools):
    factory, a, _ = team_db
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    await request(factory, a, task_id, run_id, payload("hello", "fail"))
    state = await execution.execute_run(run_id, factory=factory)
    assert state["state"] == "failed"
    assert sorted(item["name"] for item in state["artifacts"]) == ["step-000.log", "step-001.log"]


@pytest.mark.asyncio
async def test_artifact_routes_are_for_project_members_only(team_db, source_repo, artifact_tools, monkeypatch):
    from apps.api.routers import team as team_router
    from tests.test_team_transport import ADMIN, CONTRIBUTOR, READER, client_for, membership_driven, personal_app

    factory, a, b = team_db
    membership_driven(monkeypatch)
    app = personal_app(factory, monkeypatch)
    queue = AsyncMock()
    queue.enqueue = AsyncMock(return_value="job-exec-1")
    monkeypatch.setattr(team_router, "_execution_queue", lambda: queue)
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_LLM_PROVIDERS", json.dumps(PROVIDERS))
    task_id, run_id, _ = await make_run(factory, a, source_repo[1])
    await grant(factory, a)
    base = f"/team/projects/{a}/tasks/{task_id}/runs/{run_id}/execution"
    async with client_for(app) as client:
        # LLM consent and budget through the API.
        consents = f"/team/projects/{a}/execution/llm-consents"
        assert (await client.put(consents, headers=CONTRIBUTOR, json={"provider": "openai"})).status_code == 403
        assert (await client.put(consents, headers=ADMIN, json={"provider": "nope"})).status_code == 409
        assert (await client.put(consents, headers=ADMIN, json={"provider": "openai", "x": 1})).status_code == 422
        put = await client.put(consents, headers=ADMIN, json={"provider": "openai", "model": "gpt-fixture"})
        assert put.status_code == 200 and put.json()["active"]
        assert [row["model"] for row in (await client.get(consents, headers=READER)).json()] == ["gpt-fixture"]
        budget_url = f"/team/projects/{a}/execution/llm-budget"
        assert (await client.put(budget_url, headers=CONTRIBUTOR, json={"max_tokens": 10})).status_code == 403
        assert (await client.put(budget_url, headers=ADMIN, json={"period": "week"})).status_code == 422
        assert (await client.put(budget_url, headers=ADMIN, json={"max_tokens": 1000})).json()["budget"][
            "max_tokens"] == 1000
        over = await client.post(base, headers=CONTRIBUTOR, json=payload("hello", key="http-llm-0001",
                                                                          llm=llm(1001, model="gpt-fixture")))
        assert over.status_code == 403 and "budget exceeded" in over.json()["detail"]
        assert (await client.post(base, headers=CONTRIBUTOR, json=payload(
            "hello", key="http-llm-0002", llm=dict(llm(), extra=1)))).status_code == 422
        accepted = await client.post(base, headers=CONTRIBUTOR, json=payload("edit", key="http-llm-0003", llm=llm()))
        assert accepted.status_code == 200 and accepted.json()["llm"] == llm()
        assert (await client.get(budget_url, headers=READER)).json()["usage"]["tokens"] == 500
        revoked = await client.delete(f"{consents}/openai", headers=ADMIN, params={"model": "gpt-fixture"})
        assert revoked.status_code == 200 and not revoked.json()["active"]
        assert (await client.delete(f"{consents}/openai", headers=ADMIN)).status_code == 404
        assert (await client.delete(budget_url, headers=ADMIN)).json()["budget"] is None

    # Artifacts (the consent is revoked, so execute a fresh run without LLM use).
    task_2, run_2, _ = await make_run(factory, a, source_repo[1])
    await request(factory, a, task_2, run_2, payload("edit"))
    assert (await execution.execute_run(run_2, factory=factory))["state"] == "completed"
    base_2 = f"/team/projects/{a}/tasks/{task_2}/runs/{run_2}/execution"
    async with client_for(app) as client:
        listed = await client.get(f"{base_2}/artifacts", headers=READER)
        assert listed.status_code == 200
        by_name = {item["name"]: item for item in listed.json()}
        assert set(by_name) == {"step-000.log", "final.patch"}
        detail = (await client.get(base_2, headers=READER)).json()
        assert [item["name"] for item in detail["artifacts"]] == [item["name"] for item in listed.json()]
        download = await client.get(f"{base_2}/artifacts/{by_name['final.patch']['id']}", headers=READER)
        assert download.status_code == 200 and b"+changed" in download.content
        assert download.headers["content-disposition"] == 'attachment; filename="final.patch"'
        assert download.headers["x-content-type-options"] == "nosniff"
        assert download.headers["content-type"].startswith("text/x-diff")
        assert download.headers["x-artifact-sha256"] == by_name["final.patch"]["sha256"]
        assert (await client.get(f"{base_2}/artifacts/{uuid.uuid4()}", headers=READER)).status_code == 404
        # Another run's artifact id, or a project the caller is not a member of, reveals nothing.
        other = f"/team/projects/{a}/tasks/{task_id}/runs/{run_id}/execution/artifacts/{by_name['final.patch']['id']}"
        assert (await client.get(other, headers=READER)).status_code == 404
        foreign = f"/team/projects/{b}/tasks/{task_2}/runs/{run_2}/execution/artifacts"
        assert (await client.get(foreign, headers=READER)).status_code in {403, 404}
        assert (await client.get(f"{base_2}/artifacts", headers={"X-API-Key": "fixture-9"})).status_code == 401


# --------------------------------------------------------------------------- patch building (both backends)


def _copy(tmp_path, repo, base):
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    return WorkingCopy(run_dir=run_dir, path=repo, branch="main", base_sha=base)


def test_patch_covers_tracked_untracked_and_binary_changes_and_applies(source_repo, tmp_path):
    repo, base = source_repo
    (repo / "README.md").write_bytes(b"synthetic\nmore\n")  # bytes: no Windows newline translation
    (repo / "sub").mkdir()
    (repo / "sub" / "new.txt").write_bytes(b"a\nb\n")
    (repo / "empty.txt").write_text("")
    (repo / "blob.bin").write_bytes(b"\x00\x01binary")
    (repo / ".gitignore").write_text("ignored.log\n")
    (repo / "ignored.log").write_text("never in the patch")
    patch, truncated = SubprocessBackend().patch(_copy(tmp_path, repo, base), 1 << 20)
    assert not truncated
    assert b"+more" in patch and b"diff --git a/sub/new.txt b/sub/new.txt\nnew file mode 100644\n" in patch
    assert b"@@ -0,0 +1,2 @@\n+a\n+b\n" in patch and b"diff --git a/empty.txt b/empty.txt\nnew file mode" in patch
    assert b"Binary files /dev/null and b/blob.bin differ" in patch and b"ignored.log" not in patch.replace(
        b"+ignored.log", b"")
    # Without the binary stub the patch applies to the base as is.
    text_only = b"diff --git".join(part for part in patch.split(b"diff --git") if b"blob.bin" not in part)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True)
    _git(clone, "checkout", "-q", base)
    (tmp_path / "p.diff").write_bytes(text_only)
    _git(clone, "apply", "--check", str(tmp_path / "p.diff"))
    small, cut = SubprocessBackend().patch(_copy(tmp_path, repo, base), 64)
    assert cut and len(small) == 64 and patch.startswith(small)


def test_build_patch_and_confined_reads_never_follow_symlinks(tmp_path):
    root = tmp_path / "copy"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret")
    (root / "a.txt").write_text("one")
    assert read_confined(root, "a.txt", 10)[0] == b"one"
    for bad in ("../outside.txt", "/etc/passwd", "a/../a.txt", ""):
        assert read_confined(root, bad, 10) is None
    assert valid_report_path("reports/junit.xml") and not valid_report_path("../x") and not valid_report_path("/x")
    try:
        os.symlink(outside, root / "link.txt")
        os.symlink(tmp_path, root / "dir", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    assert read_confined(root, "link.txt", 100)[2] == "symlink"
    assert read_confined(root, "dir/outside.txt", 100) is None
    patch, _ = build_patch(b"", 0, root, ["link.txt", "dir/outside.txt", "missing.txt"], 4096)
    assert b"new file mode 120000" in patch and b"outside secret" not in patch


@dataclass
class _Copy:
    path: Path


def test_report_artifacts_skip_symlinks_and_are_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BRAIN_EXECUTION_ARTIFACT_MAX_BYTES", 1024)
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "out.json").write_text("{" + "x" * 9000 + "}")
    tool = execution.ToolSpec(name="t", argv=("t",), report="reports/out.json")
    drafts = execution.step_artifacts(_Copy(tmp_path), tool, 3, None)
    assert [(d.name, d.kind, d.content_type, d.truncated) for d in drafts] == [
        ("step-003-report.json", "test-report", "application/json", True)]
    assert len(drafts[0].content) <= 1024 + 4096 and drafts[0].original_bytes == 9002


def test_cli_records_consent_and_budget(team_db, providers, monkeypatch):
    from typer.testing import CliRunner

    from apps.cli.commands import team as team_commands

    factory, a, _ = team_db
    monkeypatch.setattr(team_commands, "async_session_factory", factory)
    runner = CliRunner()
    cli = team_commands.team_cli
    granted = runner.invoke(cli, ["execution", "consent", "grant", "--project", str(a), "--provider", "openai"])
    assert granted.exit_code == 0, granted.output
    assert json.loads(granted.output)["consented_by"] == "operator:cli"
    unknown = runner.invoke(cli, ["execution", "consent", "grant", "--project", str(a), "--provider", "nope"])
    assert unknown.exit_code != 0 and "not configured" in unknown.output
    listed = json.loads(runner.invoke(cli, ["execution", "consent", "list", "--project", str(a)]).output)
    assert [(row["provider"], row["active"]) for row in listed] == [("openai", True)]
    set_ = runner.invoke(cli, ["execution", "budget", "set", "--project", str(a), "--max-tokens", "1000",
                               "--per-member-max-tokens", "400"])
    assert set_.exit_code == 0, set_.output
    assert json.loads(set_.output)["budget"]["per_member_max_tokens"] == 400
    shown = json.loads(runner.invoke(cli, ["execution", "budget", "show", "--project", str(a)]).output)
    assert shown["usage"]["tokens"] == 0 and shown["state"] == "team"
    revoked = runner.invoke(cli, ["execution", "consent", "revoke", "--project", str(a), "--provider", "openai"])
    assert revoked.exit_code == 0 and not json.loads(revoked.output)["active"]
    cleared = json.loads(runner.invoke(cli, ["execution", "budget", "clear", "--project", str(a)]).output)
    assert cleared["budget"] is None
