"""Tests for Workstream A Execution Contract and State Machine."""

import pytest
from brain.execution.models import (
    ExecutionContract,
    ExecutionSession,
    ExecutionState,
    PhaseBudget,
    PhaseBudgetTracker,
)
from brain.execution.journal import ExecutionJournal


def test_execution_state_transitions():
    contract = ExecutionContract(
        task_intent="Fix bug",
        success_criteria=["Tests pass"],
        prohibited_outcomes=["Data loss"],
        repository_scope=["project-brain"],
    )
    session = ExecutionSession(
        execution_id="exec-101",
        repository_id="project-brain",
        repository_revision="1bf3e0d",
        task_fingerprint="fp-101",
        task_category="bug_fix",
        selected_brain_route="targeted_context_pack",
        evidence_pack_id="pack-101",
        model_provider="NVIDIA NIM API",
        exact_model_identifier="meta/llama-3.1-70b-instruct",
        contract=contract,
    )

    assert session.state == ExecutionState.RECEIVED

    # Valid transitions
    session.transition_to(ExecutionState.ASSESSING)
    session.transition_to(ExecutionState.INVESTIGATING)
    session.transition_to(ExecutionState.PLANNING)
    session.transition_to(ExecutionState.PLAN_READY)
    session.transition_to(ExecutionState.IMPLEMENTING)
    session.transition_to(ExecutionState.TESTING)
    session.transition_to(ExecutionState.VERIFYING)
    session.transition_to(ExecutionState.COMPLETED)

    # Re-entry from terminal state must fail closed
    with pytest.raises(ValueError, match="Cannot transition from terminal state"):
        session.transition_to(ExecutionState.IMPLEMENTING)


def test_phase_budget_tracker():
    tracker = PhaseBudgetTracker(
        phase_budgets={"investigation": PhaseBudget(max_provider_calls=2)}
    )
    tracker.record_usage("investigation", input_tokens=100, output_tokens=50)
    exhausted, reason = tracker.is_exhausted("investigation")
    assert not exhausted

    tracker.record_usage("investigation", input_tokens=100, output_tokens=50)
    exhausted, reason = tracker.is_exhausted("investigation")
    assert exhausted
    assert "Max provider calls" in reason


def test_execution_journal(tmp_path):
    journal = ExecutionJournal(execution_id="exec-test-1", storage_dir=tmp_path)
    journal.append("phase_entered", "investigating", {"step": 1})
    journal.append("file_inspected", "investigating", {"file": "brain/context/budget.py"})

    entries = journal.read_all()
    assert len(entries) == 2
    assert entries[0].event_type == "phase_entered"
    assert entries[1].details["file"] == "brain/context/budget.py"
