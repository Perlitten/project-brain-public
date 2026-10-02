"""Autonomic Incident State Machine, Stable Fingerprints, and Derived Impact Aggregation (v0.7.2)."""

import hashlib
import time
from enum import Enum
from typing import Dict, List, Optional, Tuple
from pydantic import BaseModel, Field


class IncidentState(str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    REMEDIATING = "remediating"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    BLOCKED_HUMAN_ACTION_REQUIRED = "blocked_human_action_required"


class IncidentSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class DerivedImpactType(str, Enum):
    JOB_FAILURE = "job_failure"
    FRESHNESS_UNVERIFIED = "freshness_unverified"
    VECTOR_COVERAGE_UNVERIFIED = "vector_coverage_unverified"


class DerivedImpact(BaseModel):
    impact_type: DerivedImpactType
    description: str
    blocked_remediations: List[str] = Field(default_factory=list)


class AutonomicIncident(BaseModel):
    incident_id: str
    fingerprint: str
    incident_type: str
    repository_id: str
    state: IncidentState = IncidentState.OPEN
    severity: IncidentSeverity = IncidentSeverity.ERROR
    root_cause_summary: str
    derived_impacts: List[DerivedImpact] = Field(default_factory=list)
    expected_revision: Optional[str] = None
    actual_revision: Optional[str] = None
    suggested_action: str = "investigate"
    remediation_patch_id: Optional[str] = None
    notification_emitted_count: int = 0
    created_at_utc: str
    updated_at_utc: str


class IncidentStateMachine:
    """Manages root-cause incident lifecycle, stable fingerprints, and notification deduplication."""

    def __init__(self):
        self._active_incidents: Dict[str, AutonomicIncident] = {}

    def compute_fingerprint(self, incident_type: str, repository_id: str, lineage_family: str = "default") -> str:
        """Computes a stable fingerprint excluding transient revision strings or timestamps."""
        payload = f"{incident_type}:{repository_id}:{lineage_family}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def report_root_incident(
        self,
        incident_type: str,
        repository_id: str,
        root_cause_summary: str,
        expected_revision: Optional[str] = None,
        actual_revision: Optional[str] = None,
        derived_impacts: Optional[List[DerivedImpact]] = None,
        severity: IncidentSeverity = IncidentSeverity.ERROR,
    ) -> Tuple[AutonomicIncident, bool]:
        """Creates or updates a root incident. Returns (incident, should_emit_telegram_notification)."""
        fp = self.compute_fingerprint(incident_type, repository_id)
        now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        existing = self._active_incidents.get(fp)
        should_notify = False

        if not existing:
            # New incident -> OPEN
            inc = AutonomicIncident(
                incident_id=f"inc-{fp[:10]}",
                fingerprint=fp,
                incident_type=incident_type,
                repository_id=repository_id,
                state=IncidentState.OPEN,
                severity=severity,
                root_cause_summary=root_cause_summary,
                derived_impacts=derived_impacts or [],
                expected_revision=expected_revision,
                actual_revision=actual_revision,
                notification_emitted_count=1,
                created_at_utc=now_utc,
                updated_at_utc=now_utc,
            )
            self._active_incidents[fp] = inc
            return inc, True

        # Existing incident -> check state or severity changes
        existing.updated_at_utc = now_utc
        existing.expected_revision = expected_revision
        existing.actual_revision = actual_revision
        if derived_impacts:
            existing.derived_impacts = derived_impacts

        if existing.state in (IncidentState.OPEN, IncidentState.ACKNOWLEDGED, IncidentState.REMEDIATING, IncidentState.VERIFYING):
            # Same ongoing incident state -> SUPPRESS TELEGRAM SPAM
            should_notify = False
        elif existing.state == IncidentState.RESOLVED:
            # Reopened incident -> Notify
            existing.state = IncidentState.OPEN
            existing.notification_emitted_count += 1
            should_notify = True

        return existing, should_notify

    def resolve_incident(self, incident_type: str, repository_id: str) -> Optional[Tuple[AutonomicIncident, bool]]:
        """Resolves an active incident and triggers a single RESOLVED Telegram notification."""
        fp = self.compute_fingerprint(incident_type, repository_id)
        existing = self._active_incidents.get(fp)
        if not existing or existing.state == IncidentState.RESOLVED:
            return None

        existing.state = IncidentState.RESOLVED
        existing.updated_at_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        existing.notification_emitted_count += 1
        return existing, True
