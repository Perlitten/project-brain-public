"""Tests for Workstream C Incremental Multi-File Executor."""

from pathlib import Path
from brain.execution.models import ExecutionContract
from brain.execution.planner import ActionablePlan
from brain.execution.step_executor import StepExecutor


def test_step_executor_success(tmp_path: Path):
    target_file = tmp_path / "brain" / "config" / "settings.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("x = 1\n", encoding="utf-8")

    contract = ExecutionContract(
        task_intent="Fix settings",
        success_criteria=["Pass"],
        prohibited_outcomes=[],
        repository_scope=["project-brain"],
    )
    plan = ActionablePlan(
        problem_statement="Update settings",
        primary_hypothesis="Missing setting",
        confirmed_files=["brain/config/settings.py"],
        ordered_steps=[{"file": "brain/config/settings.py", "expected_change": "Add MAX_EXPORTS"}],
        required_tests=[],
    )

    def dummy_apply(path: Path) -> str:
        path.write_text("x = 1\nMAX_EXPORTS = 100\n", encoding="utf-8")
        return "+ MAX_EXPORTS = 100"

    checkpoint = StepExecutor.execute_step(
        workspace_dir=tmp_path,
        plan=plan,
        step_idx=1,
        step_def={"file": "brain/config/settings.py", "expected_change": "Add MAX_EXPORTS"},
        contract=contract,
        apply_func=dummy_apply,
    )

    assert checkpoint.success is True
    assert checkpoint.before_hash != checkpoint.after_hash


def test_step_executor_unplanned_file_rejection(tmp_path: Path):
    contract = ExecutionContract(
        task_intent="Fix settings",
        success_criteria=["Pass"],
        prohibited_outcomes=[],
        repository_scope=["project-brain"],
    )
    plan = ActionablePlan(
        problem_statement="Update settings",
        primary_hypothesis="Missing setting",
        confirmed_files=["brain/config/settings.py"],
        ordered_steps=[{"file": "brain/config/settings.py", "expected_change": "Add MAX_EXPORTS"}],
        required_tests=[],
    )

    checkpoint = StepExecutor.execute_step(
        workspace_dir=tmp_path,
        plan=plan,
        step_idx=1,
        step_def={"file": "unplanned.py", "expected_change": "Hack"},
        contract=contract,
        apply_func=lambda p: "",
    )

    assert checkpoint.success is False
    assert "not in accepted plan" in checkpoint.error_message
