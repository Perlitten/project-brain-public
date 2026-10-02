"""Experiment orchestration, comparison and export — Phases E3, E4, E5, E9.

Options are executed one per disposable workspace, compared by an explicit
deterministic formula, and turned into a recommendation. No language model
participates in choosing a winner, and nothing here writes to an authoritative
repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from brain.experiments.models import (
    ACTIONS_ALLOWING_EXPORT,
    ACTIONS_REQUIRING_CONCLUSION,
    TERMINAL_STATES,
    ComparisonResult,
    Experiment,
    ExperimentConclusion,
    ExperimentOption,
    ExperimentState,
    ExportNotAuthorizedError,
    ExportStaleError,
    HumanAction,
    HumanActionRecord,
    PatchExport,
    new_experiment_id,
    new_export_id,
    utc_now,
)
from brain.lab.laboratory import ChangeLaboratory
from brain.lab.models import PatchRecord, PatchSource


# ──────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────


class ExperimentStore:
    """Atomic JSON persistence for experiments and exports."""

    def __init__(self, storage_dir: Path):
        self._dir = Path(storage_dir).resolve()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @property
    def directory(self) -> Path:
        return self._dir

    @staticmethod
    def _write_atomic(path: Path, payload: Dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, indent=2, default=str))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)

    def save(self, experiment: Experiment) -> Experiment:
        with self._lock:
            now = utc_now()
            experiment.created_at_utc = experiment.created_at_utc or now
            experiment.updated_at_utc = now
            experiment.semantic_fingerprint = experiment.compute_fingerprint()
            self._write_atomic(
                self._dir / f"{experiment.experiment_id}.json", experiment.to_dict()
            )
        return experiment

    def get(self, experiment_id: str) -> Optional[Experiment]:
        path = self._dir / f"{experiment_id}.json"
        if not path.is_file():
            return None
        try:
            return Experiment.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError):
            return None

    def list_all(self) -> List[Experiment]:
        experiments = []
        for path in sorted(self._dir.glob("exp-*.json")):
            try:
                experiments.append(
                    Experiment.from_dict(json.loads(path.read_text(encoding="utf-8")))
                )
            except (json.JSONDecodeError, OSError, TypeError):
                continue
        return experiments

    def save_comparison(self, result: ComparisonResult) -> ComparisonResult:
        with self._lock:
            self._write_atomic(
                self._dir / f"comparison-{result.experiment_id}.json", result.to_dict()
            )
        return result

    def get_comparison(self, experiment_id: str) -> Optional[ComparisonResult]:
        path = self._dir / f"comparison-{experiment_id}.json"
        if not path.is_file():
            return None
        try:
            return ComparisonResult.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError):
            return None

    def save_export(self, export: PatchExport) -> PatchExport:
        with self._lock:
            export.exported_at_utc = export.exported_at_utc or utc_now()
            self._write_atomic(self._dir / f"export-{export.export_id}.json", export.to_dict())
        return export

    def get_export(self, export_id: str) -> Optional[PatchExport]:
        path = self._dir / f"export-{export_id}.json"
        if not path.is_file():
            return None
        try:
            return PatchExport.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError):
            return None


# ──────────────────────────────────────────────
# Phase E4 — deterministic comparison
# ──────────────────────────────────────────────


class ExperimentComparator:
    """Ranks options by an explicit, reproducible formula.

    Every dimension is normalized within the surviving option set, so the
    contribution of each metric is visible and the outcome does not depend on
    absolute scales. Ties are resolved by declared, deterministic keys — never
    by iteration order and never by a language model.
    """

    #: Weights sum to 100 so a normalized contribution reads as a percentage.
    WEIGHTS: Dict[str, float] = {
        "optional_tests": 10.0,
        "findings_net": 25.0,
        "cycle_delta": 8.0,
        "forbidden_edge_delta": 7.0,
        "coupling_delta": 12.0,
        "hotspot_delta": 8.0,
        "blast_radius": 10.0,
        "changed_files": 5.0,
        "changed_lines": 5.0,
        "rollback_complexity": 5.0,
        "validation_duration": 3.0,
        "unresolved_uncertainty": 2.0,
    }

    #: True when a smaller raw value is better.
    LOWER_IS_BETTER = {
        "cycle_delta",
        "forbidden_edge_delta",
        "coupling_delta",
        "hotspot_delta",
        "blast_radius",
        "changed_files",
        "changed_lines",
        "rollback_complexity",
        "validation_duration",
        "unresolved_uncertainty",
    }

    TIE_EPSILON = 0.01

    @staticmethod
    def formula() -> str:
        terms = ", ".join(
            f"{k}={v:g}" for k, v in sorted(ExperimentComparator.WEIGHTS.items())
        )
        return f"weighted_sum_of_min_max_normalized({terms})"

    @staticmethod
    def _raw_metrics(opt: ExperimentOption) -> Dict[str, float]:
        return {
            "optional_tests": 1.0 if opt.passed_optional else 0.0,
            "findings_net": float(opt.resolved_findings - opt.new_findings),
            "cycle_delta": float(opt.cycle_delta),
            "forbidden_edge_delta": float(opt.forbidden_edge_delta),
            "coupling_delta": float(opt.coupling_delta),
            "hotspot_delta": float(opt.hotspot_delta),
            "blast_radius": float(opt.impact_blast_radius),
            "changed_files": float(opt.changed_files),
            "changed_lines": float(opt.changed_lines),
            "rollback_complexity": float(opt.rollback_complexity),
            "validation_duration": float(opt.validation_duration),
            "unresolved_uncertainty": float(len(opt.unresolved_uncertainty)),
        }

    @staticmethod
    def _disqualify(
        opt: ExperimentOption, policy_allows_new_critical: bool
    ) -> Tuple[Optional[str], List[str]]:
        """Return (disqualification reason or None, prominent warnings)."""
        warnings: List[str] = []

        if opt.new_critical_findings > 0:
            # Marked prominently whether or not policy tolerates it — a
            # suppressed violation that nobody sees is the failure mode here.
            warnings.append(
                f"Option '{opt.option_id}' introduces {opt.new_critical_findings} "
                "new critical architecture violation(s)"
            )
        if opt.forbidden_edge_delta > 0:
            warnings.append(
                f"Option '{opt.option_id}' adds {opt.forbidden_edge_delta} forbidden edge(s)"
            )

        if not opt.executed:
            return "Option was never executed", warnings
        if not opt.passed_required:
            return (opt.failure_reason or "Failed required tests"), warnings
        if opt.new_critical_findings > 0 and not policy_allows_new_critical:
            return (
                f"Introduced {opt.new_critical_findings} new critical violation(s); "
                "policy does not permit them"
            ), warnings
        if opt.forbidden_edge_delta > 0 and not policy_allows_new_critical:
            return (
                f"Introduced {opt.forbidden_edge_delta} forbidden edge(s); "
                "policy does not permit them"
            ), warnings
        if opt.new_findings > 0 and not opt.architecture_improved:
            return (
                "Introduced new findings without an architectural improvement"
            ), warnings
        return None, warnings

    @staticmethod
    def compare(experiment: Experiment) -> ComparisonResult:
        result = ComparisonResult(
            experiment_id=experiment.experiment_id,
            formula=ExperimentComparator.formula(),
        )

        if not experiment.options:
            result.conclusion = ExperimentConclusion.INSUFFICIENT_EVIDENCE.value
            result.warnings.append("Experiment has no options to compare")
            return result

        # ── Phase 1: disqualification ──
        active: List[ExperimentOption] = []
        any_executed = False
        for opt in experiment.options:
            any_executed = any_executed or opt.executed
            reason, warnings = ExperimentComparator._disqualify(
                opt, experiment.policy_allows_new_critical
            )
            opt.prominent_warnings = warnings
            result.warnings.extend(warnings)
            if opt.disqualified and opt.disqualification_reason:
                reason = reason or opt.disqualification_reason
            if reason:
                opt.disqualified = True
                opt.disqualification_reason = reason
                result.disqualified.append({"option_id": opt.option_id, "reason": reason})
                continue
            opt.disqualified = False
            opt.disqualification_reason = ""
            active.append(opt)

        raw_all = {o.option_id: ExperimentComparator._raw_metrics(o) for o in experiment.options}
        for opt in experiment.options:
            result.dimensions[opt.option_id] = {
                **raw_all[opt.option_id],
                "executed": opt.executed,
                "required_tests_passed": opt.passed_required,
                "architecture_improved": opt.architecture_improved,
                "new_findings": opt.new_findings,
                "new_critical_findings": opt.new_critical_findings,
                "resolved_findings": opt.resolved_findings,
                "disqualified": opt.disqualified,
                "disqualification_reason": opt.disqualification_reason,
            }

        if not any_executed:
            result.conclusion = ExperimentConclusion.INSUFFICIENT_EVIDENCE.value
            result.warnings.append("No option produced validation evidence")
            return result

        if not active:
            all_failed_tests = all(
                o.executed and not o.passed_required for o in experiment.options
            )
            result.conclusion = (
                ExperimentConclusion.ALL_FAILED.value
                if all_failed_tests
                else ExperimentConclusion.NO_SAFE_OPTION.value
            )
            return result

        # ── Phase 2: normalize within the surviving set, then score ──
        raw = {o.option_id: raw_all[o.option_id] for o in active}
        for opt in active:
            contributions: Dict[str, float] = {}
            total = 0.0
            for metric, weight in ExperimentComparator.WEIGHTS.items():
                values = [raw[o.option_id][metric] for o in active]
                low, high = min(values), max(values)
                value = raw[opt.option_id][metric]
                if high - low < 1e-12:
                    # No discriminating power: neither reward nor punish.
                    unit = 1.0
                elif metric in ExperimentComparator.LOWER_IS_BETTER:
                    unit = (high - value) / (high - low)
                else:
                    unit = (value - low) / (high - low)
                contributions[metric] = round(unit * weight, 4)
                total += unit * weight
            result.normalized[opt.option_id] = contributions
            result.scores[opt.option_id] = round(total, 4)

        # ── Phase 3: deterministic ranking ──
        ranked = sorted(
            active,
            key=lambda o: (
                -result.scores[o.option_id],
                o.changed_lines,
                o.impact_blast_radius,
                o.option_id,
            ),
        )
        result.rankings = [o.option_id for o in ranked]

        top = result.scores[ranked[0].option_id]
        tied = [
            o.option_id
            for o in ranked
            if abs(result.scores[o.option_id] - top) < ExperimentComparator.TIE_EPSILON
        ]
        result.recommended_option_id = ranked[0].option_id
        if len(tied) > 1:
            result.ties.append(tied)
            result.conclusion = ExperimentConclusion.MULTIPLE_EQUIVALENT.value
        else:
            result.conclusion = ExperimentConclusion.RECOMMEND_OPTION.value
        return result


# ──────────────────────────────────────────────
# Phase E3 — bounded orchestration
# ──────────────────────────────────────────────


class ExperimentRunner:
    """Executes each option in its own disposable workspace.

    Failures are isolated per option: a crash while validating one candidate
    records that option's failure and leaves the rest of the experiment
    running. Execution is idempotent — an option that already produced evidence
    is not re-run on resume.
    """

    def __init__(self, laboratory: ChangeLaboratory):
        self.lab = laboratory

    def run(
        self,
        experiment: Experiment,
        repo_path: Path,
        cancel_event: Optional[threading.Event] = None,
        allowed_profiles: Optional[List[str]] = None,
        on_option: Optional[Callable[[ExperimentOption], None]] = None,
    ) -> Experiment:
        cancel_event = cancel_event or threading.Event()
        pending = [o for o in experiment.options if not o.executed]
        workers = max(1, min(int(experiment.max_parallel_options or 1), 8))

        def _execute(option: ExperimentOption) -> ExperimentOption:
            if cancel_event.is_set():
                option.failure_reason = "Cancelled before execution"
                return option
            try:
                self._run_option(experiment, option, repo_path, allowed_profiles)
            except Exception as exc:  # isolate: one bad option must not stop the rest
                option.executed = True
                option.passed_required = False
                option.failure_reason = f"{type(exc).__name__}: {exc}"[:500]
            if on_option is not None:
                on_option(option)
            return option

        if workers == 1 or len(pending) <= 1:
            for option in pending:
                _execute(option)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(_execute, pending))

        experiment.workspace_ids = [o.workspace_id for o in experiment.options if o.workspace_id]
        if cancel_event.is_set():
            experiment.state = ExperimentState.CANCELLED.value
        return experiment

    def _run_option(
        self,
        experiment: Experiment,
        option: ExperimentOption,
        repo_path: Path,
        allowed_profiles: Optional[List[str]],
    ) -> None:
        patch = PatchRecord(
            patch_id=f"{experiment.experiment_id}:{option.option_id}",
            source_type=PatchSource.UNIFIED_DIFF.value,
            repository_id=experiment.repository_id,
            base_revision=experiment.base_revision,
            patch_content=option.candidate_patch,
            allowed_paths=list(option.expected_files),
        )
        session = self.lab.run_session(
            repository_id=experiment.repository_id,
            repo_path=repo_path,
            base_revision=experiment.base_revision,
            patch=patch,
            profile_name=option.validation_profile or experiment.validation_profile,
            declared_paths=option.expected_files or None,
            # Each option gets a fresh workspace and never inherits a dirty one.
            cleanup=not experiment.retain_workspaces,
            allowed_profiles=allowed_profiles,
        )

        option.executed = True
        option.workspace_id = session.get("workspace_id", "")
        option.results = session
        option.artifacts = {"session_id": session.get("session_id", "")}
        option.validation_duration = float(session.get("duration_seconds", 0.0))

        outcome = session.get("outcome")
        option.passed_required = outcome == "passed"
        validation = session.get("validation") or {}
        option.passed_optional = bool(validation.get("passed")) and outcome == "passed"

        apply_result = session.get("apply") or {}
        affected = apply_result.get("affected_files") or []
        option.changed_files = len(affected)
        option.changed_lines = _count_changed_lines(option.candidate_patch)
        option.rollback_complexity = option.changed_files

        post = session.get("post_patch") or {}
        unexpected = post.get("unexpected_files") or []
        if unexpected:
            option.unresolved_uncertainty.append(
                f"{len(unexpected)} file(s) changed outside the declared scope"
            )
        if not option.passed_required:
            reasons = session.get("rejected_reasons") or []
            option.failure_reason = (
                "; ".join(str(r) for r in reasons)[:500] or f"Validation outcome: {outcome}"
            )
        if session.get("network_isolation") != "verified":
            option.unresolved_uncertainty.append(
                "Network isolation during validation is unverified"
            )


def _count_changed_lines(patch_content: str) -> int:
    """Added plus removed lines, excluding diff headers."""
    count = 0
    for line in patch_content.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            count += 1
    return count


# ──────────────────────────────────────────────
# Lifecycle facade
# ──────────────────────────────────────────────


class ExperimentManager:
    """Experiment lifecycle: create, run, compare, human workflow, export."""

    def __init__(self, store: ExperimentStore, laboratory: Optional[ChangeLaboratory] = None):
        self._store = store
        self._lab = laboratory
        self._cancels: Dict[str, threading.Event] = {}

    @property
    def store(self) -> ExperimentStore:
        return self._store

    # ── create ──

    def create_experiment(
        self,
        repository_id: str,
        base_revision: str,
        options: List[ExperimentOption],
        source_plan_id: str = "",
        source_finding_id: str = "",
        graph_generation: str = "",
        policy_version: str = "",
        creation_actor: str = "unknown",
        validation_profile: str = "python-standard",
        max_parallel_options: int = 1,
        policy_allows_new_critical: bool = False,
        retain_workspaces: bool = False,
    ) -> Experiment:
        experiment = Experiment(
            experiment_id=new_experiment_id(),
            repository_id=repository_id,
            base_revision=base_revision,
            source_plan_id=source_plan_id,
            source_finding_id=source_finding_id,
            graph_generation=graph_generation,
            policy_version=policy_version,
            creation_actor=creation_actor,
            options=list(options),
            validation_profile=validation_profile,
            max_parallel_options=max_parallel_options,
            policy_allows_new_critical=policy_allows_new_critical,
            retain_workspaces=retain_workspaces,
            state=ExperimentState.PROPOSED.value,
        )
        return self._store.save(experiment)

    def get(self, experiment_id: str) -> Optional[Experiment]:
        return self._store.get(experiment_id)

    def list_all(self) -> List[Experiment]:
        return self._store.list_all()

    def update_state(
        self, experiment_id: str, new_state: ExperimentState
    ) -> Optional[Experiment]:
        exp = self._store.get(experiment_id)
        if exp is None:
            return None
        exp.state = new_state.value
        return self._store.save(exp)

    # ── run ──

    def request_cancel(self, experiment_id: str) -> Optional[Experiment]:
        exp = self._store.get(experiment_id)
        if exp is None:
            return None
        self._cancels.setdefault(experiment_id, threading.Event()).set()
        exp.cancel_requested = True
        if exp.state not in TERMINAL_STATES:
            exp.state = ExperimentState.CANCELLED.value
        return self._store.save(exp)

    def run_experiment(
        self,
        experiment_id: str,
        repo_path: Path,
        authoritative_revision: str = "",
        allowed_profiles: Optional[List[str]] = None,
    ) -> Optional[Experiment]:
        """Prepare, execute and compare one experiment.

        Resuming an experiment re-runs only the options that have no evidence
        yet, so an interrupted run costs at most the options it never finished.
        """
        exp = self._store.get(experiment_id)
        if exp is None or self._lab is None:
            return exp

        if exp.state in TERMINAL_STATES and exp.state != ExperimentState.COMPLETED.value:
            return exp
        if exp.cancel_requested:
            exp.state = ExperimentState.CANCELLED.value
            return self._store.save(exp)

        # Stale-plan invalidation: a moved authoritative revision makes every
        # measurement in this experiment describe a tree that no longer exists.
        if authoritative_revision and authoritative_revision != exp.base_revision:
            exp.state = ExperimentState.STALE.value
            exp.conclusion = ExperimentConclusion.EXPERIMENT_STALE.value
            exp.failure_reason = (
                f"Base revision {exp.base_revision} no longer matches the "
                f"authoritative revision {authoritative_revision}"
            )
            self._store.save(exp)
            comparison = ComparisonResult(
                experiment_id=exp.experiment_id,
                conclusion=ExperimentConclusion.EXPERIMENT_STALE.value,
                formula=ExperimentComparator.formula(),
                warnings=[exp.failure_reason],
            )
            self._store.save_comparison(comparison)
            return exp

        cancel = self._cancels.setdefault(experiment_id, threading.Event())
        exp.state = ExperimentState.PREPARING.value
        self._store.save(exp)

        exp.state = ExperimentState.RUNNING.value
        self._store.save(exp)

        def _save_exp(_option: ExperimentOption) -> None:
            self._store.save(exp)

        runner = ExperimentRunner(self._lab)
        runner.run(
            exp,
            Path(repo_path),
            cancel_event=cancel,
            allowed_profiles=allowed_profiles,
            on_option=_save_exp,
        )

        if cancel.is_set():
            exp.state = ExperimentState.CANCELLED.value
            exp.cancel_requested = True
            return self._store.save(exp)

        exp.state = ExperimentState.COMPARING.value
        self._store.save(exp)
        self._compare_and_finish(exp)
        return exp

    # ── compare ──

    def complete_comparison(self, experiment_id: str) -> Optional[ComparisonResult]:
        exp = self._store.get(experiment_id)
        if exp is None:
            return None
        return self._compare_and_finish(exp)

    def _compare_and_finish(self, exp: Experiment) -> ComparisonResult:
        result = ExperimentComparator.compare(exp)
        exp.conclusion = result.conclusion
        exp.recommended_option_id = result.recommended_option_id
        exp.state = ExperimentState.COMPLETED.value
        exp.completed_at_utc = utc_now()
        self._store.save(exp)
        return self._store.save_comparison(result)

    def get_comparison(self, experiment_id: str) -> Optional[ComparisonResult]:
        return self._store.get_comparison(experiment_id)

    def get_recommendation(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        exp = self._store.get(experiment_id)
        if exp is None:
            return None
        comparison = self._store.get_comparison(experiment_id)
        return {
            "experiment_id": exp.experiment_id,
            "state": exp.state,
            "conclusion": exp.conclusion or ExperimentConclusion.INSUFFICIENT_EVIDENCE.value,
            "recommended_option_id": exp.recommended_option_id,
            "rankings": comparison.rankings if comparison else [],
            "ties": comparison.ties if comparison else [],
            "disqualified": comparison.disqualified if comparison else [],
            "warnings": comparison.warnings if comparison else [],
            "formula": comparison.formula if comparison else ExperimentComparator.formula(),
            "approval_status": "not_approved",
            "note": exp.recommendation_note,
        }

    # ── Phase E8: human workflow ──

    def record_human_action(
        self,
        experiment_id: str,
        action: str,
        actor: str = "unknown",
        option_id: str = "",
        note: str = "",
    ) -> Optional[Experiment]:
        exp = self._store.get(experiment_id)
        if exp is None:
            return None
        if action not in {a.value for a in HumanAction}:
            raise ValueError(f"Unknown human action: '{action}'")
        if action in ACTIONS_REQUIRING_CONCLUSION and not exp.conclusion:
            raise ValueError(
                f"Action '{action}' requires a completed experiment with a conclusion"
            )
        if option_id and exp.get_option(option_id) is None:
            raise ValueError(f"Unknown option '{option_id}' for experiment {experiment_id}")

        record = HumanActionRecord(
            action=action,
            actor=actor,
            option_id=option_id or exp.recommended_option_id,
            note=note,
        )
        exp.human_actions.append(record)
        exp.human_action = action
        exp.human_action_timestamp = record.recorded_at_utc
        return self._store.save(exp)

    # ── Phase E9: export ──

    def export_patch(
        self,
        experiment_id: str,
        option_id: str,
        authoritative_revision: str,
        revalidated: bool = False,
        expected_source_hashes: Optional[Dict[str, str]] = None,
    ) -> PatchExport:
        """Package an accepted option for a human to apply themselves.

        Raises when nobody accepted the option, or when the authoritative
        revision has moved and no explicit revalidation was performed.
        """
        exp = self._store.get(experiment_id)
        if exp is None:
            raise LookupError(f"Unknown experiment '{experiment_id}'")
        option = exp.get_option(option_id)
        if option is None:
            raise LookupError(f"Unknown option '{option_id}'")

        accepted = any(
            a.action in ACTIONS_ALLOWING_EXPORT and (not a.option_id or a.option_id == option_id)
            for a in exp.human_actions
        )
        if not accepted:
            raise ExportNotAuthorizedError(
                f"Option '{option_id}' has not been accepted for export by a human"
            )

        if authoritative_revision != exp.base_revision and not revalidated:
            raise ExportStaleError(
                f"Authoritative revision changed: expected {exp.base_revision}, "
                f"got {authoritative_revision}. Re-validate before exporting."
            )

        diff = option.candidate_patch
        export = PatchExport(
            export_id=new_export_id(),
            experiment_id=experiment_id,
            option_id=option_id,
            repository_id=exp.repository_id,
            base_revision=exp.base_revision,
            unified_diff=diff,
            expected_source_hashes=dict(expected_source_hashes or {}),
            validation_summary={
                "passed_required": option.passed_required,
                "passed_optional": option.passed_optional,
                "validation_profile": option.validation_profile or exp.validation_profile,
                "duration_seconds": option.validation_duration,
                "outcome": (option.results or {}).get("outcome", ""),
            },
            architecture_comparison={
                "cycle_delta": option.cycle_delta,
                "forbidden_edge_delta": option.forbidden_edge_delta,
                "coupling_delta": option.coupling_delta,
                "hotspot_delta": option.hotspot_delta,
                "new_findings": option.new_findings,
                "new_critical_findings": option.new_critical_findings,
                "resolved_findings": option.resolved_findings,
                "architecture_improved": option.architecture_improved,
            },
            tests_run=[
                str(r.get("argv"))
                for r in ((option.results or {}).get("validation") or {}).get("results", [])
            ],
            known_limitations=[
                "The patch was validated only inside a disposable managed workspace.",
                "Network isolation during validation is unverified.",
                *option.unresolved_uncertainty,
                *option.prominent_warnings,
            ],
            application_instructions=[
                f"1. Confirm the repository is at revision {exp.base_revision}.",
                "2. Dry run: git apply --check patch.diff",
                "3. Apply: git apply patch.diff",
                "4. Run the project's own test suite.",
                "5. Review the diff before committing — Project Brain does not "
                "commit, push, merge, or deploy.",
            ],
            rollback_instructions=[
                "1. Revert with: git apply -R patch.diff",
                "2. Or discard: git checkout -- <affected files>",
                "3. Re-run the test suite to confirm the original state.",
            ],
            authorized_by=next(
                (
                    a.actor
                    for a in reversed(exp.human_actions)
                    if a.action in ACTIONS_ALLOWING_EXPORT
                ),
                "",
            ),
            revalidated=revalidated,
        )
        export.artifact_checksums = {
            "unified_diff.sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        }
        return self._store.save_export(export)

    def get_export(self, export_id: str) -> Optional[PatchExport]:
        return self._store.get_export(export_id)
