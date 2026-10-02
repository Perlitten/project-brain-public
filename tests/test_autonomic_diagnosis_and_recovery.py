"""Acceptance tests for Milestone v0.7.2: Autonomic Diagnosis & Recovery."""

from brain.memory.source_manifest import revision_matches
from brain.operations.incidents import DerivedImpact, DerivedImpactType, IncidentState, IncidentStateMachine
from brain.operations.remediation import AutonomicRemediationEngine


def test_source_revision_comparator_never_compares_different_revision_namespaces():
    """Killer Test: Verifies that short SHAs, snapshot prefixes, and bare Git SHAs match correctly."""
    # Bare full SHA matches short SHA
    assert revision_matches("b7d19fe440a84bdb", "b7d19fe") is True
    assert revision_matches("b7d19fe", "b7d19fe440a84bdb") is True

    # Snapshot prefix matches bare SHA
    assert revision_matches("b7d19fe440a84bdb", "snapshot:b7d19fe440a84bdb") is True
    assert revision_matches("snapshot:0f5fe87", "0f5fe87") is True
    assert revision_matches("snapshot:0f5fe87:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890", "0f5fe87") is True

    # Truly different revisions fail cleanly
    assert revision_matches("b7d19fe440a84bdb", "2e41633cfbb51946") is False
    assert revision_matches("snapshot:0f5fe87", "snapshot:9999999") is False


def test_incident_state_machine_deduplication():
    """Verifies that 6 consecutive health checks emit EXACTLY 1 Telegram OPEN notification."""
    sm = IncidentStateMachine()

    derived = [
        DerivedImpact(impact_type=DerivedImpactType.JOB_FAILURE, description="Reindex job failed"),
        DerivedImpact(impact_type=DerivedImpactType.FRESHNESS_UNVERIFIED, description="Repository freshness unverified"),
        DerivedImpact(impact_type=DerivedImpactType.VECTOR_COVERAGE_UNVERIFIED, description="Vectors incomplete"),
    ]

    notifications = []

    # Run 6 health diagnosis checks over 60 minutes
    for tick in range(6):
        inc, notify = sm.report_root_incident(
            incident_type="REINDEX_SOURCE_REVISION_MISMATCH",
            repository_id="project-brain",
            root_cause_summary="Reindex source revision mismatch",
            expected_revision="b7d19fe",
            actual_revision="snapshot:0f5fe87",
            derived_impacts=derived,
        )
        if notify:
            notifications.append(inc)

    # EXACTLY 1 Telegram notification emitted!
    assert len(notifications) == 1
    assert notifications[0].state == IncidentState.OPEN
    assert len(notifications[0].derived_impacts) == 3


def test_embedding_backfill_blocked_by_source_lineage():
    """Verifies that vector backfill is BLOCKED until repository lineage is verified."""
    sm = IncidentStateMachine()
    engine = AutonomicRemediationEngine()

    inc, _ = sm.report_root_incident(
        incident_type="VECTOR_COVERAGE_GAP",
        repository_id="project-brain",
        root_cause_summary="Vectors incomplete",
    )

    # Without verified lineage -> BLOCKED_BY_SOURCE_LINEAGE
    result = engine.execute_remediation(inc, lineage_verified=False)
    assert result.success is False
    assert any("BLOCKED_BY_SOURCE_LINEAGE" in r for r in result.rejection_reasons)


def test_autonomic_resolution_triggers_single_resolved_notification():
    """Verifies that repairing lineage transitions incident to RESOLVED and emits 1 RESOLVED notification."""
    sm = IncidentStateMachine()

    inc, _ = sm.report_root_incident(
        incident_type="REINDEX_SOURCE_REVISION_MISMATCH",
        repository_id="project-brain",
        root_cause_summary="Reindex source revision mismatch",
    )

    resolved_result = sm.resolve_incident("REINDEX_SOURCE_REVISION_MISMATCH", "project-brain")
    assert resolved_result is not None
    resolved_inc, notify = resolved_result
    assert notify is True
    assert resolved_inc.state == IncidentState.RESOLVED
