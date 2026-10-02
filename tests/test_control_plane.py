"""Focused tests for Workstream G — the operator control plane.

Sections mirror the specification phases: G3 budgets and fail-closed
enforcement, G1 operator summary, G2 human action queue, G4 health and
observability, then the CLI and API surfaces.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from brain.control.budgets import (
    BUDGET_FILE_NAME,
    BudgetEnforcer,
    BudgetExceededError,
    BudgetPolicy,
    BudgetUnknownError,
)
from brain.control.cli import main as control_cli
from brain.control.plane import (
    ALLOWED_ACTIONS,
    FORBIDDEN_ACTION_VERBS,
    ControlPlane,
    parse_timestamp,
)
from brain.experiments.models import Experiment, ExperimentOption, ExperimentState, PatchExport
from brain.experiments.orchestrator import ExperimentStore
from brain.insights.remediation_manager import RemediationPlanManager
from brain.insights.remediation_models import RemediationPlan
from brain.lab.laboratory import ChangeLaboratory
from brain.lab.models import WorkspaceState
from brain.ledger.ledger import EvidenceLedger
from brain.portfolio.models import PortfolioRecord, PortfolioRepository
from brain.portfolio.store import PortfolioStore
from brain.workspace.models import RepositoryRecord
from brain.workspace.registry_store import RegistryStore


# ── fixtures and seeding helpers ──


@pytest.fixture
def brain_dir(tmp_path) -> Path:
    return tmp_path / ".brain"


@pytest.fixture
def plane(brain_dir) -> ControlPlane:
    return ControlPlane(brain_dir)


def _source_repo(tmp_path: Path, name: str = "src") -> Path:
    root = tmp_path / name
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    return root


def _register(
    brain_dir: Path,
    repository_id: str = "repo-1",
    *,
    canonical_root: str = "",
    graph_generation_status: str = "active",
    current_revision: str = "rev1",
    owner: str = "platform",
) -> RepositoryRecord:
    record = RepositoryRecord(
        repository_id=repository_id,
        display_name=repository_id,
        canonical_root=canonical_root or str(brain_dir.parent / repository_id),
        normalized_root_identity=repository_id,
        repository_type="local",
        default_branch="main",
        current_revision=current_revision,
        trust_level="trusted_internal",
        organization="brain-tests",
        owner=owner,
        graph_generation_status=graph_generation_status,
    )
    return RegistryStore(brain_dir / "workspace").register(record)


def _plan(
    plan_id: str = "plan-1",
    repository: str = "repo-1",
    state: str = "proposed",
    source_revision: str = "rev1",
) -> RemediationPlan:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return RemediationPlan(
        plan_id=plan_id,
        schema_version=1,
        repository=repository,
        source_revision=source_revision,
        finding_ids=["find-1"],
        graph_generation="gen-1",
        problem_statement="Cycle between packages",
        evidence={},
        affected_files=["pkg/util.py"],
        affected_symbols=[],
        impacted_subsystems=[],
        remediation_options=[],
        recommended_option_index=0,
        implementation_steps=[],
        validation_steps=[],
        tests_to_run=[],
        rollback_plan=[],
        risks=[],
        human_approvals_required=["architecture-owner"],
        state=state,
        created_at_utc=now,
        updated_at_utc=now,
    )


def _experiment(
    experiment_id: str = "exp-1",
    state: str = ExperimentState.RUNNING.value,
    repository_id: str = "repo-1",
    options: int = 2,
) -> Experiment:
    return Experiment(
        experiment_id=experiment_id,
        repository_id=repository_id,
        base_revision="rev1",
        state=state,
        creation_actor="alice",
        options=[ExperimentOption(option_id=f"opt-{i}") for i in range(options)],
    )


def _export(export_id: str = "pex-1", *, stale: bool = False) -> PatchExport:
    return PatchExport(
        export_id=export_id,
        experiment_id="exp-1",
        option_id="opt-0",
        repository_id="repo-1",
        base_revision="rev1",
        unified_diff="diff --git a/pkg/util.py b/pkg/util.py\n",
        authorized_by="alice",
        exported_at_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        stale=stale,
        stale_reason="authoritative revision moved" if stale else "",
    )


def _seed_critical_finding(repo_root: Path, rule_id: str = "no-cycles") -> None:
    (repo_root / ".brain").mkdir(parents=True, exist_ok=True)
    (repo_root / ".brain" / "drift_baseline.json").write_text(
        json.dumps(
            {
                "repository_id": "repo-1",
                "source_revision": "rev1",
                "created_at_utc": "2026-01-01T00:00:00Z",
                "rule_registry_version": "1.0.0",
                "findings": [
                    {
                        "fingerprint": "fp-critical",
                        "rule_id": rule_id,
                        "file_path": "pkg/util.py",
                        "severity": "critical",
                        "description": "Forbidden import across subsystems",
                    },
                    {
                        "fingerprint": "fp-low",
                        "rule_id": "style",
                        "file_path": "pkg/util.py",
                        "severity": "low",
                        "description": "Cosmetic",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def _seed_expired_waiver(repo_root: Path) -> None:
    (repo_root / ".brain").mkdir(parents=True, exist_ok=True)
    (repo_root / ".brain" / "drift-waivers.yaml").write_text(
        "waivers:\n"
        "  - id: waiver-1\n"
        "    rule_id: no-cycles\n"
        "    owner: alice\n"
        "    reason: migration in flight\n"
        "    created_at: '2020-01-01'\n"
        "    expires_at: '2020-06-01'\n",
        encoding="utf-8",
    )


def _seed_workspace(brain_dir: Path, tmp_path: Path, state: WorkspaceState, repo: str = "repo-1"):
    lab = ChangeLaboratory(brain_dir)
    record = lab.manager.create_workspace(
        repository_id=repo, base_revision="rev1", repo_path=_source_repo(tmp_path, "src-ws")
    )
    return lab.manager.update_state(record.workspace_id, state)


# ── G3: budget policy ──


def test_policy_defines_every_mandated_budget():
    policy = BudgetPolicy().to_dict()
    assert set(policy) == {
        "max_active_workspaces",
        "max_workspaces_per_repository",
        "max_experiment_options",
        "max_simultaneous_validations",
        "max_total_retained_artifact_bytes",
        "max_failed_workspace_retention",
        "max_graph_builds",
        "max_portfolio_size",
        "max_process_output_bytes",
        "max_validation_seconds",
    }
    assert all(value > 0 for value in policy.values())


def test_policy_round_trips_through_disk(tmp_path):
    policy = BudgetPolicy(max_active_workspaces=2, max_portfolio_size=5)
    path = policy.save(tmp_path / "control" / BUDGET_FILE_NAME)
    assert BudgetPolicy.load(path) == policy


def test_missing_policy_file_yields_defaults(tmp_path):
    assert BudgetPolicy.load(tmp_path / "absent.json") == BudgetPolicy()


def test_malformed_policy_raises_rather_than_defaulting(tmp_path):
    path = tmp_path / "budgets.json"
    path.write_text(json.dumps({"max_active_workspaces": "many"}), encoding="utf-8")
    with pytest.raises(ValueError):
        BudgetPolicy.load(path)


def test_negative_budget_is_rejected():
    with pytest.raises(ValueError):
        BudgetPolicy.from_dict({"max_active_workspaces": -1})


def test_unknown_budget_keys_are_ignored():
    policy = BudgetPolicy.from_dict({"max_active_workspaces": 3, "max_unicorns": 9})
    assert policy.max_active_workspaces == 3
    assert not hasattr(policy, "max_unicorns")


# ── G3: fail-closed enforcement ──


def test_check_allows_an_action_that_fits():
    enforcer = BudgetEnforcer(BudgetPolicy(max_active_workspaces=3))
    usage = enforcer.check("max_active_workspaces", current=2)
    assert usage.remaining == 1 and usage.exceeded is False


def test_check_refuses_the_action_that_would_cross_the_limit():
    enforcer = BudgetEnforcer(BudgetPolicy(max_active_workspaces=3))
    with pytest.raises(BudgetExceededError) as exc:
        enforcer.check("max_active_workspaces", current=3)
    assert exc.value.budget == "max_active_workspaces" and exc.value.limit == 3


def test_unobservable_usage_fails_closed():
    enforcer = BudgetEnforcer(BudgetPolicy(max_active_workspaces=100))
    with pytest.raises(BudgetUnknownError):
        enforcer.check("max_active_workspaces", current=None)
    assert enforcer.allows("max_active_workspaces", None) is False


def test_unknown_budget_name_is_an_error():
    with pytest.raises(KeyError):
        BudgetEnforcer().check("max_coffee", current=0)


def test_report_exposes_usage_for_every_budget():
    enforcer = BudgetEnforcer(BudgetPolicy(max_active_workspaces=4))
    report = enforcer.report({"max_active_workspaces": 4, "max_graph_builds": None})
    assert report["fail_mode"] == "closed"
    assert len(report["usage"]) == len(BudgetPolicy().to_dict())
    assert "max_active_workspaces" in report["exceeded"]
    assert "max_graph_builds" in report["unobservable"]
    active = next(u for u in report["usage"] if u["budget"] == "max_active_workspaces")
    assert active["utilization"] == 1.0 and active["remaining"] == 0


def test_byte_and_time_budgets_report_their_unit():
    report = BudgetEnforcer().report({})
    units = {u["budget"]: u["unit"] for u in report["usage"]}
    assert units["max_total_retained_artifact_bytes"] == "bytes"
    assert units["max_process_output_bytes"] == "bytes"
    assert units["max_validation_seconds"] == "seconds"


def test_plane_reads_a_configured_policy(brain_dir):
    BudgetPolicy(max_active_workspaces=1).save(brain_dir / "control" / BUDGET_FILE_NAME)
    assert ControlPlane(brain_dir).policy.max_active_workspaces == 1


def test_live_budget_check_fails_closed_when_full(brain_dir, tmp_path):
    BudgetPolicy(max_active_workspaces=1).save(brain_dir / "control" / BUDGET_FILE_NAME)
    _seed_workspace(brain_dir, tmp_path, WorkspaceState.VALIDATING)
    plane = ControlPlane(brain_dir)
    assert plane.observed_usage()["max_active_workspaces"] == 1
    with pytest.raises(BudgetExceededError):
        plane.check_budget("max_active_workspaces")


def test_observed_usage_covers_every_budget(plane):
    assert set(plane.observed_usage()) == set(BudgetPolicy().to_dict())


# ── G1: operator summary ──


def test_summary_on_empty_state_is_well_formed(plane):
    summary = plane.summary()
    for section in (
        "repositories",
        "freshness",
        "graph_generations",
        "portfolios",
        "remediation_plans",
        "workspaces",
        "experiments",
        "exported_patches",
        "ledger",
        "critical_findings",
        "pending_review",
    ):
        assert section in summary
    assert summary["repositories"]["total"] == 0
    assert summary["ledger"]["valid"] is True


def test_summary_combines_every_subsystem(brain_dir, tmp_path):
    repo_root = _source_repo(tmp_path, "repo-1")
    _register(brain_dir, canonical_root=str(repo_root))
    _seed_critical_finding(repo_root)
    _seed_workspace(brain_dir, tmp_path, WorkspaceState.VALIDATING)
    _seed_workspace(brain_dir, tmp_path, WorkspaceState.QUARANTINED)

    experiments = ExperimentStore(brain_dir / "experiments")
    experiments.save(_experiment("exp-1", ExperimentState.RUNNING.value))
    experiments.save(_experiment("exp-2", ExperimentState.FAILED.value))
    experiments.save_export(_export("pex-1"))

    RemediationPlanManager(brain_dir).save_plan(_plan())
    PortfolioStore(brain_dir / "portfolio").save_portfolio(
        PortfolioRecord(
            portfolio_id="port-1",
            display_name="Platform",
            repositories=[PortfolioRepository(repository_id="repo-1", alias="api", role="service")],
            active_generation_id="pgen-1",
        )
    )
    EvidenceLedger(brain_dir).record_finding_created("repo-1", "find-1", severity="critical")

    summary = ControlPlane(brain_dir).summary()
    assert summary["repositories"]["total"] == 1
    assert summary["workspaces"]["active"] == 1
    assert summary["workspaces"]["quarantined"] == 1
    assert summary["experiments"]["running"] == 1
    assert summary["experiments"]["failed"] == 1
    assert summary["exported_patches"]["total"] == 1
    assert summary["remediation_plans"]["awaiting_approval"] == 1
    assert summary["portfolios"]["total"] == 1
    assert summary["portfolios"]["records"][0]["has_active_generation"] is True
    assert summary["critical_findings"]["total"] == 1
    assert summary["ledger"]["events"] == 1
    assert summary["pending_review"]["total"] > 0


def test_summary_reports_only_critical_findings(brain_dir, tmp_path):
    repo_root = _source_repo(tmp_path, "repo-1")
    _register(brain_dir, canonical_root=str(repo_root))
    _seed_critical_finding(repo_root)
    findings = ControlPlane(brain_dir).summary()["critical_findings"]["recent"]
    assert [f["fingerprint"] for f in findings] == ["fp-critical"]


def test_summary_states_the_non_autonomy_boundary(plane):
    assert (
        "It does not apply patches to authoritative repositories, commit changes, "
        "push branches, merge pull requests, or deploy software."
        in plane.summary()["non_autonomy"]
    )


def test_summary_survives_a_corrupt_subsystem(brain_dir, tmp_path):
    _register(brain_dir, canonical_root=str(_source_repo(tmp_path, "repo-1")))
    (brain_dir / "experiments").mkdir(parents=True, exist_ok=True)
    (brain_dir / "experiments" / "exp-broken.json").write_text("{ not json", encoding="utf-8")
    summary = ControlPlane(brain_dir).summary()
    assert summary["repositories"]["total"] == 1


def test_summary_caps_listed_records(brain_dir):
    for index in range(ControlPlane.MAX_LISTED + 5):
        _register(brain_dir, f"repo-{index:03d}")
    summary = ControlPlane(brain_dir).summary()
    assert summary["repositories"]["total"] == ControlPlane.MAX_LISTED + 5
    assert len(summary["repositories"]["records"]) == ControlPlane.MAX_LISTED


# ── G2: human action queue ──


def test_queue_is_empty_when_nothing_needs_a_human(plane):
    assert plane.action_queue() == []


def test_queue_items_carry_every_mandated_field(brain_dir, tmp_path):
    repo_root = _source_repo(tmp_path, "repo-1")
    _register(brain_dir, canonical_root=str(repo_root))
    _seed_critical_finding(repo_root)
    item = ControlPlane(brain_dir).action_queue()[0]
    for key in (
        "priority",
        "priority_label",
        "reason",
        "repository_id",
        "source_entity_type",
        "source_entity_id",
        "owner",
        "created_at_utc",
        "age_seconds",
        "status",
        "allowed_actions",
    ):
        assert key in item
    assert item["status"] == "open"
    assert item["allowed_actions"]


def test_critical_finding_queues_a_high_risk_review(brain_dir, tmp_path):
    repo_root = _source_repo(tmp_path, "repo-1")
    _register(brain_dir, canonical_root=str(repo_root))
    _seed_critical_finding(repo_root)
    queue = ControlPlane(brain_dir).action_queue()
    assert [i["action_type"] for i in queue] == ["review_high_risk_change"]
    assert queue[0]["priority"] == 1 and queue[0]["priority_label"] == "critical"


def test_proposed_plan_queues_an_approval_decision(brain_dir):
    _register(brain_dir)
    RemediationPlanManager(brain_dir).save_plan(_plan())
    item = next(
        i
        for i in ControlPlane(brain_dir).action_queue()
        if i["action_type"] == "approve_remediation_plan"
    )
    assert set(item["allowed_actions"]) == {"approve", "reject", "request_changes"}
    assert item["owner"] == "architecture-owner"


def test_plan_whose_revision_moved_queues_a_revalidation(brain_dir):
    _register(brain_dir, current_revision="rev2")
    RemediationPlanManager(brain_dir).save_plan(
        _plan(state="approved", source_revision="rev1")
    )
    types = {i["action_type"] for i in ControlPlane(brain_dir).action_queue()}
    assert "revalidate_stale_patch" in types


def test_failed_experiment_queues_an_inspection(brain_dir):
    _register(brain_dir)
    ExperimentStore(brain_dir / "experiments").save(
        _experiment("exp-2", ExperimentState.FAILED.value)
    )
    item = next(
        i
        for i in ControlPlane(brain_dir).action_queue()
        if i["action_type"] == "inspect_failed_experiment"
    )
    assert item["source_entity_id"] == "exp-2" and item["owner"] == "alice"


def test_quarantined_workspace_queues_a_cleanup_decision(brain_dir, tmp_path):
    _register(brain_dir)
    record = _seed_workspace(brain_dir, tmp_path, WorkspaceState.QUARANTINED)
    item = next(
        i
        for i in ControlPlane(brain_dir).action_queue()
        if i["action_type"] == "clean_quarantined_workspace"
    )
    assert item["source_entity_id"] == record.workspace_id
    assert "authorize_cleanup" in item["allowed_actions"]


def test_failed_graph_build_queues_an_investigation(brain_dir):
    _register(brain_dir, graph_generation_status="failed")
    item = next(
        i
        for i in ControlPlane(brain_dir).action_queue()
        if i["action_type"] == "investigate_graph_build_failure"
    )
    assert item["repository_id"] == "repo-1" and item["owner"] == "platform"


def test_expired_waiver_queues_a_resolution(brain_dir, tmp_path):
    repo_root = _source_repo(tmp_path, "repo-1")
    _register(brain_dir, canonical_root=str(repo_root))
    _seed_expired_waiver(repo_root)
    item = next(
        i
        for i in ControlPlane(brain_dir).action_queue()
        if i["action_type"] == "resolve_expired_waiver"
    )
    assert item["source_entity_id"] == "waiver-1" and item["owner"] == "alice"


def test_stale_export_queues_a_revalidation(brain_dir):
    _register(brain_dir)
    ExperimentStore(brain_dir / "experiments").save_export(_export("pex-stale", stale=True))
    item = next(
        i
        for i in ControlPlane(brain_dir).action_queue()
        if i["source_entity_type"] == "patch_export"
    )
    assert item["action_type"] == "revalidate_stale_patch"


def test_unverified_export_queues_a_verification(brain_dir):
    _register(brain_dir)
    ExperimentStore(brain_dir / "experiments").save_export(_export("pex-1"))
    item = next(
        i
        for i in ControlPlane(brain_dir).action_queue()
        if i["action_type"] == "verify_manually_applied_patch"
    )
    assert item["source_entity_id"] == "pex-1"


def test_recorded_verification_clears_the_queue_item(brain_dir):
    _register(brain_dir)
    ExperimentStore(brain_dir / "experiments").save_export(_export("pex-1"))
    EvidenceLedger(brain_dir).record_verification(
        "repo-1", "pex-1", passed=True, actor_identity="alice"
    )
    types = {i["action_type"] for i in ControlPlane(brain_dir).action_queue()}
    assert "verify_manually_applied_patch" not in types


def test_queue_never_offers_an_autonomous_execute_action():
    for action_type, actions in ALLOWED_ACTIONS.items():
        for action in actions:
            words = set(action.lower().replace("-", "_").split("_"))
            assert not (FORBIDDEN_ACTION_VERBS & words), f"{action_type} offers '{action}'"


def test_queue_is_ordered_by_priority_then_age(brain_dir, tmp_path):
    repo_root = _source_repo(tmp_path, "repo-1")
    _register(brain_dir, canonical_root=str(repo_root))
    _seed_critical_finding(repo_root)
    RemediationPlanManager(brain_dir).save_plan(_plan())
    ExperimentStore(brain_dir / "experiments").save_export(_export("pex-1"))
    priorities = [i["priority"] for i in ControlPlane(brain_dir).action_queue()]
    assert priorities == sorted(priorities)


def test_queue_filters_by_repository(brain_dir):
    _register(brain_dir, "repo-1", graph_generation_status="failed")
    _register(brain_dir, "repo-2", graph_generation_status="failed")
    plane = ControlPlane(brain_dir)
    assert len(plane.action_queue()) == 2
    filtered = plane.action_queue(repository_id="repo-2")
    assert len(filtered) == 1 and filtered[0]["repository_id"] == "repo-2"


def test_queue_is_deterministic_across_calls(brain_dir, tmp_path):
    repo_root = _source_repo(tmp_path, "repo-1")
    _register(brain_dir, canonical_root=str(repo_root), graph_generation_status="failed")
    _seed_critical_finding(repo_root)
    plane = ControlPlane(brain_dir)
    first = [i["item_id"] for i in plane.action_queue()]
    second = [i["item_id"] for i in plane.action_queue()]
    assert first == second


def test_age_is_measured_from_the_recorded_creation_time(brain_dir):
    _register(brain_dir)
    manager = RemediationPlanManager(brain_dir)
    plan = _plan()
    plan.created_at_utc = "2026-01-01T00:00:00Z"
    manager.save_plan(plan)
    item = next(
        i
        for i in ControlPlane(brain_dir).action_queue()
        if i["action_type"] == "approve_remediation_plan"
    )
    assert item["age_seconds"] > 0


def test_unparsable_timestamp_yields_no_age():
    assert parse_timestamp("") is None
    assert parse_timestamp("yesterday") is None
    assert parse_timestamp("2026-01-01T00:00:00Z") == 1767225600.0


# ── G4: health and observability ──


def test_metrics_expose_the_operational_counters(brain_dir, tmp_path):
    _register(brain_dir, graph_generation_status="failed")
    _seed_workspace(brain_dir, tmp_path, WorkspaceState.QUARANTINED)
    metrics = ControlPlane(brain_dir).metrics()
    assert metrics["repositories_total"] == 1
    assert metrics["graph_generations_failed"] == 1
    assert metrics["workspaces_quarantined"] == 1
    assert metrics["queue_depth"] >= 2
    assert metrics["ledger_valid"] is True
    assert set(metrics["queue_by_action"]) <= set(ALLOWED_ACTIONS)


def test_health_is_ok_on_a_quiet_system(plane):
    health = plane.health()
    assert health["status"] == "ok" and health["problems"] == []


def test_health_degrades_on_a_failed_graph_build(brain_dir):
    _register(brain_dir, graph_generation_status="failed")
    health = ControlPlane(brain_dir).health()
    assert health["status"] == "degraded"
    assert any("graph generation" in p for p in health["problems"])


def test_health_degrades_when_a_budget_is_exceeded(brain_dir, tmp_path):
    BudgetPolicy(max_active_workspaces=1).save(brain_dir / "control" / BUDGET_FILE_NAME)
    _seed_workspace(brain_dir, tmp_path, WorkspaceState.VALIDATING)
    health = ControlPlane(brain_dir).health()
    assert health["status"] == "degraded"
    assert any("budgets exceeded" in p for p in health["problems"])


# ── CLI ──


def test_cli_summary_queue_budgets_and_metrics(brain_dir, tmp_path, capsys):
    repo_root = _source_repo(tmp_path, "repo-1")
    _register(brain_dir, canonical_root=str(repo_root))
    _seed_critical_finding(repo_root)

    assert control_cli(["--brain-dir", str(brain_dir), "summary"]) == 0
    assert json.loads(capsys.readouterr().out)["repositories"]["total"] == 1

    assert control_cli(["--brain-dir", str(brain_dir), "queue"]) == 0
    queue = json.loads(capsys.readouterr().out)
    assert queue["total"] == 1 and queue["items"][0]["action_type"] == "review_high_risk_change"

    assert control_cli(["--brain-dir", str(brain_dir), "budgets"]) == 0
    assert json.loads(capsys.readouterr().out)["fail_mode"] == "closed"

    assert control_cli(["--brain-dir", str(brain_dir), "metrics"]) == 0
    assert json.loads(capsys.readouterr().out)["queue_depth"] == 1


def test_cli_queue_filters(brain_dir, tmp_path, capsys):
    repo_root = _source_repo(tmp_path, "repo-1")
    _register(brain_dir, canonical_root=str(repo_root), graph_generation_status="failed")
    _seed_critical_finding(repo_root)

    control_cli(["--brain-dir", str(brain_dir), "queue", "--max-priority", "1"])
    assert json.loads(capsys.readouterr().out)["total"] == 1

    control_cli(
        [
            "--brain-dir",
            str(brain_dir),
            "queue",
            "--action-type",
            "investigate_graph_build_failure",
        ]
    )
    assert json.loads(capsys.readouterr().out)["total"] == 1


def test_cli_check_budget_fails_the_shell_when_full(brain_dir, tmp_path, capsys):
    BudgetPolicy(max_active_workspaces=1).save(brain_dir / "control" / BUDGET_FILE_NAME)
    _seed_workspace(brain_dir, tmp_path, WorkspaceState.VALIDATING)
    assert (
        control_cli(["--brain-dir", str(brain_dir), "check-budget", "max_active_workspaces"])
        == 1
    )
    assert json.loads(capsys.readouterr().out)["allowed"] is False


def test_cli_check_budget_allows_room(brain_dir, capsys):
    assert (
        control_cli(["--brain-dir", str(brain_dir), "check-budget", "max_active_workspaces"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["allowed"] is True


def test_cli_check_unknown_budget_fails(brain_dir, capsys):
    assert control_cli(["--brain-dir", str(brain_dir), "check-budget", "max_coffee"]) == 1
    assert json.loads(capsys.readouterr().out)["allowed"] is False


def test_cli_health_exit_code_tracks_the_verdict(brain_dir, capsys):
    assert control_cli(["--brain-dir", str(brain_dir), "health"]) == 0
    capsys.readouterr()
    _register(brain_dir, graph_generation_status="failed")
    assert control_cli(["--brain-dir", str(brain_dir), "health"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "degraded"


def test_cli_write_budgets_refuses_to_clobber(brain_dir, capsys):
    assert control_cli(["--brain-dir", str(brain_dir), "write-budgets"]) == 0
    capsys.readouterr()
    assert control_cli(["--brain-dir", str(brain_dir), "write-budgets"]) == 1
    assert "error" in json.loads(capsys.readouterr().out)
    assert control_cli(["--brain-dir", str(brain_dir), "write-budgets", "--force"]) == 0


# ── API surface ──


def test_control_router_is_mounted():
    from apps.api.main import app

    paths = set(app.openapi()["paths"])
    assert {
        "/control/summary",
        "/control/queue",
        "/control/budgets",
        "/control/metrics",
        "/control/health",
    } <= paths


def test_control_api_is_read_only():
    from apps.api.main import app

    writes = {"post", "put", "patch", "delete"}
    schema = app.openapi()["paths"]
    control_paths = [p for p in schema if p.startswith("/control")]
    assert control_paths
    for path in control_paths:
        assert not (writes & set(schema[path])), f"{path} exposes a write method"


def test_control_api_exposes_no_execute_route():
    from apps.api.main import app

    forbidden = {"apply", "commit", "push", "merge", "deploy", "promote", "execute"}
    for path in app.openapi()["paths"]:
        if not path.startswith("/control"):
            continue
        assert not (forbidden & set(path.lower().split("/")))


def test_control_endpoints_require_authentication():
    from apps.api.routers import control as control_router

    assert control_router.router.dependencies


def test_control_module_documents_the_non_autonomy_boundary():
    import brain.control as package

    text = " ".join(Path(package.__file__).read_text(encoding="utf-8").split())
    assert (
        "It does not apply patches to authoritative repositories, commit changes, "
        "push branches, merge pull requests, or deploy software." in text
    )
