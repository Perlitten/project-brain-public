"""Enforcement Decision Engine for Architectural Change Guard (Phase 6).

Evaluates change-sets, applies policy rules and waivers, and returns deterministic exit codes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from brain.insights.drift_analyzer import DriftFinding, scan_repository_drift
from brain.insights.drift_baseline import DriftBaselineManager
from brain.insights.drift_policy import ArchitecturePolicy
from brain.insights.drift_rules import DriftRuleRegistry, get_default_rule_registry
from brain.insights.drift_waivers import DriftWaiver, DriftWaiverManager
from brain.insights.git_diff import GitDiffEngine

# Deterministic Exit Codes
EXIT_SUCCESS = 0
EXIT_POLICY_VIOLATION = 2
EXIT_INVALID_CONFIG = 3
EXIT_REPO_REVISION_ERROR = 4
EXIT_INTERNAL_FAILURE = 5


@dataclass
class EvaluatedGuardFinding:
    finding: DriftFinding
    delta_state: str  # 'new', 'persistent', 'moved', 'resolved'
    waiver_status: str  # 'active', 'waived', 'waiver_expired'
    applied_waiver: Optional[DriftWaiver] = None
    is_from_dependency_expansion: bool = False

    def to_dict(self) -> dict[str, Any]:
        res = {
            "finding": self.finding.to_dict(),
            "delta_state": self.delta_state,
            "waiver_status": self.waiver_status,
            "applied_waiver": asdict(self.applied_waiver) if self.applied_waiver else None,
            "is_from_dependency_expansion": self.is_from_dependency_expansion,
        }
        return res


@dataclass
class GuardEvaluationResult:
    decision: str  # 'pass', 'warn', 'fail'
    exit_code: int
    mode: str
    base_revision: str
    candidate_revision: str
    files_changed_count: int
    files_scanned_count: int
    dependency_expanded_count: int
    counts_by_state: dict[str, int]
    counts_by_severity: dict[str, int]
    counts_by_rule: dict[str, int]
    waived_count: int
    unwaived_count: int
    policy_reasons: List[str]
    evaluated_findings: List[EvaluatedGuardFinding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        res = asdict(self)
        res["evaluated_findings"] = [f.to_dict() for f in self.evaluated_findings]
        return res


class ArchitectureChangeGuardEngine:
    """Core evaluation engine combining Git diff, policy, waivers, and baseline analysis."""

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path.resolve()

    def evaluate_change_guard(
        self,
        base_rev: str = "HEAD~1",
        candidate_rev: str = "HEAD",
        policy_path: Optional[Path] = None,
        waiver_path: Optional[Path] = None,
        registry: Optional[DriftRuleRegistry] = None,
    ) -> GuardEvaluationResult:
        reg = registry or get_default_rule_registry()

        # 1. Load Policy & Waivers
        pol_file = policy_path or (self.repo_path / ".brain" / "architecture-policy.yaml")
        try:
            policy = ArchitecturePolicy.load_from_yaml(pol_file)
        except Exception as exc:
            return GuardEvaluationResult(
                decision="fail",
                exit_code=EXIT_INVALID_CONFIG,
                mode="fail",
                base_revision=base_rev,
                candidate_revision=candidate_rev,
                files_changed_count=0,
                files_scanned_count=0,
                dependency_expanded_count=0,
                counts_by_state={},
                counts_by_severity={},
                counts_by_rule={},
                waived_count=0,
                unwaived_count=0,
                policy_reasons=[f"Invalid architecture policy: {exc}"],
            )

        w_file = waiver_path or (self.repo_path / policy.waiver_file)
        try:
            waiver_mgr = DriftWaiverManager(w_file)
            waivers = waiver_mgr.load_waivers()
        except Exception as exc:
            if policy.enforcement.mode == "fail":
                return GuardEvaluationResult(
                    decision="fail",
                    exit_code=EXIT_INVALID_CONFIG,
                    mode=policy.enforcement.mode,
                    base_revision=base_rev,
                    candidate_revision=candidate_rev,
                    files_changed_count=0,
                    files_scanned_count=0,
                    dependency_expanded_count=0,
                    counts_by_state={},
                    counts_by_severity={},
                    counts_by_rule={},
                    waived_count=0,
                    unwaived_count=0,
                    policy_reasons=[f"Invalid waiver configuration: {exc}"],
                )
            waivers = []

        # 2. Get Git Diff
        try:
            diff_engine = GitDiffEngine(self.repo_path)
            changes = diff_engine.get_changeset(base_rev, candidate_rev, include_untracked=True)
        except Exception as exc:
            return GuardEvaluationResult(
                decision="fail",
                exit_code=EXIT_REPO_REVISION_ERROR,
                mode=policy.enforcement.mode,
                base_revision=base_rev,
                candidate_revision=candidate_rev,
                files_changed_count=0,
                files_scanned_count=0,
                dependency_expanded_count=0,
                counts_by_state={},
                counts_by_severity={},
                counts_by_rule={},
                waived_count=0,
                unwaived_count=0,
                policy_reasons=[f"Git revision resolution error: {exc}"],
            )

        # 3. Incremental Drift Scanning
        all_findings = scan_repository_drift(self.repo_path, registry=reg)
        changed_file_paths = {c.new_path for c in changes if c.language == "python" and c.change_type != "deleted"}

        # Filter findings to changed files or baseline
        baseline_mgr = DriftBaselineManager(self.repo_path / ".brain" / "drift_baseline.json")
        baseline = baseline_mgr.load_baseline()
        deltas = baseline_mgr.compute_deltas(all_findings, baseline)

        evaluated_list: List[EvaluatedGuardFinding] = []
        counts_state = {"new": 0, "persistent": 0, "moved": 0, "resolved": 0}
        counts_sev = {"info": 0, "warning": 0, "critical": 0}
        counts_rule: dict[str, int] = {}
        waived_count = 0
        unwaived_count = 0
        policy_reasons: List[str] = []

        new_unwaived_warnings = 0

        for item in deltas:
            f = item.finding
            st = item.delta_state

            # Determine waiver status
            w_status, applied_w = waiver_mgr.evaluate_finding_waiver(f, waivers)

            if w_status == "waived":
                waived_count += 1
            else:
                unwaived_count += 1

            if st in counts_state:
                counts_state[st] += 1
            if f.severity in counts_sev:
                counts_sev[f.severity] += 1
            counts_rule[f.rule_id] = counts_rule.get(f.rule_id, 0) + 1

            eval_finding = EvaluatedGuardFinding(
                finding=f,
                delta_state=st,
                waiver_status=w_status,
                applied_waiver=applied_w,
                is_from_dependency_expansion=False,
            )
            evaluated_list.append(eval_finding)

            # Evaluate Policy triggers
            if w_status != "waived":
                if st in policy.enforcement.fail_on_states and f.severity in policy.enforcement.fail_on_severities:
                    policy_reasons.append(
                        f"Unwaived {f.severity.upper()} violation '{f.rule_name}' ({st}) in {f.file_path}:{f.line_number}"
                    )

                if st == "new" and f.severity == "warning":
                    new_unwaived_warnings += 1

        if new_unwaived_warnings > policy.enforcement.max_new_warnings:
            policy_reasons.append(
                f"New warning count ({new_unwaived_warnings}) exceeded maximum allowed threshold ({policy.enforcement.max_new_warnings})"
            )

        # Determine Final Decision & Exit Code
        if policy_reasons:
            if policy.enforcement.mode == "fail":
                decision = "fail"
                exit_code = EXIT_POLICY_VIOLATION
            elif policy.enforcement.mode == "warn":
                decision = "warn"
                exit_code = EXIT_SUCCESS
            else:
                decision = "report"
                exit_code = EXIT_SUCCESS
        else:
            decision = "pass"
            exit_code = EXIT_SUCCESS

        return GuardEvaluationResult(
            decision=decision,
            exit_code=exit_code,
            mode=policy.enforcement.mode,
            base_revision=base_rev,
            candidate_revision=candidate_rev,
            files_changed_count=len(changes),
            files_scanned_count=len(changed_file_paths),
            dependency_expanded_count=0,
            counts_by_state=counts_state,
            counts_by_severity=counts_sev,
            counts_by_rule=counts_rule,
            waived_count=waived_count,
            unwaived_count=unwaived_count,
            policy_reasons=policy_reasons,
            evaluated_findings=evaluated_list,
        )
