"""Tests for remediation experiments — Workstream E.

Covers the experiment and option models, bounded orchestration against the real
Change Laboratory, the deterministic comparator and its disqualification rules,
recommendation semantics, the evidence package with external manifest checksum,
the human workflow, patch export, CLI, and API wiring.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from brain.experiments.models import (
    Experiment,
    ExperimentConclusion,
    ExperimentOption,
    ExperimentState,
    ExportNotAuthorizedError,
    ExportStaleError,
    HumanAction,
    HumanActionRecord,
)
from brain.experiments.orchestrator import (
    ExperimentComparator,
    ExperimentManager,
    ExperimentStore,
    _count_changed_lines,
)
from brain.experiments.packaging import (
    MANIFEST_CHECKSUM_NAME,
    MANIFEST_NAME,
    NON_AUTONOMY_STATEMENT,
    ExperimentPackager,
)
from brain.lab.laboratory import ChangeLaboratory
from tests.support import git_fixtures as gf


GOOD_PATCH = """\
diff --git a/pkg/util.py b/pkg/util.py
--- a/pkg/util.py
+++ b/pkg/util.py
@@ -1,2 +1,2 @@
 def helper(value):
-    return value * 2
+    return value * 3
"""

BROKEN_PATCH = """\
diff --git a/pkg/util.py b/pkg/util.py
--- a/pkg/util.py
+++ b/pkg/util.py
@@ -1,2 +1,2 @@
 def helper(value):
-    return value * 2
+    return value *
"""


@pytest.fixture
def repo(tmp_path) -> Path:
    return gf.make_python_repo(tmp_path, "exp-repo")


@pytest.fixture
def manager(tmp_path) -> ExperimentManager:
    brain = tmp_path / "brain"
    return ExperimentManager(ExperimentStore(brain / "experiments"), ChangeLaboratory(brain))


def _option(option_id: str, **kwargs) -> ExperimentOption:
    params = {
        "option_id": option_id,
        "candidate_patch": GOOD_PATCH,
        "patch_provenance": "test",
        "executed": True,
        "passed_required": True,
        "validation_profile": "python-compile",
    }
    params.update(kwargs)
    return ExperimentOption(**params)


def _experiment(*options: ExperimentOption, **kwargs) -> Experiment:
    params = {
        "experiment_id": "exp-unit",
        "repository_id": "exp-repo",
        "base_revision": "rev-1",
        "options": list(options),
    }
    params.update(kwargs)
    return Experiment(**params)


# ──────────────────────────────────────────────
# Phase E1 / E2 — models
# ──────────────────────────────────────────────


def test_experiment_round_trips_through_json():
    exp = _experiment(_option("a", unresolved_uncertainty=["net"]))
    exp.human_actions.append(
        HumanActionRecord(action=HumanAction.ACKNOWLEDGE.value, actor="tester")
    )

    restored = Experiment.from_dict(json.loads(json.dumps(exp.to_dict())))

    assert restored.options[0].option_id == "a"
    assert restored.options[0].unresolved_uncertainty == ["net"]
    assert restored.human_actions[0].actor == "tester"


def test_fingerprint_tracks_the_candidate_patches():
    base = _experiment(_option("a"))
    same = _experiment(_option("a"))
    different = _experiment(_option("a", candidate_patch=BROKEN_PATCH))

    assert base.compute_fingerprint() == same.compute_fingerprint()
    assert base.compute_fingerprint() != different.compute_fingerprint()


def test_fingerprint_is_independent_of_option_order():
    forward = _experiment(_option("a"), _option("b"))
    reverse = _experiment(_option("b"), _option("a"))

    assert forward.compute_fingerprint() == reverse.compute_fingerprint()


def test_store_persists_and_lists(tmp_path):
    store = ExperimentStore(tmp_path / "experiments")
    exp = store.save(_experiment(_option("a"), experiment_id="exp-abc"))

    assert exp.created_at_utc and exp.semantic_fingerprint
    assert store.get("exp-abc").options[0].option_id == "a"
    assert [e.experiment_id for e in store.list_all()] == ["exp-abc"]
    assert store.get("exp-missing") is None


# ──────────────────────────────────────────────
# Phase E4 — deterministic comparison
# ──────────────────────────────────────────────


def test_failed_required_test_disqualifies():
    exp = _experiment(
        _option("a", passed_required=False, failure_reason="pytest exited 1"),
        _option("b"),
    )
    result = ExperimentComparator.compare(exp)

    assert result.recommended_option_id == "b"
    assert [d["option_id"] for d in result.disqualified] == ["a"]
    assert result.disqualified[0]["reason"] == "pytest exited 1"


def test_new_critical_violation_disqualifies_unless_policy_allows():
    strict = _experiment(_option("a", new_critical_findings=1), _option("b"))
    result = ExperimentComparator.compare(strict)
    assert [d["option_id"] for d in result.disqualified] == ["a"]

    permissive = _experiment(
        _option("a", new_critical_findings=1),
        _option("b"),
        policy_allows_new_critical=True,
    )
    permissive_result = ExperimentComparator.compare(permissive)
    assert permissive_result.disqualified == []
    # Still marked prominently even when tolerated.
    assert any("critical" in w for w in permissive_result.warnings)
    assert permissive.get_option("a").prominent_warnings


def test_forbidden_edge_is_marked_and_disqualifying():
    exp = _experiment(_option("a", forbidden_edge_delta=2), _option("b"))
    result = ExperimentComparator.compare(exp)

    assert [d["option_id"] for d in result.disqualified] == ["a"]
    assert any("forbidden edge" in w for w in result.warnings)


def test_new_findings_without_improvement_disqualifies():
    exp = _experiment(
        _option("a", new_findings=3, architecture_improved=False),
        _option("b", new_findings=3, architecture_improved=True, resolved_findings=5),
    )
    result = ExperimentComparator.compare(exp)

    assert [d["option_id"] for d in result.disqualified] == ["a"]
    assert result.recommended_option_id == "b"


def test_unexecuted_option_is_disqualified_not_scored():
    exp = _experiment(_option("a", executed=False, passed_required=False), _option("b"))
    result = ExperimentComparator.compare(exp)

    assert result.disqualified[0]["reason"] == "Option was never executed"
    assert "a" not in result.scores


def test_comparison_prefers_fewer_changes_and_smaller_blast_radius():
    exp = _experiment(
        _option("wide", changed_lines=800, changed_files=20, impact_blast_radius=90),
        _option("narrow", changed_lines=4, changed_files=1, impact_blast_radius=2),
    )
    result = ExperimentComparator.compare(exp)

    assert result.rankings == ["narrow", "wide"]
    assert result.conclusion == ExperimentConclusion.RECOMMEND_OPTION.value
    assert result.scores["narrow"] > result.scores["wide"]


def test_normalized_contributions_are_exposed_and_bounded():
    exp = _experiment(_option("a", changed_lines=10), _option("b", changed_lines=100))
    result = ExperimentComparator.compare(exp)

    for option_id in ("a", "b"):
        contributions = result.normalized[option_id]
        assert set(contributions) == set(ExperimentComparator.WEIGHTS)
        for metric, value in contributions.items():
            assert 0.0 <= value <= ExperimentComparator.WEIGHTS[metric] + 1e-6
    assert "weighted_sum" in result.formula


def test_identical_options_are_reported_as_a_tie():
    exp = _experiment(_option("a"), _option("b"))
    result = ExperimentComparator.compare(exp)

    assert result.conclusion == ExperimentConclusion.MULTIPLE_EQUIVALENT.value
    assert sorted(result.ties[0]) == ["a", "b"]
    assert result.recommended_option_id in ("a", "b")


def test_ranking_is_deterministic_regardless_of_input_order():
    forward = ExperimentComparator.compare(
        _experiment(_option("a", changed_lines=5), _option("b", changed_lines=5))
    )
    reverse = ExperimentComparator.compare(
        _experiment(_option("b", changed_lines=5), _option("a", changed_lines=5))
    )

    assert forward.rankings == reverse.rankings
    assert forward.recommended_option_id == reverse.recommended_option_id


def test_comparison_never_consults_a_model():
    result = ExperimentComparator.compare(_experiment(_option("a")))
    assert result.comparator == "deterministic_weighted_sum"


# ──────────────────────────────────────────────
# Phase E5 — recommendation semantics
# ──────────────────────────────────────────────


def test_conclusion_all_failed_when_every_option_fails_tests():
    exp = _experiment(_option("a", passed_required=False), _option("b", passed_required=False))
    assert ExperimentComparator.compare(exp).conclusion == ExperimentConclusion.ALL_FAILED.value


def test_conclusion_no_safe_option_when_disqualified_for_other_reasons():
    exp = _experiment(
        _option("a", new_critical_findings=1), _option("b", forbidden_edge_delta=1)
    )
    assert (
        ExperimentComparator.compare(exp).conclusion
        == ExperimentConclusion.NO_SAFE_OPTION.value
    )


def test_conclusion_insufficient_evidence_without_options_or_execution():
    assert (
        ExperimentComparator.compare(_experiment()).conclusion
        == ExperimentConclusion.INSUFFICIENT_EVIDENCE.value
    )
    unexecuted = _experiment(_option("a", executed=False, passed_required=False))
    assert (
        ExperimentComparator.compare(unexecuted).conclusion
        == ExperimentConclusion.INSUFFICIENT_EVIDENCE.value
    )


def test_recommendation_does_not_approve_anything(manager, repo):
    exp = manager.create_experiment("exp-repo", gf.head(repo), [_option("a")])
    manager.complete_comparison(exp.experiment_id)

    recommendation = manager.get_recommendation(exp.experiment_id)

    assert recommendation["approval_status"] == "not_approved"
    assert "not an approval" in recommendation["note"]


# ──────────────────────────────────────────────
# Phase E3 — orchestration
# ──────────────────────────────────────────────


def test_run_executes_every_option_in_its_own_workspace(manager, repo):
    exp = manager.create_experiment(
        repository_id="exp-repo",
        base_revision=gf.head(repo),
        options=[
            ExperimentOption(
                option_id="good", candidate_patch=GOOD_PATCH, validation_profile="python-compile"
            ),
            ExperimentOption(
                option_id="broken",
                candidate_patch=BROKEN_PATCH,
                validation_profile="python-compile",
            ),
        ],
        validation_profile="python-compile",
    )

    result = manager.run_experiment(exp.experiment_id, repo)

    assert result.state == ExperimentState.COMPLETED.value
    good, broken = result.get_option("good"), result.get_option("broken")
    assert good.executed and good.passed_required
    assert broken.executed and not broken.passed_required
    assert good.workspace_id and broken.workspace_id
    assert good.workspace_id != broken.workspace_id
    assert result.conclusion == ExperimentConclusion.RECOMMEND_OPTION.value
    assert result.recommended_option_id == "good"


def test_run_leaves_the_authoritative_checkout_untouched(manager, repo):
    before = (repo / "pkg" / "util.py").read_text(encoding="utf-8")
    head_before = gf.head(repo)
    exp = manager.create_experiment(
        "exp-repo",
        head_before,
        [ExperimentOption(option_id="a", candidate_patch=GOOD_PATCH, validation_profile="python-compile")],
    )

    manager.run_experiment(exp.experiment_id, repo)

    assert (repo / "pkg" / "util.py").read_text(encoding="utf-8") == before
    assert gf.head(repo) == head_before


def test_failure_in_one_option_does_not_stop_the_others(manager, repo, monkeypatch):
    from brain.experiments import orchestrator as orch

    original = ChangeLaboratory.run_session

    def _explode(self, *args, **kwargs):
        if kwargs.get("patch") is not None and "boom" in kwargs["patch"].patch_id:
            raise RuntimeError("simulated laboratory crash")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(orch.ChangeLaboratory, "run_session", _explode)

    exp = manager.create_experiment(
        "exp-repo",
        gf.head(repo),
        [
            ExperimentOption(option_id="boom", candidate_patch=GOOD_PATCH, validation_profile="python-compile"),
            ExperimentOption(option_id="fine", candidate_patch=GOOD_PATCH, validation_profile="python-compile"),
        ],
    )
    result = manager.run_experiment(exp.experiment_id, repo)

    assert "simulated laboratory crash" in result.get_option("boom").failure_reason
    assert result.get_option("fine").passed_required is True
    assert result.recommended_option_id == "fine"


def test_resume_does_not_re_execute_finished_options(manager, repo):
    exp = manager.create_experiment(
        "exp-repo",
        gf.head(repo),
        [ExperimentOption(option_id="a", candidate_patch=GOOD_PATCH, validation_profile="python-compile")],
    )
    first = manager.run_experiment(exp.experiment_id, repo)
    workspace_id = first.get_option("a").workspace_id

    second = manager.run_experiment(exp.experiment_id, repo)

    assert second.get_option("a").workspace_id == workspace_id


def test_parallel_options_each_get_a_distinct_workspace(manager, repo):
    exp = manager.create_experiment(
        "exp-repo",
        gf.head(repo),
        [
            ExperimentOption(option_id=f"o{i}", candidate_patch=GOOD_PATCH, validation_profile="python-compile")
            for i in range(3)
        ],
        max_parallel_options=3,
    )

    result = manager.run_experiment(exp.experiment_id, repo)

    ids = [o.workspace_id for o in result.options]
    assert all(ids) and len(set(ids)) == 3


def test_cancellation_stops_the_run(manager, repo):
    exp = manager.create_experiment(
        "exp-repo",
        gf.head(repo),
        [ExperimentOption(option_id="a", candidate_patch=GOOD_PATCH, validation_profile="python-compile")],
    )
    manager.request_cancel(exp.experiment_id)

    result = manager.run_experiment(exp.experiment_id, repo)

    assert result.state == ExperimentState.CANCELLED.value
    assert result.get_option("a").executed is False


def test_stale_base_revision_invalidates_the_experiment(manager, repo):
    exp = manager.create_experiment(
        "exp-repo",
        gf.head(repo),
        [ExperimentOption(option_id="a", candidate_patch=GOOD_PATCH, validation_profile="python-compile")],
    )
    gf.write_files(repo, {"pkg/new.py": "X = 1\n"})
    gf.commit_all(repo, "move the base")

    result = manager.run_experiment(
        exp.experiment_id, repo, authoritative_revision=gf.head(repo)
    )

    assert result.state == ExperimentState.STALE.value
    assert result.conclusion == ExperimentConclusion.EXPERIMENT_STALE.value
    assert result.get_option("a").executed is False
    assert (
        manager.get_comparison(exp.experiment_id).conclusion
        == ExperimentConclusion.EXPERIMENT_STALE.value
    )


def test_changed_lines_counter_ignores_headers():
    assert _count_changed_lines(GOOD_PATCH) == 2


def test_run_records_unverified_network_isolation(manager, repo):
    exp = manager.create_experiment(
        "exp-repo",
        gf.head(repo),
        [ExperimentOption(option_id="a", candidate_patch=GOOD_PATCH, validation_profile="python-compile")],
    )
    result = manager.run_experiment(exp.experiment_id, repo)

    assert any(
        "Network isolation" in u for u in result.get_option("a").unresolved_uncertainty
    )


# ──────────────────────────────────────────────
# Phase E8 — human lifecycle
# ──────────────────────────────────────────────


def test_human_actions_are_recorded_without_applying_anything(manager, repo):
    exp = manager.create_experiment("exp-repo", gf.head(repo), [_option("a")])
    manager.complete_comparison(exp.experiment_id)

    updated = manager.record_human_action(
        exp.experiment_id, HumanAction.ACKNOWLEDGE.value, actor="alice"
    )
    updated = manager.record_human_action(
        exp.experiment_id, HumanAction.ACCEPT_FOR_EXPORT.value, actor="alice", option_id="a"
    )

    assert [a.action for a in updated.human_actions] == ["acknowledge", "accept_for_export"]
    assert updated.human_action == "accept_for_export"
    # Acceptance is a record, not an application.
    assert updated.state == ExperimentState.COMPLETED.value
    assert (repo / "pkg" / "util.py").read_text(encoding="utf-8").endswith("value * 2\n")


def test_unknown_action_and_unknown_option_are_refused(manager, repo):
    exp = manager.create_experiment("exp-repo", gf.head(repo), [_option("a")])
    manager.complete_comparison(exp.experiment_id)

    with pytest.raises(ValueError):
        manager.record_human_action(exp.experiment_id, "delete_everything")
    with pytest.raises(ValueError):
        manager.record_human_action(
            exp.experiment_id, HumanAction.REJECT.value, option_id="nope"
        )


def test_acknowledgement_requires_a_conclusion(manager, repo):
    exp = manager.create_experiment("exp-repo", gf.head(repo), [_option("a")])

    with pytest.raises(ValueError):
        manager.record_human_action(exp.experiment_id, HumanAction.ACKNOWLEDGE.value)

    # A request for another option is legitimate before any conclusion exists.
    assert manager.record_human_action(
        exp.experiment_id, HumanAction.REQUEST_ANOTHER.value
    ) is not None


# ──────────────────────────────────────────────
# Phase E9 — patch export
# ──────────────────────────────────────────────


def _accepted_experiment(manager, repo, **kwargs):
    exp = manager.create_experiment("exp-repo", gf.head(repo), [_option("a", **kwargs)])
    manager.complete_comparison(exp.experiment_id)
    manager.record_human_action(
        exp.experiment_id, HumanAction.ACCEPT_FOR_EXPORT.value, actor="alice", option_id="a"
    )
    return exp


def test_export_requires_human_acceptance(manager, repo):
    exp = manager.create_experiment("exp-repo", gf.head(repo), [_option("a")])
    manager.complete_comparison(exp.experiment_id)

    with pytest.raises(ExportNotAuthorizedError):
        manager.export_patch(exp.experiment_id, "a", gf.head(repo))


def test_export_contains_the_mandated_package(manager, repo):
    exp = _accepted_experiment(manager, repo)
    hashes = {"pkg/util.py": hashlib.sha256(b"x").hexdigest()}

    export = manager.export_patch(
        exp.experiment_id, "a", gf.head(repo), expected_source_hashes=hashes
    )

    assert export.unified_diff == GOOD_PATCH
    assert export.base_revision == gf.head(repo)
    assert export.expected_source_hashes == hashes
    assert export.validation_summary["passed_required"] is True
    assert export.architecture_comparison["cycle_delta"] == 0
    assert export.known_limitations
    assert export.application_instructions and export.rollback_instructions
    assert (
        export.artifact_checksums["unified_diff.sha256"]
        == hashlib.sha256(GOOD_PATCH.encode()).hexdigest()
    )
    assert export.authorized_by == "alice"
    assert manager.get_export(export.export_id).export_id == export.export_id


def test_export_fails_when_the_authoritative_revision_moved(manager, repo):
    exp = _accepted_experiment(manager, repo)

    with pytest.raises(ExportStaleError):
        manager.export_patch(exp.experiment_id, "a", "0" * 40)

    revalidated = manager.export_patch(
        exp.experiment_id, "a", "0" * 40, revalidated=True
    )
    assert revalidated.revalidated is True


def test_export_rejects_unknown_ids(manager, repo):
    exp = _accepted_experiment(manager, repo)

    with pytest.raises(LookupError):
        manager.export_patch("exp-nope", "a", gf.head(repo))
    with pytest.raises(LookupError):
        manager.export_patch(exp.experiment_id, "nope", gf.head(repo))


# ──────────────────────────────────────────────
# Phase E6 — experiment package
# ──────────────────────────────────────────────


def test_package_contains_every_mandated_artifact(manager, repo, tmp_path):
    exp = manager.create_experiment(
        "exp-repo",
        gf.head(repo),
        [ExperimentOption(option_id="a", candidate_patch=GOOD_PATCH, validation_profile="python-compile")],
    )
    result = manager.run_experiment(exp.experiment_id, repo)
    packager = ExperimentPackager(tmp_path / "reports")

    manifest = packager.build(
        result,
        manager.get_comparison(exp.experiment_id),
        manager.get_recommendation(exp.experiment_id),
    )
    root = packager.package_dir(exp.experiment_id)

    for relative in (
        "experiment.json",
        "comparison.json",
        "recommendation.json",
        "summary.md",
        "option-a/patch.diff",
        "option-a/validation.json",
        "option-a/architecture-before.json",
        "option-a/architecture-after.json",
        "option-a/impact.json",
        "option-a/stdout.log",
        "option-a/stderr.log",
    ):
        assert (root / relative).is_file(), relative
        assert relative in manifest["artifact_checksums"]

    assert manifest["non_autonomy_statement"] == NON_AUTONOMY_STATEMENT
    assert NON_AUTONOMY_STATEMENT in (root / "summary.md").read_text(encoding="utf-8")


def test_manifest_checksum_is_external_and_verifies(manager, repo, tmp_path):
    exp = manager.create_experiment("exp-repo", gf.head(repo), [_option("a")])
    manager.complete_comparison(exp.experiment_id)
    packager = ExperimentPackager(tmp_path / "reports")
    packager.build(manager.get(exp.experiment_id), manager.get_comparison(exp.experiment_id))
    root = packager.package_dir(exp.experiment_id)

    manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert MANIFEST_NAME not in manifest["artifact_checksums"]
    assert MANIFEST_CHECKSUM_NAME not in manifest["artifact_checksums"]

    expected = (root / MANIFEST_CHECKSUM_NAME).read_text(encoding="utf-8").split()[0]
    assert hashlib.sha256((root / MANIFEST_NAME).read_bytes()).hexdigest() == expected
    assert ExperimentPackager.verify(root)["valid"] is True


def test_package_verification_detects_tampering(manager, repo, tmp_path):
    exp = manager.create_experiment("exp-repo", gf.head(repo), [_option("a")])
    manager.complete_comparison(exp.experiment_id)
    packager = ExperimentPackager(tmp_path / "reports")
    packager.build(manager.get(exp.experiment_id), manager.get_comparison(exp.experiment_id))
    root = packager.package_dir(exp.experiment_id)

    (root / "option-a" / "patch.diff").write_text("tampered\n", encoding="utf-8")
    report = ExperimentPackager.verify(root)
    assert report["valid"] is False
    assert any("option-a/patch.diff" in issue for issue in report["issues"])

    (root / "option-a" / "patch.diff").unlink()
    assert any("missing artifact" in i for i in ExperimentPackager.verify(root)["issues"])


def test_package_verification_detects_a_rewritten_manifest(manager, repo, tmp_path):
    exp = manager.create_experiment("exp-repo", gf.head(repo), [_option("a")])
    manager.complete_comparison(exp.experiment_id)
    packager = ExperimentPackager(tmp_path / "reports")
    packager.build(manager.get(exp.experiment_id), manager.get_comparison(exp.experiment_id))
    root = packager.package_dir(exp.experiment_id)

    manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    manifest["conclusion"] = "recommend_option"
    (root / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")

    report = ExperimentPackager.verify(root)
    assert report["valid"] is False
    assert any("manifest.sha256" in issue for issue in report["issues"])


def test_package_logs_are_redacted(manager, repo, tmp_path):
    option = ExperimentOption(
        option_id="a",
        candidate_patch=GOOD_PATCH,
        results={
            "validation": {
                "results": [
                    {
                        "argv": ["python", "-V"],
                        "stdout_snippet": "API_KEY=sk-live-should-not-survive",
                        "stderr_snippet": "PASSWORD: hunter2",
                    }
                ]
            }
        },
    )
    exp = manager.create_experiment("exp-repo", gf.head(repo), [option])
    packager = ExperimentPackager(tmp_path / "reports")
    packager.build(exp)
    root = packager.package_dir(exp.experiment_id)

    assert "sk-live-should-not-survive" not in (root / "option-a" / "stdout.log").read_text(
        encoding="utf-8"
    )
    assert "hunter2" not in (root / "option-a" / "stderr.log").read_text(encoding="utf-8")


# ──────────────────────────────────────────────
# Phase E7 — CLI and API
# ──────────────────────────────────────────────


def _register(registry_dir: Path, repo: Path, capabilities=None):
    from brain.workspace.capabilities import CapabilitiesManager
    from brain.workspace.models import RepositoryRecord, TrustLevel, _generate_repository_id
    from brain.workspace.path_sandbox import PathSandbox
    from brain.workspace.registry_store import RegistryStore

    canonical = str(repo.resolve())
    record = RepositoryRecord(
        repository_id=_generate_repository_id(canonical),
        display_name=repo.name,
        canonical_root=canonical,
        normalized_root_identity=PathSandbox.get_normalized_identity(repo.resolve()),
        repository_type="git",
        default_branch="main",
        current_revision=gf.head(repo),
        trust_level=TrustLevel.TRUSTED_INTERNAL.value,
        organization="brain-tests",
        owner="platform",
        capabilities=(
            capabilities
            if capabilities is not None
            else CapabilitiesManager.get_defaults(TrustLevel.TRUSTED_INTERNAL)
        ),
        allowed_validation_profiles=["python-compile"],
    )
    return RegistryStore(registry_dir).register(record, actor="test", source="test")


def test_cli_create_run_compare_and_export(tmp_path, repo, capsys):
    from brain.experiments import cli as exp_cli

    registry_dir = tmp_path / "registry"
    record = _register(registry_dir, repo)
    patch_file = tmp_path / "option-a.diff"
    patch_file.write_text(GOOD_PATCH, encoding="utf-8")
    common = ["--brain-dir", str(tmp_path / "brain"), "--registry-dir", str(registry_dir)]

    exp_cli.main(
        common
        + [
            "create",
            "--repository",
            record.repository_id,
            "--option",
            str(patch_file),
            "--profile",
            "python-compile",
        ]
    )
    experiment_id = json.loads(capsys.readouterr().out)["experiment_id"]

    exp_cli.main(common + ["run", experiment_id])
    assert json.loads(capsys.readouterr().out)["state"] == "completed"

    exp_cli.main(common + ["compare", experiment_id])
    comparison = json.loads(capsys.readouterr().out)
    assert comparison["recommended_option_id"] == "opt-1"

    exp_cli.main(common + ["recommend", experiment_id])
    assert json.loads(capsys.readouterr().out)["approval_status"] == "not_approved"

    exp_cli.main(
        common + ["action", experiment_id, "--action", "accept_for_export", "--actor", "bob"]
    )
    assert json.loads(capsys.readouterr().out)["human_action"] == "accept_for_export"

    out_dir = tmp_path / "exported"
    exp_cli.main(
        common + ["export", experiment_id, "--option-id", "opt-1", "--out", str(out_dir)]
    )
    export = json.loads(capsys.readouterr().out)
    assert (out_dir / f"{export['export_id']}.diff").read_text(encoding="utf-8") == GOOD_PATCH
    # Exporting writes a file for a human — the checkout is untouched.
    assert "value * 2" in (repo / "pkg" / "util.py").read_text(encoding="utf-8")


def test_cli_package_and_list(tmp_path, repo, capsys):
    from brain.experiments import cli as exp_cli

    registry_dir = tmp_path / "registry"
    record = _register(registry_dir, repo)
    patch_file = tmp_path / "option-a.diff"
    patch_file.write_text(GOOD_PATCH, encoding="utf-8")
    common = ["--brain-dir", str(tmp_path / "brain"), "--registry-dir", str(registry_dir)]

    exp_cli.main(
        common
        + ["create", "--repository", record.repository_id, "--option", str(patch_file),
           "--profile", "python-compile"]
    )
    experiment_id = json.loads(capsys.readouterr().out)["experiment_id"]

    exp_cli.main(common + ["list"])
    assert json.loads(capsys.readouterr().out)["experiments"][0]["experiment_id"] == experiment_id

    exp_cli.main(
        common + ["package", experiment_id, "--reports-dir", str(tmp_path / "reports")]
    )
    payload = json.loads(capsys.readouterr().out)
    assert ExperimentPackager.verify(Path(payload["package_dir"]))["valid"] is True


def test_cli_refuses_repository_without_capability(tmp_path, repo, capsys):
    from brain.experiments import cli as exp_cli

    registry_dir = tmp_path / "registry"
    record = _register(registry_dir, repo, capabilities=["read_source"])
    patch_file = tmp_path / "option-a.diff"
    patch_file.write_text(GOOD_PATCH, encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        exp_cli.main(
            [
                "--brain-dir",
                str(tmp_path / "brain"),
                "--registry-dir",
                str(registry_dir),
                "create",
                "--repository",
                record.repository_id,
                "--option",
                str(patch_file),
            ]
        )
    assert exc.value.code == 1
    assert "lacks capability" in capsys.readouterr().err


def test_cli_cancel_marks_the_experiment(tmp_path, repo, capsys):
    from brain.experiments import cli as exp_cli

    registry_dir = tmp_path / "registry"
    record = _register(registry_dir, repo)
    patch_file = tmp_path / "option-a.diff"
    patch_file.write_text(GOOD_PATCH, encoding="utf-8")
    common = ["--brain-dir", str(tmp_path / "brain"), "--registry-dir", str(registry_dir)]

    exp_cli.main(
        common
        + ["create", "--repository", record.repository_id, "--option", str(patch_file),
           "--profile", "python-compile"]
    )
    experiment_id = json.loads(capsys.readouterr().out)["experiment_id"]

    exp_cli.main(common + ["cancel", experiment_id])
    payload = json.loads(capsys.readouterr().out)
    assert payload["cancel_requested"] is True
    assert payload["state"] == "cancelled"


def test_experiments_router_is_mounted():
    from apps.api.main import app

    paths = set(app.openapi()["paths"])
    for path in (
        "/experiments",
        "/experiments/{experiment_id}",
        "/experiments/{experiment_id}/run",
        "/experiments/{experiment_id}/cancel",
        "/experiments/{experiment_id}/comparison",
        "/experiments/{experiment_id}/recommendation",
    ):
        assert path in paths


def test_no_experiment_endpoint_promotes_a_patch():
    from apps.api.main import app

    schema = app.openapi()["paths"]
    experiment_paths = {p for p in schema if p.startswith("/experiments")}
    forbidden = {"apply", "commit", "push", "merge", "deploy", "promote"}
    for path in experiment_paths:
        assert not (forbidden & set(path.lower().split("/")))
