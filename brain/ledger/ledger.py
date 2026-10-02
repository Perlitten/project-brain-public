"""Evidence ledger facade — Phases F1, F3, F5.

Writes reach the ledger through named product actions. There is no generic
"write anything you like" entry point, because an unrestricted writer turns an
evidence record into a scratch pad.

Project Brain may apply candidate patches only inside disposable managed
workspaces for validation. It does not apply patches to authoritative
repositories, commit changes, push branches, merge pull requests, or deploy
software.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from brain.ledger.models import (
    ActorType,
    EventType,
    LedgerEvent,
    LedgerIntegrityError,
)
from brain.ledger.store import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    LedgerStore,
    build_event,
)
from brain.ledger.verifier import LedgerVerifier

DEFAULT_DB_NAME = "ledger.sqlite3"


class EvidenceLedger:
    """Operator-facing evidence ledger."""

    def __init__(self, brain_dir: Path):
        self.brain_dir = Path(brain_dir).resolve()
        self.ledger_dir = self.brain_dir / "ledger"
        self.store = LedgerStore(self.ledger_dir / DEFAULT_DB_NAME)

    # ── generic internals (module-private by convention, not an API) ──

    def _record(self, event_type: str, **kwargs: Any) -> LedgerEvent:
        return self.store.append(build_event(event_type, **kwargs))

    # ── Phase F1: registry and capability events ──

    def record_repository_registered(
        self,
        repository_id: str,
        *,
        actor_identity: str,
        actor_type: str = ActorType.HUMAN.value,
        trust_level: str = "",
        source_revision: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> LedgerEvent:
        return self._record(
            EventType.REPOSITORY_REGISTERED.value,
            actor_identity=actor_identity,
            actor_type=actor_type,
            repository_id=repository_id,
            entity_type="repository",
            entity_id=repository_id,
            action="register",
            previous_state="unregistered",
            new_state=trust_level or "registered",
            source_revision=source_revision,
            metadata=metadata,
        )

    def record_capability_changed(
        self,
        repository_id: str,
        *,
        actor_identity: str,
        capability: str,
        previous_state: str,
        new_state: str,
        reason: str = "",
        actor_type: str = ActorType.HUMAN.value,
    ) -> LedgerEvent:
        return self._record(
            EventType.CAPABILITY_CHANGED.value,
            actor_identity=actor_identity,
            actor_type=actor_type,
            repository_id=repository_id,
            entity_type="capability",
            entity_id=f"{repository_id}:{capability}",
            action="change_capability",
            previous_state=previous_state,
            new_state=new_state,
            reason=reason,
        )

    # ── Phase F1: graph and portfolio events ──

    def record_graph_generation_built(
        self,
        repository_id: str,
        generation_id: str,
        *,
        actor_identity: str = "project-brain",
        source_revision: str = "",
        artifact_references: Optional[Sequence[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> LedgerEvent:
        return self._record(
            EventType.GRAPH_GENERATION_BUILT.value,
            actor_identity=actor_identity,
            actor_type=ActorType.SYSTEM.value,
            repository_id=repository_id,
            entity_type="graph_generation",
            entity_id=generation_id,
            action="build",
            new_state="built",
            source_revision=source_revision,
            artifact_references=artifact_references,
            metadata=metadata,
        )

    def record_graph_generation_activated(
        self,
        repository_id: str,
        generation_id: str,
        *,
        actor_identity: str = "project-brain",
        previous_state: str = "",
        source_revision: str = "",
    ) -> LedgerEvent:
        return self._record(
            EventType.GRAPH_GENERATION_ACTIVATED.value,
            actor_identity=actor_identity,
            actor_type=ActorType.SYSTEM.value,
            repository_id=repository_id,
            entity_type="graph_generation",
            entity_id=generation_id,
            action="activate",
            previous_state=previous_state,
            new_state="active",
            source_revision=source_revision,
        )

    def record_graph_build_failed(
        self,
        repository_id: str,
        generation_id: str,
        *,
        reason: str,
        actor_identity: str = "project-brain",
        source_revision: str = "",
    ) -> LedgerEvent:
        return self._record(
            EventType.GRAPH_BUILD_FAILED.value,
            actor_identity=actor_identity,
            actor_type=ActorType.SYSTEM.value,
            repository_id=repository_id,
            entity_type="graph_generation",
            entity_id=generation_id,
            action="build",
            new_state="failed",
            reason=reason,
            source_revision=source_revision,
        )

    def record_portfolio_generation_activated(
        self,
        portfolio_id: str,
        generation_id: str,
        *,
        actor_identity: str = "project-brain",
        previous_state: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> LedgerEvent:
        return self._record(
            EventType.PORTFOLIO_GENERATION_ACTIVATED.value,
            actor_identity=actor_identity,
            actor_type=ActorType.SYSTEM.value,
            portfolio_id=portfolio_id,
            entity_type="portfolio_generation",
            entity_id=generation_id,
            action="activate",
            previous_state=previous_state,
            new_state="active",
            metadata=metadata,
        )

    # ── Phase F1: findings and remediation ──

    def record_finding_created(
        self,
        repository_id: str,
        finding_id: str,
        *,
        severity: str,
        actor_identity: str = "project-brain",
        source_revision: str = "",
        reason: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> LedgerEvent:
        return self._record(
            EventType.FINDING_CREATED.value,
            actor_identity=actor_identity,
            actor_type=ActorType.SYSTEM.value,
            repository_id=repository_id,
            entity_type="finding",
            entity_id=finding_id,
            action="create",
            new_state=severity,
            reason=reason,
            source_revision=source_revision,
            metadata=metadata,
        )

    def record_remediation_plan_proposed(
        self,
        repository_id: str,
        plan_id: str,
        *,
        actor_identity: str = "project-brain",
        finding_id: str = "",
        source_revision: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> LedgerEvent:
        return self._record(
            EventType.REMEDIATION_PLAN_PROPOSED.value,
            actor_identity=actor_identity,
            actor_type=ActorType.SYSTEM.value,
            repository_id=repository_id,
            entity_type="remediation_plan",
            entity_id=plan_id,
            action="propose",
            new_state="proposed",
            reason=f"finding:{finding_id}" if finding_id else "",
            source_revision=source_revision,
            metadata=metadata,
        )

    def record_remediation_plan_decision(
        self,
        repository_id: str,
        plan_id: str,
        *,
        approved: bool,
        actor_identity: str,
        reason: str = "",
    ) -> LedgerEvent:
        """Approval is a human act.

        Project Brain proposes plans; it does not approve its own. The actor
        type is fixed to `human` here so an automated caller cannot record a
        self-approval that later reads as human sign-off.
        """
        event_type = (
            EventType.REMEDIATION_PLAN_APPROVED
            if approved
            else EventType.REMEDIATION_PLAN_REJECTED
        )
        return self._record(
            event_type.value,
            actor_identity=actor_identity,
            actor_type=ActorType.HUMAN.value,
            repository_id=repository_id,
            entity_type="remediation_plan",
            entity_id=plan_id,
            action="approve" if approved else "reject",
            previous_state="proposed",
            new_state="approved" if approved else "rejected",
            reason=reason,
        )

    # ── Phase F1: laboratory events ──

    def record_workspace_created(
        self,
        repository_id: str,
        workspace_id: str,
        *,
        base_revision: str,
        creation_method: str = "",
        actor_identity: str = "project-brain",
    ) -> LedgerEvent:
        return self._record(
            EventType.WORKSPACE_CREATED.value,
            actor_identity=actor_identity,
            actor_type=ActorType.SYSTEM.value,
            repository_id=repository_id,
            entity_type="workspace",
            entity_id=workspace_id,
            action="create",
            new_state="created",
            source_revision=base_revision,
            metadata={"creation_method": creation_method} if creation_method else None,
        )

    def record_workspace_state(
        self,
        repository_id: str,
        workspace_id: str,
        *,
        previous_state: str,
        new_state: str,
        reason: str = "",
        actor_identity: str = "project-brain",
    ) -> LedgerEvent:
        event_type = (
            EventType.WORKSPACE_QUARANTINED
            if new_state == "quarantined"
            else EventType.WORKSPACE_CLEANED
        )
        return self._record(
            event_type.value,
            actor_identity=actor_identity,
            actor_type=ActorType.SYSTEM.value,
            repository_id=repository_id,
            entity_type="workspace",
            entity_id=workspace_id,
            action=new_state,
            previous_state=previous_state,
            new_state=new_state,
            reason=reason,
        )

    def record_patch_applied_in_workspace(
        self,
        repository_id: str,
        workspace_id: str,
        *,
        patch_id: str,
        patch_hash: str,
        changed_files: int = 0,
        actor_identity: str = "project-brain",
    ) -> LedgerEvent:
        """The patch content itself is never stored — only its digest."""
        return self._record(
            EventType.PATCH_APPLIED_IN_WORKSPACE.value,
            actor_identity=actor_identity,
            actor_type=ActorType.SYSTEM.value,
            repository_id=repository_id,
            entity_type="workspace",
            entity_id=workspace_id,
            action="apply_patch",
            new_state="patched",
            metadata={
                "patch_id": patch_id,
                "patch_sha256": patch_hash,
                "changed_files": changed_files,
                "scope": "disposable_managed_workspace",
            },
        )

    def record_validation_command_executed(
        self,
        repository_id: str,
        workspace_id: str,
        *,
        command: Sequence[str],
        exit_code: int,
        duration_seconds: float,
        profile_name: str = "",
        artifact_references: Optional[Sequence[str]] = None,
        actor_identity: str = "project-brain",
        network_isolation: str = "unverified",
    ) -> LedgerEvent:
        """Command argv is recorded; process output is referenced, not embedded.

        ``network_isolation`` records the mechanism actually used for the run
        (``enforced:<backend>`` when a sandbox wrapped it), never a stronger
        claim than the runner made.
        """
        return self._record(
            EventType.VALIDATION_COMMAND_EXECUTED.value,
            actor_identity=actor_identity,
            actor_type=ActorType.SYSTEM.value,
            repository_id=repository_id,
            entity_type="workspace",
            entity_id=workspace_id,
            action="execute_validation",
            new_state="passed" if exit_code == 0 else "failed",
            artifact_references=artifact_references,
            metadata={
                "argv": list(command),
                "exit_code": exit_code,
                "duration_seconds": round(float(duration_seconds), 3),
                "profile_name": profile_name,
                "network_isolation": network_isolation,
            },
        )

    # ── Phase F1: experiments, recommendations, export ──

    def record_experiment_completed(
        self,
        repository_id: str,
        experiment_id: str,
        *,
        conclusion: str,
        recommended_option_id: str = "",
        actor_identity: str = "project-brain",
        source_revision: str = "",
        artifact_references: Optional[Sequence[str]] = None,
    ) -> LedgerEvent:
        return self._record(
            EventType.EXPERIMENT_COMPLETED.value,
            actor_identity=actor_identity,
            actor_type=ActorType.SYSTEM.value,
            repository_id=repository_id,
            entity_type="experiment",
            entity_id=experiment_id,
            action="complete",
            new_state=conclusion,
            source_revision=source_revision,
            artifact_references=artifact_references,
            metadata={"recommended_option_id": recommended_option_id},
        )

    def record_recommendation_acknowledged(
        self,
        repository_id: str,
        experiment_id: str,
        *,
        actor_identity: str,
        action: str,
        option_id: str = "",
        reason: str = "",
    ) -> LedgerEvent:
        return self._record(
            EventType.RECOMMENDATION_ACKNOWLEDGED.value,
            actor_identity=actor_identity,
            actor_type=ActorType.HUMAN.value,
            repository_id=repository_id,
            entity_type="experiment",
            entity_id=experiment_id,
            action=action,
            new_state="acknowledged",
            reason=reason,
            metadata={"option_id": option_id},
        )

    def record_patch_exported(
        self,
        repository_id: str,
        export_id: str,
        *,
        experiment_id: str,
        option_id: str,
        patch_hash: str,
        actor_identity: str,
        source_revision: str = "",
        artifact_references: Optional[Sequence[str]] = None,
    ) -> LedgerEvent:
        """Export hands a patch to a human. It does not apply anything."""
        return self._record(
            EventType.PATCH_EXPORTED.value,
            actor_identity=actor_identity,
            actor_type=ActorType.HUMAN.value,
            repository_id=repository_id,
            entity_type="patch_export",
            entity_id=export_id,
            action="export",
            new_state="exported",
            source_revision=source_revision,
            artifact_references=artifact_references,
            metadata={
                "experiment_id": experiment_id,
                "option_id": option_id,
                "patch_sha256": patch_hash,
                "applied_to_repository": False,
            },
        )

    def record_human_marked_patch_applied(
        self,
        repository_id: str,
        export_id: str,
        *,
        actor_identity: str,
        applied_revision: str = "",
        reason: str = "",
    ) -> LedgerEvent:
        return self._record(
            EventType.HUMAN_MARKED_PATCH_APPLIED.value,
            actor_identity=actor_identity,
            actor_type=ActorType.HUMAN.value,
            repository_id=repository_id,
            entity_type="patch_export",
            entity_id=export_id,
            action="mark_applied",
            previous_state="exported",
            new_state="applied_by_human",
            reason=reason,
            source_revision=applied_revision,
        )

    def record_verification(
        self,
        repository_id: str,
        entity_id: str,
        *,
        passed: bool,
        actor_identity: str,
        entity_type: str = "patch_export",
        reason: str = "",
        source_revision: str = "",
        artifact_references: Optional[Sequence[str]] = None,
    ) -> LedgerEvent:
        event_type = (
            EventType.VERIFICATION_PASSED if passed else EventType.VERIFICATION_FAILED
        )
        return self._record(
            event_type.value,
            actor_identity=actor_identity,
            actor_type=ActorType.HUMAN.value,
            repository_id=repository_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action="verify",
            new_state="verified" if passed else "verification_failed",
            reason=reason,
            source_revision=source_revision,
            artifact_references=artifact_references,
        )

    def record_artifact_superseded(
        self,
        repository_id: str,
        entity_id: str,
        *,
        entity_type: str,
        superseded_by: str,
        actor_identity: str = "project-brain",
        reason: str = "",
    ) -> LedgerEvent:
        return self._record(
            EventType.ARTIFACT_SUPERSEDED.value,
            actor_identity=actor_identity,
            actor_type=ActorType.SYSTEM.value,
            repository_id=repository_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action="supersede",
            previous_state="active",
            new_state="superseded",
            reason=reason,
            metadata={"superseded_by": superseded_by},
        )

    # ── Phase F3/F4: retention by tombstone, never by deletion ──

    def record_redaction(
        self,
        *,
        target_event_id: str,
        actor_identity: str,
        reason: str,
        redacted_fields: Sequence[str],
    ) -> LedgerEvent:
        """Append a tombstone describing what was withheld and why.

        The referenced event stays exactly as written. Silently rewriting it
        would destroy the only property this ledger has.
        """
        target = self.store.get(target_event_id)
        if target is None:
            raise LedgerIntegrityError(f"Unknown ledger event: {target_event_id}")
        return self._record(
            EventType.REDACTION_RECORDED.value,
            actor_identity=actor_identity,
            actor_type=ActorType.HUMAN.value,
            repository_id=target.repository_id,
            entity_type="ledger_event",
            entity_id=target_event_id,
            action="redact",
            previous_state="recorded",
            new_state="redaction_noted",
            reason=reason,
            metadata={
                "target_event_hash": target.event_hash,
                "target_sequence": target.sequence,
                "withheld_fields": sorted({str(f) for f in redacted_fields}),
                "history_preserved": True,
            },
        )

    # ── reads ──

    def get(self, event_id: str) -> Optional[LedgerEvent]:
        return self.store.get(event_id)

    def query(self, **kwargs: Any) -> Dict[str, Any]:
        return self.store.query(**kwargs)

    def entity_history(
        self, entity_type: str, entity_id: str, limit: int = MAX_PAGE_SIZE, offset: int = 0
    ) -> Dict[str, Any]:
        return self.store.entity_history(entity_type, entity_id, limit=limit, offset=offset)

    def verify(self) -> Dict[str, Any]:
        return LedgerVerifier.verify(self.store.all_events())

    def export(self, output: Path) -> Dict[str, Any]:
        return self.store.export_jsonl(Path(output))

    @staticmethod
    def verify_export(path: Path) -> Dict[str, Any]:
        return LedgerVerifier.verify(LedgerStore.load_jsonl(Path(path)))

    def health(self) -> Dict[str, Any]:
        """Compact integrity summary for the control plane — Phase G1/G4."""
        report = self.verify()
        head = self.store.head()
        return {
            "events": self.store.count(),
            "valid": report["valid"],
            "issue_count": report["issue_count"],
            "codes": report["codes"],
            "head_sequence": head.sequence if head else 0,
            "head_hash": head.event_hash if head else "",
            "last_event_at": head.timestamp_utc if head else "",
            "database": str(self.store.db_path),
        }


__all__ = [
    "DEFAULT_DB_NAME",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "EvidenceLedger",
]
