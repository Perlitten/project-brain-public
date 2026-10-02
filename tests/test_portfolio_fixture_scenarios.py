"""The fifty mandated fixture-portfolio scenarios.

Every scenario runs against the real five-repository fixture portfolio from
:mod:`tests.support.portfolio_fixture` — real Git repositories, real commits,
real workspaces, real processes. Nothing about Git, the filesystem, or process
execution is mocked; the failures these scenarios exist to catch only show up
against the real thing.

Scenario numbering follows the specification exactly, so a reader can map a
requirement to a test without a lookup table.
"""

from __future__ import annotations

import difflib
import hashlib
import threading
import time
from pathlib import Path

import pytest

from brain.control.budgets import BudgetExceededError, BudgetPolicy
from brain.control.plane import ControlPlane
from brain.experiments.models import (
    Experiment,
    ExperimentConclusion,
    ExperimentOption,
    ExperimentState,
    ExportNotAuthorizedError,
    ExportStaleError,
    HumanAction,
)
from brain.experiments.orchestrator import (
    ExperimentComparator,
    ExperimentManager,
    ExperimentStore,
)
from brain.freshness.generation_deriver import GenerationDeriver
from brain.freshness.incremental_planner import IncrementalPlanner
from brain.freshness.models import ArtifactType, FreshnessState, RebuildDecision
from brain.freshness.tracker import FreshnessTracker
from brain.graph.builder_v2 import GraphBuilderV2
from brain.graph.generation_manager import GraphGenerationManager
from brain.insights.remediation_models import RemediationPlan
from brain.lab.engine import (
    PatchValidator,
    ValidationRunner,
)
from brain.lab.laboratory import ChangeLaboratory
from brain.lab.models import (
    CommandDef,
    PatchRecord,
    PatchSource,
    ValidationProfile,
    WorkspaceState,
)
from brain.ledger.ledger import EvidenceLedger
from brain.ledger.verifier import (
    ALTERED_EVENT,
    BROKEN_PREVIOUS_HASH,
    MISSING_EVENT,
    REORDERED_EVENT,
    LedgerVerifier,
)
from brain.portfolio.graph_builder import (
    PortfolioCoupling,
    PortfolioGraphBuilder,
    PortfolioQualityGate,
)
from brain.portfolio.models import ContractStatus, CrossRepoRelType, PortfolioGenStatus
from brain.portfolio.store import PortfolioStore
from brain.workspace.capabilities import CapabilitiesManager, CapabilityDeniedError
from brain.workspace.models import Capability, TrustLevel
from brain.workspace.registry_store import RegistryStore
from tests.support import git_fixtures as gf
from tests.support import portfolio_fixture as pf


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────


@pytest.fixture
def portfolio(tmp_path) -> pf.FixturePortfolio:
    return pf.build_fixture_portfolio(tmp_path)


@pytest.fixture
def brain_dir(tmp_path) -> Path:
    return tmp_path / ".brain"


@pytest.fixture
def registry(brain_dir, portfolio) -> RegistryStore:
    store = RegistryStore(brain_dir / "workspace")
    for alias in pf.ROLES:
        store.register(portfolio.record(alias))
    return store


def _generation(portfolio: pf.FixturePortfolio):
    builder = PortfolioGraphBuilder(portfolio.portfolio)
    return builder, builder.build_generation(portfolio.revisions(), portfolio.generations())


def _patch_for(rel_path: str, old: str, new: str) -> str:
    """A minimal unified diff, for cases that must be rejected before apply.

    Zero-context hunks are deliberate here: these patches never reach
    ``git apply``. Use :func:`_real_patch` for anything expected to apply.
    """
    return (
        f"diff --git a/{rel_path} b/{rel_path}\n"
        f"--- a/{rel_path}\n"
        f"+++ b/{rel_path}\n"
        "@@ -1,1 +1,1 @@\n"
        f"-{old}\n"
        f"+{new}\n"
    )


def _real_patch(repo: Path, rel_path: str, old: str, new: str) -> str:
    """A unified diff against real file content, with real context lines.

    ``git apply`` refuses zero-context hunks without ``--unidiff-zero``, which
    the product deliberately does not pass, so an applicable patch has to be
    derived from the file as it actually is on disk.
    """
    before = (repo / rel_path).read_text(encoding="utf-8").splitlines(keepends=True)
    assert any(line.rstrip("\n") == old for line in before), f"{old!r} not in {rel_path}"
    after = [new + "\n" if line.rstrip("\n") == old else line for line in before]
    body = "".join(
        difflib.unified_diff(before, after, fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}")
    )
    return f"diff --git a/{rel_path} b/{rel_path}\n{body}"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ──────────────────────────────────────────────
# Scenarios 1–3 — registration and trust
# ──────────────────────────────────────────────


def test_scenario_01_clean_portfolio_registration(brain_dir, portfolio, registry):
    store = PortfolioStore(brain_dir / "portfolio")
    saved = store.save_portfolio(portfolio.portfolio)

    assert len(registry.list_all()) == 5
    assert len(saved.repositories) == 5
    assert store.get_portfolio(pf.PORTFOLIO_ID) is not None
    assert {r.alias for r in saved.repositories} == set(pf.ROLES)
    ok, message = registry.verify_integrity()
    assert ok, message


def test_scenario_02_duplicate_repository_registration(registry, portfolio):
    with pytest.raises(ValueError, match="already registered"):
        registry.register(portfolio.record("api-service"))
    assert len(registry.list_all()) == 5


def test_scenario_03_untrusted_repository_capability_denial(tmp_path):
    untrusted = pf.build_fixture_portfolio(
        tmp_path, dir_name="untrusted", trust_level=TrustLevel.UNTRUSTED_EXTERNAL
    )
    record = untrusted.record("api-service")

    assert record.capabilities == [Capability.READ_SOURCE.value]
    with pytest.raises(CapabilityDeniedError):
        CapabilitiesManager.require(
            record.repository_id, record.capabilities, Capability.EXECUTE_VALIDATION
        )
    with pytest.raises(CapabilityDeniedError):
        CapabilitiesManager.grant(
            record.capabilities,
            TrustLevel.UNTRUSTED_EXTERNAL,
            Capability.APPLY_CANDIDATE_PATCHES,
        )


# ──────────────────────────────────────────────
# Scenarios 4–7 — cross-repository impact
# ──────────────────────────────────────────────


def test_scenario_04_api_contract_change_impacts_client(portfolio):
    before = portfolio.head("api-service")
    after = pf.change_api_contract(portfolio)

    assert before != after
    assert "web-client" in pf.dependents_of(portfolio.portfolio, "api-service")
    _builder, generation = _generation(portfolio)
    calls_api = [
        r for r in generation.relationships if r.rel_type == CrossRepoRelType.CALLS_API.value
    ]
    assert [r.source_repository for r in calls_api] == [portfolio.repo_id("web-client")]
    assert calls_api[0].target_repository == portfolio.repo_id("api-service")


def test_scenario_05_shared_schema_change_impacts_api_and_worker(portfolio):
    pf.break_shared_schema(portfolio)

    impacted = pf.dependents_of(portfolio.portfolio, "shared-contracts")
    assert impacted == ["api-service", "worker-service"]
    text = (portfolio.path("shared-contracts") / "contracts" / "schema.py").read_text()
    assert "total" not in text


def test_scenario_06_event_schema_change_impacts_worker(portfolio):
    pf.change_event_schema(portfolio)

    events = (portfolio.path("shared-contracts") / "contracts" / "events.py").read_text()
    assert "orders.created.v2" in events
    consumers = [
        c.from_repo
        for c in portfolio.portfolio.contracts
        if c.dep_type == CrossRepoRelType.CONSUMES_EVENT.value
    ]
    assert consumers == ["worker-service"]


def test_scenario_07_deployment_change_impacts_multiple_services(portfolio):
    pf.change_deployment(portfolio)

    deployed = sorted(
        c.to_repo
        for c in portfolio.portfolio.contracts
        if c.dep_type == CrossRepoRelType.DEPLOYS_WITH.value
    )
    assert deployed == ["api-service", "web-client", "worker-service"]
    topology = (portfolio.path("deployment-config") / "deploy" / "services.py").read_text()
    assert "9090" in topology


# ──────────────────────────────────────────────
# Scenarios 8–10 — contract classification
# ──────────────────────────────────────────────


def test_scenario_08_forbidden_source_import_across_repositories(portfolio):
    _builder, generation = _generation(portfolio)

    forbidden = [
        r
        for r in generation.relationships
        if r.contract_status == ContractStatus.FORBIDDEN.value
    ]
    assert len(forbidden) == 1
    assert forbidden[0].source_repository == portfolio.repo_id("web-client")
    assert forbidden[0].target_repository == portfolio.repo_id("worker-service")

    quality = PortfolioQualityGate.validate(generation, portfolio.portfolio)
    assert quality["forbidden_count"] == 1
    assert any("Forbidden dependency" in w for w in quality["warnings"])
    # A forbidden edge is a warning, not a blocker: the graph must still build
    # so an operator can see the violation.
    assert quality["passed"] is True

    coupling = PortfolioCoupling.calculate(generation, portfolio.portfolio)
    assert coupling["forbidden_dependencies"] == 1


def test_scenario_09_circular_repository_dependency(portfolio):
    builder = PortfolioGraphBuilder(portfolio.portfolio)
    generation = builder.build_generation(
        portfolio.revisions(),
        portfolio.generations(),
        declared_relationships=[
            {
                "rel_type": CrossRepoRelType.CALLS_API.value,
                "source_repository": portfolio.repo_id("shared-contracts"),
                "target_repository": portfolio.repo_id("web-client"),
                "source_entity": "contracts",
                "target_entity": "client",
                "evidence": "detected import",
            }
        ],
    )

    cycles = PortfolioCoupling.calculate(generation, portfolio.portfolio)[
        "circular_dependencies"
    ]
    assert cycles, "expected a cross-repository cycle"
    members = {portfolio.alias_of(r) for cycle in cycles for r in cycle}
    assert {"shared-contracts", "web-client"} <= members


def test_scenario_10_undocumented_dependency(portfolio):
    builder = PortfolioGraphBuilder(portfolio.portfolio)
    generation = builder.build_generation(
        portfolio.revisions(),
        portfolio.generations(),
        declared_relationships=[
            {
                "rel_type": CrossRepoRelType.SHARES_DATABASE.value,
                "source_repository": portfolio.repo_id("worker-service"),
                "target_repository": portfolio.repo_id("api-service"),
                "source_entity": "worker",
                "target_entity": "api",
                "evidence": "shared connection string",
            }
        ],
    )

    undocumented = [
        r
        for r in generation.relationships
        if r.contract_status == ContractStatus.UNDOCUMENTED.value
    ]
    assert len(undocumented) == 1
    assert undocumented[0].rel_type == CrossRepoRelType.SHARES_DATABASE.value
    assert PortfolioQualityGate.validate(generation, portfolio.portfolio)[
        "undocumented_count"
    ] == 1


# ──────────────────────────────────────────────
# Scenarios 11–12 — staleness
# ──────────────────────────────────────────────


def test_scenario_11_stale_repository_graph(brain_dir, portfolio):
    tracker = FreshnessTracker(brain_dir / "freshness")
    repo_id = portfolio.repo_id("shared-contracts")
    base = portfolio.head("shared-contracts")
    tracker.record_from_repository(
        artifact_id=f"graph:{repo_id}",
        artifact_type=ArtifactType.REPOSITORY_GRAPH,
        repository_id=repo_id,
        repo_path=portfolio.path("shared-contracts"),
        source_revision=base,
        evidence="built from HEAD",
    )
    assert tracker.get(f"graph:{repo_id}").state == FreshnessState.CURRENT.value

    new_revision = pf.break_shared_schema(portfolio)
    assert new_revision != base
    tracker.invalidate(f"graph:{repo_id}", caused_by="commit", details=new_revision[:12])

    record = tracker.get(f"graph:{repo_id}")
    assert record.state == FreshnessState.STALE.value
    assert new_revision[:12] in record.reason


def test_scenario_12_stale_portfolio_graph(brain_dir, portfolio):
    tracker = FreshnessTracker(brain_dir / "freshness")
    builder, generation = _generation(portfolio)
    builder.activate(generation)
    tracker.record(
        artifact_id=f"portfolio:{pf.PORTFOLIO_ID}",
        artifact_type=ArtifactType.PORTFOLIO_GRAPH.value,
        repository_id="",
        state=FreshnessState.CURRENT,
        source_revision=portfolio.head("shared-contracts"),
        evidence=generation.generation_id,
        observed_revision=portfolio.head("shared-contracts"),
    )

    pf.break_shared_schema(portfolio)
    tracker.invalidate(
        f"portfolio:{pf.PORTFOLIO_ID}", caused_by="member repository moved"
    )
    superseded = builder.supersede(generation)

    assert tracker.get(f"portfolio:{pf.PORTFOLIO_ID}").state == FreshnessState.STALE.value
    assert superseded.status == PortfolioGenStatus.STALE.value
    assert superseded.superseded_at_utc


# ──────────────────────────────────────────────
# Scenarios 13–18 — incremental intelligence
# ──────────────────────────────────────────────


@pytest.fixture
def built_api(portfolio):
    """api-service with an active generation-1 graph."""
    repo = portfolio.path("api-service")
    meta, _report = GraphBuilderV2(repo).build_generation(gf.head(repo))
    manager = GraphGenerationManager(repo / ".brain")
    assert manager.activate_generation(meta.generation_id)
    return repo, meta.generation_id, gf.head(repo)


def test_scenario_13_incremental_file_modification(built_api, portfolio):
    repo, _gen, base = built_api
    candidate = pf.change_api_contract(portfolio)

    plan = IncrementalPlanner().plan_update(
        repo, base, candidate, repository_id="api-service"
    )

    assert plan.modified_files == ["api/routes.py"]
    assert plan.decision == RebuildDecision.INCREMENTAL.value
    assert plan.fallback_reason is None


def test_scenario_14_incremental_rename(built_api):
    repo, _gen, base = built_api
    gf.rename_file(repo, "api/routes.py", "api/orders_routes.py")
    candidate = gf.commit_all(repo, "rename routes")

    plan = IncrementalPlanner().plan_update(
        repo, base, candidate, repository_id="api-service"
    )

    assert plan.renamed_files == [("api/routes.py", "api/orders_routes.py")]
    assert "api/orders_routes.py" in plan.re_extraction_scope


def test_scenario_15_incremental_deletion(built_api):
    repo, _gen, base = built_api
    gf.delete_files(repo, ["api/service.py"])
    candidate = gf.commit_all(repo, "drop the service wrapper")

    plan = IncrementalPlanner().plan_update(
        repo, base, candidate, repository_id="api-service"
    )

    assert plan.deleted_files == ["api/service.py"]
    assert "api/service.py" not in plan.re_extraction_scope


def test_scenario_16_incremental_update_falls_back_to_full_rebuild(built_api):
    repo, gen_id, base = built_api
    # requirements.txt is infrastructure: its change invalidates assumptions the
    # incremental path cannot verify, so the planner must refuse to be clever.
    candidate = gf.commit_files(
        repo, {"requirements.txt": "shared-contracts==2.0.0\n"}, "bump the contracts package"
    )

    plan = IncrementalPlanner().plan_update(
        repo, base, candidate, repository_id="api-service"
    )
    fallback, reason = IncrementalPlanner.should_fallback(plan)

    assert fallback is True
    assert "requirements.txt" in reason

    plan.decision = RebuildDecision.FULL_REBUILD.value
    plan.fallback_reason = reason
    report = GenerationDeriver(repo, "api-service").derive(plan, gen_id, activate=False)
    assert report.decision == RebuildDecision.FULL_REBUILD.value
    assert "requirements.txt" in report.fallback_reason


def test_scenario_17_interrupted_incremental_build(brain_dir, built_api, portfolio):
    repo, gen_id, base = built_api
    repo_id = portfolio.repo_id("api-service")
    tracker = FreshnessTracker(brain_dir / "freshness")
    tracker.record(
        artifact_id=f"graph:{repo_id}",
        artifact_type=ArtifactType.REPOSITORY_GRAPH.value,
        repository_id=repo_id,
        state=FreshnessState.CURRENT,
        source_revision=base,
        evidence=gen_id,
        observed_revision=base,
    )

    candidate = pf.change_api_contract(portfolio)
    tracker.mark_building(f"graph:{repo_id}", caused_by="incremental update")
    assert tracker.get(f"graph:{repo_id}").state == FreshnessState.BUILDING.value

    # Interruption: the build never completes.
    tracker.mark_failed(f"graph:{repo_id}", reason="process interrupted")
    failed = tracker.get(f"graph:{repo_id}")
    assert failed.state == FreshnessState.FAILED.value

    # The previous generation is still the active one — an interrupted build
    # must not leave a half-derived graph serving queries.
    manager = GraphGenerationManager(repo / ".brain")
    assert manager.get_active_generation_id() == gen_id

    # And a later attempt still succeeds from the same base.
    plan = IncrementalPlanner().plan_update(
        repo, base, candidate, repository_id="api-service"
    )
    report = GenerationDeriver(repo, "api-service").derive(plan, gen_id, activate=False)
    assert report.new_generation_id


def test_scenario_18_graph_activation_rollback(built_api, portfolio):
    repo, gen_id, base = built_api
    manager = GraphGenerationManager(repo / ".brain")
    candidate = pf.change_api_contract(portfolio)

    plan = IncrementalPlanner().plan_update(
        repo, base, candidate, repository_id="api-service"
    )
    report = GenerationDeriver(repo, "api-service").derive(plan, gen_id, activate=True)
    assert manager.get_active_generation_id() == report.new_generation_id

    rolled_back = manager.rollback()
    assert rolled_back == gen_id
    assert manager.get_active_generation_id() == gen_id


# ──────────────────────────────────────────────
# Scenarios 19–23 — remediation plans and candidate patches
# ──────────────────────────────────────────────


def _two_option_plan(portfolio: pf.FixturePortfolio) -> RemediationPlan:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return RemediationPlan(
        plan_id="plan-forbidden-import",
        schema_version=1,
        repository=portfolio.repo_id("web-client"),
        source_revision=portfolio.head("web-client"),
        finding_ids=["finding-forbidden-import"],
        graph_generation="pgen-fixture",
        problem_statement="web-client imports worker source directly",
        evidence={"relationship": "web-client -> worker-service (DEPENDS_ON_PACKAGE)"},
        affected_files=["client/reporting.py"],
        affected_symbols=["summarize"],
        impacted_subsystems=["client"],
        remediation_options=[
            {
                "option_id": "opt-inline",
                "summary": "Inline the counting logic in the client",
                "risk": "low",
            },
            {
                "option_id": "opt-api",
                "summary": "Fetch the summary through the orders API",
                "risk": "medium",
            },
        ],
        recommended_option_index=1,
        implementation_steps=["Remove the worker import", "Use the API client"],
        validation_steps=["python -m compileall"],
        tests_to_run=["compileall"],
        rollback_plan=["git apply -R patch.diff"],
        risks=["The API summary endpoint may not exist yet"],
        human_approvals_required=["architecture-owner"],
        state="proposed",
        created_at_utc=now,
        updated_at_utc=now,
    )


def test_scenario_19_remediation_plan_with_two_options(portfolio):
    plan = _two_option_plan(portfolio)

    assert len(plan.remediation_options) == 2
    assert plan.recommended_option_index == 1
    assert plan.human_approvals_required == ["architecture-owner"]
    assert plan.state == "proposed"


def test_scenario_20_candidate_patch_applies_successfully(brain_dir, portfolio):
    lab = ChangeLaboratory(brain_dir)
    repo = portfolio.path("web-client")
    patch = PatchRecord(
        patch_id="patch-ok",
        source_type=PatchSource.TEST_FIXTURE.value,
        repository_id=portfolio.repo_id("web-client"),
        base_revision=portfolio.head("web-client"),
        patch_content=_real_patch(
            repo,
            "client/api_client.py",
            '"""Thin client over the orders API."""',
            '"""Thin client over the orders API (documented)."""',
        ),
        allowed_paths=["client"],
    )

    session = lab.run_session(
        repository_id=portfolio.repo_id("web-client"),
        repo_path=repo,
        base_revision=portfolio.head("web-client"),
        patch=patch,
        profile_name="python-compile",
    )

    assert session["outcome"] == "passed", session.get("rejected_reasons")
    assert session["apply"]["success"] is True
    assert session["cleanup"]["removed"] is True
    # The authoritative repository is untouched: only the workspace copy moved.
    assert '"""Thin client over the orders API."""' in (
        repo / "client" / "api_client.py"
    ).read_text(encoding="utf-8")


def test_scenario_21_stale_candidate_patch_is_rejected(brain_dir, portfolio):
    lab = ChangeLaboratory(brain_dir)
    repo = portfolio.path("web-client")
    patch = PatchRecord(
        patch_id="patch-stale",
        repository_id=portfolio.repo_id("web-client"),
        base_revision=portfolio.head("web-client"),
        patch_content=_patch_for(
            "client/api_client.py", "old line", "new line"
        ),
        expected_file_hashes={"client/api_client.py": "0" * 64},
        allowed_paths=["client"],
    )

    session = lab.run_session(
        repository_id=portfolio.repo_id("web-client"),
        repo_path=repo,
        base_revision=portfolio.head("web-client"),
        patch=patch,
        profile_name="python-compile",
    )

    assert session["outcome"] == "rejected"
    assert any("stale" in reason for reason in session["rejected_reasons"])


def test_scenario_22_patch_path_traversal_is_rejected(portfolio):
    patch = PatchRecord(
        patch_id="patch-traversal",
        repository_id=portfolio.repo_id("api-service"),
        patch_content=_patch_for("../../etc/passwd", "root", "attacker"),
    )

    reasons = PatchValidator.validate(patch)

    assert any("Path traversal" in r for r in reasons)


def test_scenario_23_candidate_patch_modifying_forbidden_path_is_rejected(portfolio):
    git_patch = PatchRecord(
        patch_id="patch-git",
        repository_id=portfolio.repo_id("api-service"),
        patch_content=_patch_for(".git/config", "[core]", "[remote]"),
    )
    out_of_scope = PatchRecord(
        patch_id="patch-scope",
        repository_id=portfolio.repo_id("api-service"),
        patch_content=_patch_for("deploy/services.py", "SERVICES = {}", "SERVICES = {'x': 1}"),
        allowed_paths=["api"],
    )

    assert any(".git" in r for r in PatchValidator.validate(git_patch))
    assert any("outside declared scope" in r for r in PatchValidator.validate(out_of_scope))


# ──────────────────────────────────────────────
# Scenarios 24–27 — validation and process supervision
# ──────────────────────────────────────────────


def test_scenario_24_validation_profile_command_is_rejected(brain_dir, portfolio):
    profile = ValidationProfile(
        profile_name="hostile",
        commands=[
            CommandDef(argv=["curl", "https://example.invalid"]),
            CommandDef(argv=["/usr/bin/python", "-c", "print(1)"]),
        ],
        environment={"API_KEY": "leak-me"},
    )

    issues = ValidationRunner.validate_profile(profile)

    assert any("not in allowlist" in i for i in issues)
    assert any("executable path not allowed" in i for i in issues)
    assert any("secret-like variable" in i for i in issues)


def test_scenario_25_validation_timeout(brain_dir, portfolio):
    manager = ChangeLaboratory(brain_dir).manager
    record = manager.create_workspace(
        repository_id=portfolio.repo_id("api-service"),
        base_revision=portfolio.head("api-service"),
        repo_path=portfolio.path("api-service"),
    )
    profile = ValidationProfile(
        profile_name="slow",
        timeout_seconds=1,
        commands=[CommandDef(argv=["python", "-c", "import time; time.sleep(30)"])],
    )

    report = ValidationRunner.run_profile(
        manager.workspace_path(record), profile, workspace_id=record.workspace_id
    )

    assert report.passed is False
    assert report.results[0]["timed_out"] is True
    manager.clean_workspace(record.workspace_id, reason="scenario complete")


def test_scenario_26_validation_cancellation(brain_dir, portfolio):
    manager = ChangeLaboratory(brain_dir).manager
    record = manager.create_workspace(
        repository_id=portfolio.repo_id("api-service"),
        base_revision=portfolio.head("api-service"),
        repo_path=portfolio.path("api-service"),
    )
    profile = ValidationProfile(
        profile_name="cancellable",
        timeout_seconds=60,
        commands=[CommandDef(argv=["python", "-c", "import time; time.sleep(30)"])],
    )
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()

    report = ValidationRunner.run_profile(
        manager.workspace_path(record),
        profile,
        cancel_event=cancel,
        workspace_id=record.workspace_id,
    )

    assert report.cancelled is True
    assert report.passed is False
    manager.clean_workspace(record.workspace_id, reason="scenario complete")


def test_scenario_27_orphan_process_cleanup(brain_dir, portfolio):
    manager = ChangeLaboratory(brain_dir).manager
    record = manager.create_workspace(
        repository_id=portfolio.repo_id("worker-service"),
        base_revision=portfolio.head("worker-service"),
        repo_path=portfolio.path("worker-service"),
    )
    # The directory vanishes underneath us — a crashed run, a manual delete.
    import shutil

    shutil.rmtree(manager.workspace_path(record))

    report = manager.recover_orphans()

    assert record.workspace_id in report.missing_workspaces
    assert manager.get_workspace(record.workspace_id).state == WorkspaceState.CLEANED.value


# ──────────────────────────────────────────────
# Scenarios 28–30 — workspace lifecycle
# ──────────────────────────────────────────────


def test_scenario_28_workspace_cleanup(brain_dir, portfolio):
    manager = ChangeLaboratory(brain_dir).manager
    record = manager.create_workspace(
        repository_id=portfolio.repo_id("api-service"),
        base_revision=portfolio.head("api-service"),
        repo_path=portfolio.path("api-service"),
    )
    path = manager.workspace_path(record)
    assert path.is_dir()

    report = manager.clean_workspace(record.workspace_id, reason="scenario complete")

    assert report.removed is True
    assert not path.exists()
    assert manager.get_workspace(record.workspace_id).state == WorkspaceState.CLEANED.value
    # The source repository is untouched.
    assert (portfolio.path("api-service") / "api" / "routes.py").is_file()


def test_scenario_29_workspace_quarantine_after_cleanup_failure(brain_dir, portfolio):
    manager = ChangeLaboratory(brain_dir).manager
    record = manager.create_workspace(
        repository_id=portfolio.repo_id("api-service"),
        base_revision=portfolio.head("api-service"),
        repo_path=portfolio.path("api-service"),
    )
    # Ownership can no longer be proven: the record now points outside the
    # managed root, so cleanup must refuse rather than delete.
    manager.update_state(
        record.workspace_id,
        WorkspaceState.FAILED,
        workspace_root=str(portfolio.path("api-service")),
    )

    report = manager.clean_workspace(record.workspace_id, reason="attempt cleanup")

    assert report.quarantined is True
    assert report.removed is False
    assert (
        manager.get_workspace(record.workspace_id).state == WorkspaceState.QUARANTINED.value
    )
    # Nothing outside the managed root was deleted.
    assert (portfolio.path("api-service") / "api" / "routes.py").is_file()


def test_scenario_30_two_simultaneous_workspace_requests(brain_dir, portfolio):
    manager = ChangeLaboratory(brain_dir).manager
    results: list = []

    def create():
        results.append(
            manager.create_workspace(
                repository_id=portfolio.repo_id("api-service"),
                base_revision=portfolio.head("api-service"),
                repo_path=portfolio.path("api-service"),
            )
        )

    threads = [threading.Thread(target=create) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert len(results) == 2
    assert results[0].workspace_id != results[1].workspace_id
    paths = {str(manager.workspace_path(r)) for r in results}
    assert len(paths) == 2
    assert all(Path(p).is_dir() for p in paths)
    for record in results:
        manager.clean_workspace(record.workspace_id, reason="scenario complete")


# ──────────────────────────────────────────────
# Scenarios 31–37 — experiments and comparison
# ──────────────────────────────────────────────


def _option(option_id: str, **kwargs) -> ExperimentOption:
    defaults = dict(executed=True, passed_required=True, architecture_improved=True)
    defaults.update(kwargs)
    return ExperimentOption(option_id=option_id, **defaults)


def _experiment_with(portfolio, *options, **kwargs) -> Experiment:
    return Experiment(
        experiment_id="exp-scenario",
        repository_id=portfolio.repo_id("web-client"),
        base_revision=portfolio.head("web-client"),
        options=list(options),
        state=ExperimentState.COMPARING.value,
        **kwargs,
    )


def test_scenario_31_experiment_with_one_passing_and_one_failing_option(portfolio):
    experiment = _experiment_with(
        portfolio,
        _option("opt-pass", resolved_findings=1),
        _option("opt-fail", passed_required=False, failure_reason="compileall failed"),
    )

    result = ExperimentComparator.compare(experiment)

    assert result.conclusion == ExperimentConclusion.RECOMMEND_OPTION.value
    assert result.recommended_option_id == "opt-pass"
    assert [d["option_id"] for d in result.disqualified] == ["opt-fail"]


def test_scenario_32_experiment_with_two_equivalent_options(portfolio):
    experiment = _experiment_with(
        portfolio,
        _option("opt-a", resolved_findings=1, changed_files=1, changed_lines=4),
        _option("opt-b", resolved_findings=1, changed_files=1, changed_lines=4),
    )

    result = ExperimentComparator.compare(experiment)

    assert result.conclusion == ExperimentConclusion.MULTIPLE_EQUIVALENT.value
    assert sorted(result.ties[0]) == ["opt-a", "opt-b"]


def test_scenario_33_experiment_where_all_options_fail(portfolio):
    experiment = _experiment_with(
        portfolio,
        _option("opt-a", passed_required=False, failure_reason="tests failed"),
        _option("opt-b", passed_required=False, failure_reason="tests failed"),
    )

    result = ExperimentComparator.compare(experiment)

    assert result.conclusion == ExperimentConclusion.ALL_FAILED.value
    assert result.recommended_option_id == ""


def test_scenario_34_experiment_invalidated_by_new_source_revision(brain_dir, portfolio):
    store = ExperimentStore(brain_dir / "experiments")
    manager = ExperimentManager(store, ChangeLaboratory(brain_dir))
    experiment = manager.create_experiment(
        repository_id=portfolio.repo_id("web-client"),
        base_revision=portfolio.head("web-client"),
        options=[_option("opt-a", executed=False)],
    )
    moved = portfolio.commit(
        "web-client", {"client/__init__.py": "# touched\n"}, "move the revision"
    )

    updated = manager.run_experiment(
        experiment.experiment_id, portfolio.path("web-client"), authoritative_revision=moved
    )

    assert updated.state == ExperimentState.STALE.value
    assert updated.conclusion == ExperimentConclusion.EXPERIMENT_STALE.value
    comparison = store.get_comparison(experiment.experiment_id)
    assert comparison.conclusion == ExperimentConclusion.EXPERIMENT_STALE.value


def test_scenario_35_option_introducing_critical_architecture_regression(portfolio):
    experiment = _experiment_with(
        portfolio,
        _option("opt-clean", resolved_findings=1),
        _option("opt-regression", new_critical_findings=2, forbidden_edge_delta=1),
    )

    result = ExperimentComparator.compare(experiment)

    disqualified = {d["option_id"]: d["reason"] for d in result.disqualified}
    assert "opt-regression" in disqualified
    assert "critical" in disqualified["opt-regression"]
    assert any("forbidden edge" in w for w in result.warnings)
    assert result.recommended_option_id == "opt-clean"


def test_scenario_36_option_resolving_issue_but_failing_required_tests(portfolio):
    experiment = _experiment_with(
        portfolio,
        _option(
            "opt-resolves-but-fails",
            passed_required=False,
            resolved_findings=3,
            failure_reason="pytest exited 1",
        ),
        _option("opt-safe", resolved_findings=1),
    )

    result = ExperimentComparator.compare(experiment)

    disqualified = {d["option_id"]: d["reason"] for d in result.disqualified}
    assert disqualified["opt-resolves-but-fails"] == "pytest exited 1"
    assert result.recommended_option_id == "opt-safe"


def test_scenario_37_option_passing_tests_and_improving_architecture(portfolio):
    experiment = _experiment_with(
        portfolio,
        _option(
            "opt-best",
            resolved_findings=3,
            forbidden_edge_delta=-1,
            coupling_delta=-0.2,
            changed_files=1,
            changed_lines=6,
        ),
        _option(
            "opt-ok",
            resolved_findings=1,
            changed_files=4,
            changed_lines=90,
        ),
    )

    result = ExperimentComparator.compare(experiment)

    assert result.conclusion == ExperimentConclusion.RECOMMEND_OPTION.value
    assert result.recommended_option_id == "opt-best"
    assert result.rankings == ["opt-best", "opt-ok"]
    assert "weighted_sum_of_min_max_normalized" in result.formula


# ──────────────────────────────────────────────
# Scenarios 38–39 — patch export
# ──────────────────────────────────────────────


@pytest.fixture
def accepted_experiment(brain_dir, portfolio):
    """A completed experiment whose winning option a human accepted."""
    store = ExperimentStore(brain_dir / "experiments")
    manager = ExperimentManager(store)
    diff = _real_patch(
        portfolio.path("web-client"),
        "client/reporting.py",
        "from worker.consumer import OrderConsumer",
        "from client.api_client import OrdersClient",
    )
    experiment = manager.create_experiment(
        repository_id=portfolio.repo_id("web-client"),
        base_revision=portfolio.head("web-client"),
        options=[_option("opt-api", candidate_patch=diff, resolved_findings=1)],
        creation_actor="alice",
    )
    manager.complete_comparison(experiment.experiment_id)
    manager.record_human_action(
        experiment.experiment_id,
        HumanAction.ACCEPT_FOR_EXPORT,
        actor="alice",
        option_id="opt-api",
    )
    return manager, experiment


def test_scenario_38_accepted_recommendation_exported_as_patch(
    accepted_experiment, portfolio
):
    manager, experiment = accepted_experiment

    export = manager.export_patch(
        experiment.experiment_id,
        "opt-api",
        authoritative_revision=portfolio.head("web-client"),
    )

    assert export.authorized_by == "alice"
    assert export.unified_diff
    assert export.artifact_checksums["unified_diff.sha256"] == hashlib.sha256(
        export.unified_diff.encode("utf-8")
    ).hexdigest()
    assert any("git apply" in step for step in export.application_instructions)
    assert any(
        "does not" in step and "commit" in step for step in export.application_instructions
    )
    # Exporting is packaging, not applying: the repository is unchanged.
    assert "from worker.consumer import OrderConsumer" in (
        portfolio.path("web-client") / "client" / "reporting.py"
    ).read_text(encoding="utf-8")


def test_scenario_39_exported_patch_stale_after_authoritative_change(
    accepted_experiment, portfolio
):
    manager, experiment = accepted_experiment
    moved = portfolio.commit(
        "web-client", {"client/__init__.py": "# moved\n"}, "authoritative change"
    )

    with pytest.raises(ExportStaleError, match="Re-validate"):
        manager.export_patch(experiment.experiment_id, "opt-api", authoritative_revision=moved)

    # An explicit human revalidation is the only way through.
    export = manager.export_patch(
        experiment.experiment_id, "opt-api", authoritative_revision=moved, revalidated=True
    )
    assert export.revalidated is True


# ──────────────────────────────────────────────
# Scenarios 40–43 — evidence ledger
# ──────────────────────────────────────────────


@pytest.fixture
def seeded_ledger(brain_dir, portfolio) -> EvidenceLedger:
    ledger = EvidenceLedger(brain_dir)
    for alias in pf.ROLES:
        ledger.record_repository_registered(
            portfolio.repo_id(alias),
            actor_identity="alice",
            trust_level=TrustLevel.FIXTURE.value,
            metadata={"display_name": alias},
        )
    return ledger


def test_scenario_40_evidence_ledger_successful_verification(seeded_ledger):
    result = seeded_ledger.verify()

    assert result["valid"] is True
    assert result["events_checked"] == 5
    assert result["issues"] == []
    assert seeded_ledger.health()["valid"] is True


def test_scenario_41_modified_ledger_event_detection(seeded_ledger):
    events = seeded_ledger.store.all_events()
    events[2].reason = "silently rewritten"

    report = LedgerVerifier.verify(events)

    assert report["valid"] is False
    assert ALTERED_EVENT in report["codes"]
    assert any(i["sequence"] == 3 for i in report["issues"] if i["code"] == ALTERED_EVENT)


def test_scenario_42_missing_ledger_event_detection(seeded_ledger):
    events = seeded_ledger.store.all_events()
    without_middle = events[:2] + events[3:]

    report = LedgerVerifier.verify(without_middle)

    assert report["valid"] is False
    assert {MISSING_EVENT, BROKEN_PREVIOUS_HASH} & set(report["codes"])


def test_scenario_43_reordered_ledger_event_detection(seeded_ledger):
    events = seeded_ledger.store.all_events()
    swapped = [events[0], events[2], events[1], events[3], events[4]]

    report = LedgerVerifier.verify(swapped)

    assert report["valid"] is False
    assert {REORDERED_EVENT, BROKEN_PREVIOUS_HASH} & set(report["codes"])


# ──────────────────────────────────────────────
# Scenarios 44–45 — control plane and budgets
# ──────────────────────────────────────────────


def test_scenario_44_control_plane_action_for_failed_experiment(
    brain_dir, portfolio, registry
):
    store = ExperimentStore(brain_dir / "experiments")
    store.save(
        Experiment(
            experiment_id="exp-failed",
            repository_id=portfolio.repo_id("web-client"),
            base_revision=portfolio.head("web-client"),
            state=ExperimentState.FAILED.value,
            creation_actor="alice",
            options=[_option("opt-a", passed_required=False)],
        )
    )

    queue = ControlPlane(brain_dir).action_queue()

    item = next(i for i in queue if i["action_type"] == "inspect_failed_experiment")
    assert item["source_entity_id"] == "exp-failed"
    assert item["repository_id"] == portfolio.repo_id("web-client")
    assert "inspect" in item["allowed_actions"]


def test_scenario_45_budget_prevents_excessive_workspace_creation(brain_dir, portfolio):
    BudgetPolicy(max_active_workspaces=2, max_workspaces_per_repository=2).save(
        brain_dir / "control" / "budgets.json"
    )
    manager = ChangeLaboratory(brain_dir).manager
    created = [
        manager.create_workspace(
            repository_id=portfolio.repo_id("api-service"),
            base_revision=portfolio.head("api-service"),
            repo_path=portfolio.path("api-service"),
        )
        for _ in range(2)
    ]

    plane = ControlPlane(brain_dir)
    assert plane.observed_usage()["max_active_workspaces"] == 2
    with pytest.raises(BudgetExceededError):
        plane.check_budget("max_active_workspaces")

    for record in created:
        manager.clean_workspace(record.workspace_id, reason="scenario complete")
    # Capacity returns once the workspaces are actually gone, not merely marked.
    assert ControlPlane(brain_dir).check_budget("max_active_workspaces").remaining >= 1


# ──────────────────────────────────────────────
# Scenarios 46–49 — access isolation
# ──────────────────────────────────────────────


def test_scenario_46_portfolio_access_isolation(brain_dir, portfolio, registry, tmp_path):
    outsider = pf.build_fixture_portfolio(tmp_path, dir_name="other-portfolio")
    outsider_id = outsider.repo_id("api-service")

    assert portfolio.portfolio.get_repo_id("api-service") == portfolio.repo_id("api-service")
    assert outsider_id not in {r.repository_id for r in portfolio.portfolio.repositories}

    # A queue filtered to one repository never leaks another repository's work.
    ExperimentStore(brain_dir / "experiments").save(
        Experiment(
            experiment_id="exp-outsider",
            repository_id=outsider_id,
            base_revision=outsider.head("api-service"),
            state=ExperimentState.FAILED.value,
            creation_actor="mallory",
        )
    )
    plane = ControlPlane(brain_dir)
    scoped = plane.action_queue(repository_id=portfolio.repo_id("api-service"))
    assert all(i["repository_id"] == portfolio.repo_id("api-service") for i in scoped)
    assert any(i["repository_id"] == outsider_id for i in plane.action_queue())


def test_scenario_47_unauthorized_experiment_start(brain_dir, tmp_path):
    untrusted = pf.build_fixture_portfolio(
        tmp_path, dir_name="untrusted-exp", trust_level=TrustLevel.UNTRUSTED_EXTERNAL
    )
    record = untrusted.record("api-service")

    with pytest.raises(CapabilityDeniedError, match="execute_validation"):
        CapabilitiesManager.require(
            record.repository_id, record.capabilities, Capability.EXECUTE_VALIDATION
        )
    assert CapabilitiesManager.check(record.capabilities, Capability.APPLY_CANDIDATE_PATCHES) is False


def test_scenario_48_unauthorized_patch_export(brain_dir, portfolio):
    store = ExperimentStore(brain_dir / "experiments")
    manager = ExperimentManager(store)
    experiment = manager.create_experiment(
        repository_id=portfolio.repo_id("web-client"),
        base_revision=portfolio.head("web-client"),
        options=[_option("opt-a", candidate_patch="diff --git a/x b/x\n")],
    )
    manager.complete_comparison(experiment.experiment_id)

    # No human accepted anything: a recommendation is not an approval.
    with pytest.raises(ExportNotAuthorizedError, match="not been accepted"):
        manager.export_patch(
            experiment.experiment_id,
            "opt-a",
            authoritative_revision=portfolio.head("web-client"),
        )


def test_scenario_49_read_only_user_can_inspect_but_not_mutate(tmp_path):
    read_only = pf.build_fixture_portfolio(
        tmp_path, dir_name="read-only", trust_level=TrustLevel.TRUSTED_READ_ONLY
    )
    caps = read_only.record("api-service").capabilities

    assert Capability.READ_SOURCE.value in caps
    assert Capability.BUILD_GRAPH.value in caps
    for denied in (
        Capability.APPLY_CANDIDATE_PATCHES,
        Capability.EXECUTE_VALIDATION,
        Capability.EXPORT_PATCH,
        Capability.MUTATE_POLICY,
        Capability.UPDATE_BASELINE,
    ):
        assert CapabilitiesManager.check(caps, denied) is False

    # And the read surfaces really are read surfaces.
    from apps.api.main import app

    schema = app.openapi()["paths"]
    for prefix in ("/control", "/ledger"):
        for path in (p for p in schema if p.startswith(prefix)):
            assert not ({"post", "put", "patch", "delete"} & set(schema[path]))


# ──────────────────────────────────────────────
# Scenario 50 — complete portfolio end-to-end lifecycle
# ──────────────────────────────────────────────


def test_scenario_50_complete_portfolio_end_to_end_lifecycle(brain_dir, portfolio):
    """register → portfolio → graphs → breaking change → stale → incremental
    → impact → plan → two workspaces → validate → recommend → accept → export
    → verify ledger → clean → no workspace leak."""
    ledger = EvidenceLedger(brain_dir)
    registry_store = RegistryStore(brain_dir / "workspace")
    tracker = FreshnessTracker(brain_dir / "freshness")

    # 1. register five repositories
    for alias in pf.ROLES:
        registry_store.register(portfolio.record(alias))
        ledger.record_repository_registered(
            portfolio.repo_id(alias),
            actor_identity="alice",
            trust_level=TrustLevel.FIXTURE.value,
            metadata={"display_name": alias},
        )
    assert len(registry_store.list_all()) == 5

    # 2. configure the portfolio
    portfolio_store = PortfolioStore(brain_dir / "portfolio")
    portfolio_store.save_portfolio(portfolio.portfolio)

    # 3. build every repository graph
    repo_generations = {}
    for alias in ("shared-contracts", "api-service", "worker-service"):
        repo = portfolio.path(alias)
        meta, _report = GraphBuilderV2(repo).build_generation(gf.head(repo))
        assert GraphGenerationManager(repo / ".brain").activate_generation(meta.generation_id)
        repo_generations[portfolio.repo_id(alias)] = meta.generation_id
        tracker.record_from_repository(
            artifact_id=f"graph:{portfolio.repo_id(alias)}",
            artifact_type=ArtifactType.REPOSITORY_GRAPH,
            repository_id=portfolio.repo_id(alias),
            repo_path=repo,
            source_revision=gf.head(repo),
            evidence=meta.generation_id,
        )

    # 4. build the portfolio graph
    builder = PortfolioGraphBuilder(portfolio.portfolio)
    generation = builder.build_generation(portfolio.revisions(), portfolio.generations())
    builder.activate(generation)
    portfolio_store.save_generation(generation)
    assert generation.status == PortfolioGenStatus.ACTIVE.value

    base_api = portfolio.head("api-service")

    # 5. introduce a breaking shared-contract change
    broken = pf.break_shared_schema(portfolio)

    # 6. mark dependent graphs and the portfolio stale
    impacted = pf.dependents_of(portfolio.portfolio, "shared-contracts")
    assert impacted == ["api-service", "worker-service"]
    for alias in impacted:
        tracker.invalidate(
            f"graph:{portfolio.repo_id(alias)}",
            caused_by="shared-contracts",
            details=broken[:12],
        )
    stale = {
        r.artifact_id for r in tracker.list_all() if r.state == FreshnessState.STALE.value
    }
    assert len(stale) == 2

    # 7. incrementally rebuild an affected repository
    candidate_api = pf.change_api_contract(portfolio)
    plan = IncrementalPlanner().plan_update(
        portfolio.path("api-service"),
        base_api,
        candidate_api,
        repository_id="api-service",
    )
    assert plan.decision == RebuildDecision.INCREMENTAL.value
    report = GenerationDeriver(portfolio.path("api-service"), "api-service").derive(
        plan, repo_generations[portfolio.repo_id("api-service")], activate=True
    )
    assert report.new_generation_id
    ledger.record_graph_generation_built(
        portfolio.repo_id("api-service"),
        report.new_generation_id,
        actor_identity="brain",
        metadata={"decision": report.decision},
    )

    # 8. detect the API and worker impact
    assert set(pf.dependents_of(portfolio.portfolio, "shared-contracts")) == {
        "api-service",
        "worker-service",
    }

    # 9. generate a remediation plan with two options
    remediation = _two_option_plan(portfolio)
    assert len(remediation.remediation_options) == 2

    # 10–13. two disposable workspaces, one candidate patch each, real validation
    lab = ChangeLaboratory(brain_dir)
    client_repo = portfolio.path("web-client")
    client_id = portfolio.repo_id("web-client")
    client_rev = portfolio.head("web-client")

    patch_a = PatchRecord(
        patch_id="patch-a",
        repository_id=client_id,
        base_revision=client_rev,
        patch_content=_real_patch(
            client_repo,
            "client/reporting.py",
            "from worker.consumer import OrderConsumer",
            "from worker.consumer import OrderConsumer  # noqa: kept",
        ),
        allowed_paths=["client"],
    )
    patch_b = PatchRecord(
        patch_id="patch-b",
        repository_id=client_id,
        base_revision=client_rev,
        patch_content=_real_patch(
            client_repo,
            "client/reporting.py",
            "from worker.consumer import OrderConsumer",
            "from client.api_client import OrdersClient",
        ),
        allowed_paths=["client"],
    )

    session_a = lab.run_session(
        repository_id=client_id,
        repo_path=client_repo,
        base_revision=client_rev,
        patch=patch_a,
        profile_name="python-compile",
        cleanup=False,
    )
    session_b = lab.run_session(
        repository_id=client_id,
        repo_path=client_repo,
        base_revision=client_rev,
        patch=patch_b,
        profile_name="python-compile",
        cleanup=False,
    )
    assert session_a["outcome"] == "passed", session_a.get("rejected_reasons")
    assert session_b["outcome"] == "passed", session_b.get("rejected_reasons")
    assert session_a["workspace_id"] != session_b["workspace_id"]

    # 14–15. patch A keeps the forbidden coupling, patch B resolves it
    option_a = _option(
        "opt-a",
        candidate_patch=patch_a.patch_content,
        forbidden_edge_delta=0,
        resolved_findings=0,
        architecture_improved=False,
        changed_files=1,
        changed_lines=2,
        validation_duration=session_a["validation"]["duration_seconds"],
    )
    option_b = _option(
        "opt-b",
        candidate_patch=patch_b.patch_content,
        forbidden_edge_delta=-1,
        resolved_findings=1,
        architecture_improved=True,
        changed_files=1,
        changed_lines=2,
        validation_duration=session_b["validation"]["duration_seconds"],
    )

    store = ExperimentStore(brain_dir / "experiments")
    manager = ExperimentManager(store)
    experiment = manager.create_experiment(
        repository_id=client_id,
        base_revision=client_rev,
        options=[option_a, option_b],
        source_plan_id=remediation.plan_id,
        creation_actor="alice",
    )

    # 16. recommend patch B
    comparison = manager.complete_comparison(experiment.experiment_id)
    assert comparison.conclusion == ExperimentConclusion.RECOMMEND_OPTION.value
    assert comparison.recommended_option_id == "opt-b"

    # 17. a human accepts the recommendation
    manager.record_human_action(
        experiment.experiment_id,
        HumanAction.ACCEPT_FOR_EXPORT,
        actor="alice",
        option_id="opt-b",
    )

    # 18–19. export patch B without applying it anywhere authoritative
    export = manager.export_patch(
        experiment.experiment_id, "opt-b", authoritative_revision=client_rev
    )
    ledger.record_patch_exported(
        client_id,
        export.export_id,
        experiment_id=experiment.experiment_id,
        option_id="opt-b",
        patch_hash=hashlib.sha256(export.unified_diff.encode("utf-8")).hexdigest(),
        actor_identity="alice",
        source_revision=client_rev,
    )
    assert export.authorized_by == "alice"
    assert "from worker.consumer import OrderConsumer" in (
        client_repo / "client" / "reporting.py"
    ).read_text(encoding="utf-8")
    exported_event = next(
        e
        for e in ledger.query(limit=100)["events"]
        if e["event_type"] == "patch_exported"
    )
    assert exported_event["metadata"]["applied_to_repository"] is False

    # 20. verify the ledger chain
    verification = ledger.verify()
    assert verification["valid"] is True
    assert verification["events_checked"] >= 7

    # 21. clean both workspaces
    for session in (session_a, session_b):
        report = lab.manager.clean_workspace(
            session["workspace_id"], reason="end-to-end scenario complete"
        )
        assert report.removed is True

    # 22. the control plane reports no workspace leak
    plane = ControlPlane(brain_dir)
    summary = plane.summary()
    assert summary["workspaces"]["active"] == 0
    assert summary["workspaces"]["quarantined"] == 0
    assert summary["repositories"]["total"] == 5
    assert summary["ledger"]["valid"] is True
    assert plane.observed_usage()["max_active_workspaces"] == 0
    assert plane.health()["status"] in {"ok", "degraded"}
