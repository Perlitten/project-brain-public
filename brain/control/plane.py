"""Operator control plane — Phases G1, G2, G4.

Aggregates the v0.5.0 subsystems into one operator-facing view: what is
registered, what is fresh, what is running, what failed, what is waiting for a
person, and how much of each budget is in use.

The deliverable is a stable API and CLI. Every queued item asks a human to
decide something; none of them is an autonomous execute action. Project Brain
may apply candidate patches only inside disposable managed workspaces for
validation. It does not apply patches to authoritative repositories, commit
changes, push branches, merge pull requests, or deploy software.
"""

from __future__ import annotations

import calendar
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from brain.control.budgets import (
    BUDGET_FILE_NAME,
    BudgetEnforcer,
    BudgetPolicy,
)
from brain.experiments.models import ExperimentState
from brain.experiments.orchestrator import ExperimentStore
from brain.freshness.tracker import FreshnessTracker
from brain.insights.drift_baseline import DriftBaselineManager
from brain.insights.drift_waivers import DriftWaiverManager
from brain.insights.remediation_manager import RemediationPlanManager
from brain.lab.laboratory import ChangeLaboratory
from brain.lab.models import WorkspaceState
from brain.ledger.ledger import EvidenceLedger
from brain.portfolio.store import PortfolioStore
from brain.workspace.registry_store import RegistryStore

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: Workspace states that still hold disk and are not accounted for.
UNCLEANED_STATES = frozenset(
    {
        WorkspaceState.REQUESTED.value,
        WorkspaceState.PREPARING.value,
        WorkspaceState.READY.value,
        WorkspaceState.PATCHING.value,
        WorkspaceState.VALIDATING.value,
        WorkspaceState.PASSED.value,
        WorkspaceState.FAILED.value,
        WorkspaceState.EXPIRED.value,
        WorkspaceState.CLEANING.value,
        WorkspaceState.QUARANTINED.value,
    }
)

#: Workspace states that are actively consuming a validation slot.
ACTIVE_STATES = frozenset(
    {
        WorkspaceState.PREPARING.value,
        WorkspaceState.READY.value,
        WorkspaceState.PATCHING.value,
        WorkspaceState.VALIDATING.value,
    }
)

RUNNING_EXPERIMENT_STATES = frozenset(
    {
        ExperimentState.QUEUED.value,
        ExperimentState.PREPARING.value,
        ExperimentState.RUNNING.value,
        ExperimentState.COMPARING.value,
    }
)

PLAN_AWAITING_STATES = frozenset({"proposed", "draft", "pending_approval", "awaiting_approval"})
PLAN_STALE_STATES = frozenset({"invalidated", "stale", "expired"})

# ── Phase G2: the queue vocabulary ──

PRIORITY_LABELS = {1: "critical", 2: "high", 3: "medium", 4: "low"}

#: Every allowed action is a human decision. Nothing here executes a change to
#: an authoritative repository, and nothing here is performed autonomously.
ALLOWED_ACTIONS: Dict[str, List[str]] = {
    "review_high_risk_change": ["review", "acknowledge", "escalate"],
    "approve_remediation_plan": ["approve", "reject", "request_changes"],
    "inspect_failed_experiment": ["inspect", "acknowledge", "dismiss"],
    "clean_quarantined_workspace": ["inspect", "authorize_cleanup", "retain_for_analysis"],
    "revalidate_stale_patch": ["revalidate", "reject", "acknowledge"],
    "investigate_graph_build_failure": ["investigate", "acknowledge", "dismiss"],
    "resolve_expired_waiver": ["renew", "revoke", "acknowledge"],
    "verify_manually_applied_patch": ["confirm_verified", "report_failed", "acknowledge"],
}

#: Verbs an action name must never contain. An operator queue that can execute
#: is no longer a queue of decisions.
FORBIDDEN_ACTION_VERBS = frozenset(
    {"apply", "commit", "push", "merge", "deploy", "promote", "execute", "run"}
)


def utc_now() -> str:
    return time.strftime(TIMESTAMP_FORMAT, time.gmtime())


def parse_timestamp(value: str) -> Optional[float]:
    """Parse a Brain UTC timestamp into epoch seconds, or None."""
    if not value:
        return None
    try:
        return float(calendar.timegm(time.strptime(str(value), TIMESTAMP_FORMAT)))
    except (ValueError, TypeError):
        return None


@dataclass
class ActionItem:
    """One thing a human is being asked to decide — Phase G2."""

    item_id: str
    action_type: str
    priority: int
    reason: str
    repository_id: str = ""
    source_entity_type: str = ""
    source_entity_id: str = ""
    owner: str = "unassigned"
    created_at_utc: str = ""
    age_seconds: int = 0
    status: str = "open"
    allowed_actions: List[str] = field(default_factory=list)

    @property
    def priority_label(self) -> str:
        return PRIORITY_LABELS.get(self.priority, "low")

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["priority_label"] = self.priority_label
        return payload


class ControlPlane:
    """Read-only aggregation over the v0.5.0 subsystems."""

    #: The summary never walks an unbounded list into a response.
    MAX_LISTED = 50

    def __init__(self, brain_dir: Path, policy: Optional[BudgetPolicy] = None):
        self.brain_dir = Path(brain_dir).resolve()
        self.registry = RegistryStore(self.brain_dir / "workspace")
        self.portfolios = PortfolioStore(self.brain_dir / "portfolio")
        self.freshness = FreshnessTracker(self.brain_dir / "freshness")
        self.laboratory = ChangeLaboratory(self.brain_dir)
        self.experiments = ExperimentStore(self.brain_dir / "experiments")
        self.ledger = EvidenceLedger(self.brain_dir)
        self.plans = RemediationPlanManager(self.brain_dir)
        self.policy = policy or BudgetPolicy.load(self.brain_dir / "control" / BUDGET_FILE_NAME)
        self.enforcer = BudgetEnforcer(self.policy)

    # ── tolerant readers ──

    @staticmethod
    def _safe(reader: Callable[[], Any], fallback: Any) -> Any:
        """Read one subsystem without letting it take the whole view down.

        A degraded subsystem must still leave the operator able to see the
        rest — but see §budgets: an unreadable subsystem makes its budget
        unobservable, which fails closed rather than granting capacity.
        """
        try:
            return reader()
        except Exception:
            return fallback

    def _repositories(self) -> List[Any]:
        return self._safe(self.registry.list_all, [])

    def _workspaces(self) -> List[Any]:
        return self._safe(self.laboratory.manager.list_workspaces, [])

    def _experiment_records(self) -> List[Any]:
        return self._safe(self.experiments.list_all, [])

    def _exports(self) -> List[Dict[str, Any]]:
        def _read() -> List[Dict[str, Any]]:
            found = []
            for path in sorted(self.experiments.directory.glob("export-*.json")):
                try:
                    found.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
            return found

        return self._safe(_read, [])

    def _plan_records(self) -> List[Any]:
        return self._safe(lambda: self.plans.list_plans(), [])

    def _freshness_records(self) -> List[Any]:
        return self._safe(self.freshness.list_all, [])

    def _critical_findings(self) -> List[Dict[str, Any]]:
        """Read persisted drift baselines. Never rescans a repository."""
        findings: List[Dict[str, Any]] = []
        for repo in self._repositories():
            baseline_path = Path(repo.canonical_root) / ".brain" / "drift_baseline.json"
            if not self._safe(baseline_path.is_file, False):
                continue
            baseline = self._safe(
                lambda: DriftBaselineManager(baseline_path).load_baseline(), None
            )
            if baseline is None:
                continue
            for raw in getattr(baseline, "findings", []) or []:
                if str(raw.get("severity", "")).lower() != "critical":
                    continue
                findings.append(
                    {
                        "repository_id": repo.repository_id,
                        "fingerprint": raw.get("fingerprint", ""),
                        "rule_id": raw.get("rule_id", ""),
                        "file_path": raw.get("file_path", ""),
                        "severity": "critical",
                        "description": raw.get("description", ""),
                        "source_revision": getattr(baseline, "source_revision", "") or "",
                        "created_at_utc": getattr(baseline, "created_at_utc", "") or "",
                    }
                )
        findings.sort(key=lambda f: (f["repository_id"], f["fingerprint"]))
        return findings

    def _expired_waivers(self) -> List[Dict[str, Any]]:
        expired: List[Dict[str, Any]] = []
        for repo in self._repositories():
            waiver_file = Path(repo.canonical_root) / ".brain" / "drift-waivers.yaml"
            if not self._safe(waiver_file.is_file, False):
                continue
            waivers = self._safe(
                lambda: DriftWaiverManager(waiver_file).load_waivers(), []
            )
            for waiver in waivers:
                if not self._safe(waiver.is_expired, False):
                    continue
                expired.append(
                    {
                        "repository_id": repo.repository_id,
                        "waiver_id": waiver.id,
                        "rule_id": waiver.rule_id,
                        "owner": waiver.owner or "unassigned",
                        "expires_at": waiver.expires_at,
                        "reason": waiver.reason,
                    }
                )
        return expired

    def _artifact_bytes(self) -> Optional[int]:
        """Total retained bytes under the Brain state directory."""

        def _walk() -> int:
            total = 0
            for path in self.brain_dir.rglob("*"):
                if path.is_file():
                    total += path.stat().st_size
            return total

        return self._safe(_walk, None)

    # ── Phase G1: operator summary ──

    def summary(self) -> Dict[str, Any]:
        repositories = self._repositories()
        workspaces = self._workspaces()
        experiments = self._experiment_records()
        exports = self._exports()
        plans = self._plan_records()
        freshness = self._freshness_records()
        findings = self._critical_findings()

        revisions = {r.repository_id: r.current_revision for r in repositories}
        stale_plans = [p for p in plans if self._plan_is_stale(p, revisions)]
        awaiting_plans = [p for p in plans if str(p.state).lower() in PLAN_AWAITING_STATES]

        active_workspaces = [w for w in workspaces if w.state in ACTIVE_STATES]
        uncleaned = [w for w in workspaces if w.state in UNCLEANED_STATES]
        quarantined = [w for w in workspaces if w.state == WorkspaceState.QUARANTINED.value]
        failed_workspaces = [w for w in workspaces if w.state == WorkspaceState.FAILED.value]

        running = [e for e in experiments if e.state in RUNNING_EXPERIMENT_STATES]
        failed_experiments = [
            e for e in experiments if e.state == ExperimentState.FAILED.value
        ]

        portfolios = self._safe(self.portfolios.list_portfolios, [])

        return {
            "generated_at_utc": utc_now(),
            "brain_dir": str(self.brain_dir),
            "repositories": {
                "total": len(repositories),
                "enabled": sum(1 for r in repositories if not r.disabled),
                "disabled": sum(1 for r in repositories if r.disabled),
                "by_trust_level": _tally(r.trust_level for r in repositories),
                "records": [
                    {
                        "repository_id": r.repository_id,
                        "display_name": r.display_name,
                        "trust_level": r.trust_level,
                        "graph_generation_status": r.graph_generation_status,
                        "indexing_status": r.indexing_status,
                        "current_revision": r.current_revision,
                        "disabled": r.disabled,
                        "owner": r.owner,
                    }
                    for r in repositories[: self.MAX_LISTED]
                ],
            },
            "freshness": {
                "total_artifacts": len(freshness),
                "by_state": _tally(f.state for f in freshness),
                "stale": [f.artifact_id for f in freshness if f.state == "stale"][
                    : self.MAX_LISTED
                ],
                "failed": [f.artifact_id for f in freshness if f.state == "failed"][
                    : self.MAX_LISTED
                ],
            },
            "graph_generations": {
                "active": sum(
                    1 for r in repositories if r.graph_generation_status == "active"
                ),
                "building": sum(
                    1 for r in repositories if r.graph_generation_status == "building"
                ),
                "failed": [
                    r.repository_id
                    for r in repositories
                    if r.graph_generation_status == "failed"
                ],
                "stale": [
                    r.repository_id
                    for r in repositories
                    if r.graph_generation_status == "stale"
                ],
            },
            "portfolios": {
                "total": len(portfolios),
                "records": [
                    {
                        "portfolio_id": p.portfolio_id,
                        "display_name": p.display_name,
                        "repository_count": len(p.repositories),
                        "active_generation_id": p.active_generation_id or "",
                        "has_active_generation": bool(p.active_generation_id),
                    }
                    for p in portfolios[: self.MAX_LISTED]
                ],
            },
            "remediation_plans": {
                "total": len(plans),
                "by_state": _tally(str(p.state) for p in plans),
                "awaiting_approval": len(awaiting_plans),
                "stale": [p.plan_id for p in stale_plans][: self.MAX_LISTED],
            },
            "workspaces": {
                "total": len(workspaces),
                "active": len(active_workspaces),
                "uncleaned": len(uncleaned),
                "quarantined": len(quarantined),
                "failed_retained": len(failed_workspaces),
                "by_state": _tally(w.state for w in workspaces),
            },
            "experiments": {
                "total": len(experiments),
                "running": len(running),
                "failed": len(failed_experiments),
                "by_state": _tally(e.state for e in experiments),
                "running_ids": [e.experiment_id for e in running][: self.MAX_LISTED],
                "failed_ids": [e.experiment_id for e in failed_experiments][
                    : self.MAX_LISTED
                ],
            },
            "exported_patches": {
                "total": len(exports),
                "stale": sum(1 for e in exports if e.get("stale")),
                "records": [
                    {
                        "export_id": e.get("export_id", ""),
                        "repository_id": e.get("repository_id", ""),
                        "experiment_id": e.get("experiment_id", ""),
                        "option_id": e.get("option_id", ""),
                        "exported_at_utc": e.get("exported_at_utc", ""),
                        "stale": bool(e.get("stale")),
                        "applied_to_repository": False,
                    }
                    for e in exports[: self.MAX_LISTED]
                ],
            },
            "ledger": self._safe(self.ledger.health, {"valid": False, "events": 0}),
            "critical_findings": {
                "total": len(findings),
                "recent": findings[: self.MAX_LISTED],
            },
            "pending_review": {"total": len(self.action_queue())},
            "non_autonomy": (
                "Project Brain may apply candidate patches only inside disposable "
                "managed workspaces for validation. It does not apply patches to "
                "authoritative repositories, commit changes, push branches, merge "
                "pull requests, or deploy software."
            ),
        }

    @staticmethod
    def _plan_is_stale(plan: Any, revisions: Dict[str, str]) -> bool:
        """A plan is stale when its state says so, or its base revision moved."""
        if str(getattr(plan, "state", "")).lower() in PLAN_STALE_STATES:
            return True
        current = revisions.get(getattr(plan, "repository", ""), "")
        source = getattr(plan, "source_revision", "")
        return bool(current and source and current != source)

    # ── Phase G2: human action queue ──

    def action_queue(self, repository_id: str = "") -> List[Dict[str, Any]]:
        now = time.time()
        items: List[ActionItem] = []

        for finding in self._critical_findings():
            items.append(
                self._item(
                    "review_high_risk_change",
                    priority=1,
                    reason=(
                        f"Critical architectural finding {finding['rule_id']} in "
                        f"{finding['file_path']}"
                    ),
                    repository_id=finding["repository_id"],
                    entity_type="finding",
                    entity_id=finding["fingerprint"] or finding["rule_id"],
                    created_at=finding["created_at_utc"],
                    now=now,
                )
            )

        revisions = {r.repository_id: r.current_revision for r in self._repositories()}
        for plan in self._plan_records():
            state = str(plan.state).lower()
            if state in PLAN_AWAITING_STATES:
                items.append(
                    self._item(
                        "approve_remediation_plan",
                        priority=2,
                        reason="Remediation plan awaits a human approval decision",
                        repository_id=getattr(plan, "repository", ""),
                        entity_type="remediation_plan",
                        entity_id=plan.plan_id,
                        created_at=getattr(plan, "created_at_utc", ""),
                        now=now,
                        owner=_first_owner(getattr(plan, "human_approvals_required", None)),
                    )
                )
            elif self._plan_is_stale(plan, revisions):
                items.append(
                    self._item(
                        "revalidate_stale_patch",
                        priority=3,
                        reason=(
                            "Plan was produced against a revision that is no longer "
                            "the repository head"
                        ),
                        repository_id=getattr(plan, "repository", ""),
                        entity_type="remediation_plan",
                        entity_id=plan.plan_id,
                        created_at=getattr(plan, "updated_at_utc", "")
                        or getattr(plan, "created_at_utc", ""),
                        now=now,
                    )
                )

        for experiment in self._experiment_records():
            if experiment.state != ExperimentState.FAILED.value:
                continue
            items.append(
                self._item(
                    "inspect_failed_experiment",
                    priority=2,
                    reason=experiment.failure_reason or "Experiment failed without a verdict",
                    repository_id=experiment.repository_id,
                    entity_type="experiment",
                    entity_id=experiment.experiment_id,
                    created_at=experiment.updated_at_utc or experiment.created_at_utc,
                    now=now,
                    owner=experiment.creation_actor or "unassigned",
                )
            )

        for workspace in self._workspaces():
            if workspace.state != WorkspaceState.QUARANTINED.value:
                continue
            items.append(
                self._item(
                    "clean_quarantined_workspace",
                    priority=2,
                    reason=workspace.quarantine_reason
                    or "Workspace was quarantined and still occupies disk",
                    repository_id=workspace.repository_id,
                    entity_type="workspace",
                    entity_id=workspace.workspace_id,
                    created_at=workspace.updated_at_utc or workspace.created_at_utc,
                    now=now,
                )
            )

        for repo in self._repositories():
            if repo.graph_generation_status != "failed":
                continue
            items.append(
                self._item(
                    "investigate_graph_build_failure",
                    priority=2,
                    reason="Graph generation failed; intelligence for this repository is stale",
                    repository_id=repo.repository_id,
                    entity_type="graph_generation",
                    entity_id=repo.repository_id,
                    created_at=repo.updated_at_utc,
                    now=now,
                    owner=repo.owner or "unassigned",
                )
            )

        for waiver in self._expired_waivers():
            items.append(
                self._item(
                    "resolve_expired_waiver",
                    priority=3,
                    reason=(
                        f"Waiver {waiver['waiver_id']} for rule {waiver['rule_id']} "
                        f"expired on {waiver['expires_at']}"
                    ),
                    repository_id=waiver["repository_id"],
                    entity_type="waiver",
                    entity_id=waiver["waiver_id"],
                    created_at="",
                    now=now,
                    owner=waiver["owner"],
                )
            )

        verified = self._verified_export_ids()
        for export in self._exports():
            export_id = export.get("export_id", "")
            if export.get("stale"):
                items.append(
                    self._item(
                        "revalidate_stale_patch",
                        priority=2,
                        reason=export.get("stale_reason")
                        or "Exported patch no longer matches the authoritative revision",
                        repository_id=export.get("repository_id", ""),
                        entity_type="patch_export",
                        entity_id=export_id,
                        created_at=export.get("exported_at_utc", ""),
                        now=now,
                        owner=export.get("authorized_by", "") or "unassigned",
                    )
                )
            elif export_id and export_id not in verified:
                items.append(
                    self._item(
                        "verify_manually_applied_patch",
                        priority=3,
                        reason="Patch was exported to a human and has no recorded verification",
                        repository_id=export.get("repository_id", ""),
                        entity_type="patch_export",
                        entity_id=export_id,
                        created_at=export.get("exported_at_utc", ""),
                        now=now,
                        owner=export.get("authorized_by", "") or "unassigned",
                    )
                )

        if repository_id:
            items = [i for i in items if i.repository_id == repository_id]

        # Deterministic order: severity first, then the oldest wait, then id.
        items.sort(key=lambda i: (i.priority, -i.age_seconds, i.item_id))
        return [i.to_dict() for i in items]

    def _verified_export_ids(self) -> set:
        """Export ids the ledger already records a human verification for."""

        def _read() -> set:
            found: set[str] = set()
            for event_type in ("verification_passed", "verification_failed"):
                page = self.ledger.query(
                    event_type=event_type, entity_type="patch_export", limit=500
                )
                found.update(e["entity_id"] for e in page["events"])
            return found

        return self._safe(_read, set())

    @staticmethod
    def _item(
        action_type: str,
        *,
        priority: int,
        reason: str,
        repository_id: str,
        entity_type: str,
        entity_id: str,
        created_at: str,
        now: float,
        owner: str = "unassigned",
    ) -> ActionItem:
        created_epoch = parse_timestamp(created_at)
        age = int(max(0.0, now - created_epoch)) if created_epoch is not None else 0
        return ActionItem(
            item_id=f"{action_type}:{entity_type}:{entity_id}",
            action_type=action_type,
            priority=priority,
            reason=reason,
            repository_id=repository_id,
            source_entity_type=entity_type,
            source_entity_id=entity_id,
            owner=owner or "unassigned",
            created_at_utc=created_at,
            age_seconds=age,
            status="open",
            allowed_actions=list(ALLOWED_ACTIONS[action_type]),
        )

    # ── Phase G3: budget usage ──

    def observed_usage(self) -> Dict[str, Optional[int]]:
        workspaces = self._workspaces()
        experiments = self._experiment_records()
        repositories = self._repositories()
        portfolios = self._safe(self.portfolios.list_portfolios, [])

        per_repo = _tally(w.repository_id for w in workspaces if w.state in ACTIVE_STATES)
        return {
            "max_active_workspaces": sum(1 for w in workspaces if w.state in ACTIVE_STATES),
            "max_workspaces_per_repository": max(per_repo.values()) if per_repo else 0,
            "max_experiment_options": max(
                (len(e.options) for e in experiments), default=0
            ),
            "max_simultaneous_validations": sum(
                1 for w in workspaces if w.state == WorkspaceState.VALIDATING.value
            ),
            "max_total_retained_artifact_bytes": self._artifact_bytes(),
            "max_failed_workspace_retention": sum(
                1 for w in workspaces if w.state == WorkspaceState.FAILED.value
            ),
            "max_graph_builds": sum(
                1 for r in repositories if r.graph_generation_status in ("active", "building")
            ),
            "max_portfolio_size": max(
                (len(p.repositories) for p in portfolios), default=0
            ),
            # Per-process limits are enforced at execution time by the
            # laboratory; the control plane reports the configured ceiling
            # rather than inventing a fleet-wide "current" for them.
            "max_process_output_bytes": 0,
            "max_validation_seconds": 0,
        }

    def budgets(self) -> Dict[str, Any]:
        return self.enforcer.report(self.observed_usage())

    def check_budget(self, budget: str, requested: int = 1):
        """Fail-closed admission check against live usage."""
        return self.enforcer.check(budget, self.observed_usage().get(budget), requested)

    # ── Phase G4: health and observability ──

    def metrics(self) -> Dict[str, Any]:
        summary = self.summary()
        budgets = self.budgets()
        queue = self.action_queue()
        ledger = summary["ledger"]

        oldest = max((i["age_seconds"] for i in queue), default=0)
        return {
            "generated_at_utc": summary["generated_at_utc"],
            "repositories_total": summary["repositories"]["total"],
            "repositories_enabled": summary["repositories"]["enabled"],
            "graph_generations_active": summary["graph_generations"]["active"],
            "graph_generations_failed": len(summary["graph_generations"]["failed"]),
            "artifacts_stale": len(summary["freshness"]["stale"]),
            "workspaces_active": summary["workspaces"]["active"],
            "workspaces_uncleaned": summary["workspaces"]["uncleaned"],
            "workspaces_quarantined": summary["workspaces"]["quarantined"],
            "experiments_running": summary["experiments"]["running"],
            "experiments_failed": summary["experiments"]["failed"],
            "exports_total": summary["exported_patches"]["total"],
            "exports_stale": summary["exported_patches"]["stale"],
            "critical_findings": summary["critical_findings"]["total"],
            "queue_depth": len(queue),
            "queue_by_priority": _tally(str(i["priority_label"]) for i in queue),
            "queue_by_action": _tally(str(i["action_type"]) for i in queue),
            "queue_oldest_age_seconds": oldest,
            "ledger_events": ledger.get("events", 0),
            "ledger_valid": bool(ledger.get("valid")),
            "budgets_exceeded": budgets["exceeded"],
            "budgets_unobservable": budgets["unobservable"],
        }

    def health(self) -> Dict[str, Any]:
        metrics = self.metrics()
        problems: List[str] = []
        if not metrics["ledger_valid"]:
            problems.append("evidence ledger failed verification")
        if metrics["budgets_exceeded"]:
            problems.append(f"budgets exceeded: {', '.join(metrics['budgets_exceeded'])}")
        if metrics["budgets_unobservable"]:
            problems.append(
                "budget usage unobservable: " + ", ".join(metrics["budgets_unobservable"])
            )
        if metrics["graph_generations_failed"]:
            problems.append(
                f"{metrics['graph_generations_failed']} graph generation(s) failed"
            )
        if metrics["workspaces_quarantined"]:
            problems.append(
                f"{metrics['workspaces_quarantined']} quarantined workspace(s) retained"
            )

        return {
            "status": "degraded" if problems else "ok",
            "problems": problems,
            "generated_at_utc": metrics["generated_at_utc"],
            "metrics": metrics,
        }


def _tally(values) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _first_owner(approvals: Any) -> str:
    if isinstance(approvals, (list, tuple)) and approvals:
        return str(approvals[0])
    return "unassigned"


__all__ = [
    "ALLOWED_ACTIONS",
    "FORBIDDEN_ACTION_VERBS",
    "PRIORITY_LABELS",
    "ActionItem",
    "ControlPlane",
    "parse_timestamp",
    "utc_now",
]
