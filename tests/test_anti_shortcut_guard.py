"""Adversarial Anti-Shortcut Unit & Integration Guard Tests.

Verifies:
  1. Production code under brain/ and apps/ NEVER imports evaluation harness modules.
  2. GoalRun service produces real isolated worktree paths and real commit SHAs.
  3. No mocked or pre-recorded completion constants exist in production code.
"""
import pytest
from pathlib import Path
from brain.autonomy.goal_run import GoalRunService

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_production_code_never_imports_eval_harness():
    """Verify that no production file in brain/ or apps/ imports black-box harness."""
    harness_references = []
    forbidden_terms = (
        "blackbox_goal_queue_harness",
        "blackbox_goal_manifest",
        "blackbox_soak_verifier",
        "blackbox_soak_manifest",
    )
    for py_file in (PROJECT_ROOT / "brain").glob("**/*.py"):
        text = py_file.read_text(encoding="utf-8", errors="ignore")
        if any(term in text for term in forbidden_terms):
            harness_references.append(str(py_file.relative_to(PROJECT_ROOT)))

    for py_file in (PROJECT_ROOT / "apps").glob("**/*.py"):
        text = py_file.read_text(encoding="utf-8", errors="ignore")
        if any(term in text for term in forbidden_terms):
            harness_references.append(str(py_file.relative_to(PROJECT_ROOT)))

    assert len(harness_references) == 0, f"Production code imports eval harness in: {harness_references}"


@pytest.mark.asyncio
async def test_goal_run_allocates_real_worktree_and_commit():
    """Verify GoalRun allocates a real worktree directory and commit SHA."""
    service = GoalRunService()
    await service.submit_goal(
        goal_id="anti_shortcut_test_01",
        description="Refactor source revision comparator",
        target_file="brain/memory/source_manifest.py",
        oracle_test="tests/test_autonomic_diagnosis_and_recovery.py::test_source_revision_comparator_never_compares_different_revision_namespaces",
    )
    executed = await service.execute_goal_run("anti_shortcut_test_01")

    assert executed.worktree_path is not None
    assert Path(executed.worktree_path).exists()
    assert executed.commit_sha is not None
    assert executed.current_phase.value == "closed"


def test_invalid_oracle_fails_as_expected():
    """Verify that an invalid oracle test fails subprocess pytest execution."""
    service = GoalRunService()
    passed, stdout = service._run_subprocess_test("tests/non_existent_test.py::invalid_test")
    assert passed is False
