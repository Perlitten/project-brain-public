"""Tests for Workstream D Verification Gate and Bounded Repair Loop."""

from brain.execution.models import ExecutionContract
from brain.execution.verifier import (
    BoundedRepairLoop,
    RepairHypothesis,
    StructuredTestResult,
    VerificationGate,
)


def test_bounded_repair_loop():
    loop = BoundedRepairLoop(max_repairs=2)
    can_rep, reason = loop.can_repair("sig-1")
    assert can_rep is True

    hyp = RepairHypothesis("Fail", "Cause", "f.py", "Fix", 1)
    loop.record_attempt("sig-1", hyp)

    # Repeating same signature fails
    can_rep, reason = loop.can_repair("sig-1")
    assert can_rep is False
    assert "already attempted" in reason

    # Different signature succeeds up to max
    can_rep, reason = loop.can_repair("sig-2")
    assert can_rep is True
    loop.record_attempt("sig-2", hyp)

    # Reaching max repairs fails
    can_rep, reason = loop.can_repair("sig-3")
    assert can_rep is False
    assert "Max repair iterations" in reason


def test_verification_gate():
    contract = ExecutionContract(
        task_intent="Fix bug",
        success_criteria=["Tests pass"],
        prohibited_outcomes=[],
        repository_scope=["project-brain"],
        max_changed_files=2,
    )

    passing_tr = StructuredTestResult("pytest", exit_code=0, duration_seconds=1.0, passed_count=5, failed_count=0)
    passed, violations = VerificationGate.verify([passing_tr], arch_findings=0, modified_files=["brain/config/settings.py"], contract=contract)
    assert passed is True
    assert len(violations) == 0

    failing_tr = StructuredTestResult("pytest", exit_code=1, duration_seconds=1.0, passed_count=4, failed_count=1)
    passed, violations = VerificationGate.verify([failing_tr], arch_findings=1, modified_files=["f1.py", "f2.py", "f3.py"], contract=contract)
    assert passed is False
    assert len(violations) >= 3
