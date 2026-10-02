"""Tests for Workstream B Investigation and Planning."""

import pytest
from brain.execution.models import ExecutionContract
from brain.execution.planner import ActionablePlan, PlanValidator
from brain.execution.investigator import InvestigationScopeGuard


def test_plan_validator():
    contract = ExecutionContract(
        task_intent="Fix rate limit",
        success_criteria=["Tests pass"],
        prohibited_outcomes=["Data loss"],
        repository_scope=["project-brain"],
        max_changed_files=2,
        required_arch_constraints=["Direct DB queries must use facade"],
    )

    valid_plan = ActionablePlan(
        problem_statement="Fix rate limit issue in export router",
        primary_hypothesis="Missing configuration setting",
        confirmed_files=["brain/config/settings.py", "apps/api/routers/core.py"],
        ordered_steps=[
            {"file": "brain/config/settings.py", "expected_change": "Add MAX_EXPORTS setting"},
            {"file": "apps/api/routers/core.py", "expected_change": "Check rate limit in router"},
        ],
        required_tests=["tests/test_harness_api.py"],
        arch_constraints=["Direct DB queries must use facade"],
    )

    reasons = PlanValidator.validate(valid_plan, contract)
    assert len(reasons) == 0

    # Test invalid plan (exceeds file count, missing arch constraint)
    invalid_plan = ActionablePlan(
        problem_statement="Short",
        primary_hypothesis="Bad",
        confirmed_files=["f1.py", "f2.py", "f3.py"],
        ordered_steps=[],
        required_tests=[],
    )

    reasons = PlanValidator.validate(invalid_plan, contract)
    assert len(reasons) >= 3


def test_investigation_scope_guard():
    assert InvestigationScopeGuard.validate_tool_access("investigating", "view_file") is True
    with pytest.raises(PermissionError, match="forbidden during 'investigating' phase"):
        InvestigationScopeGuard.validate_tool_access("investigating", "replace_file_content")
