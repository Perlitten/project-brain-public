"""Unit Tests for Selective Holdout Suite in Project Brain v0.5.3."""

from benchmarks.utility_pilot.tasks.selective_v053_tasks import SELECTIVE_HOLDOUT_V053_TASKS


def test_selective_holdout_task_suite():
    assert len(SELECTIVE_HOLDOUT_V053_TASKS) == 12
    multi_repo_count = sum(1 for t in SELECTIVE_HOLDOUT_V053_TASKS.values() if t["multi_repo"])
    assert multi_repo_count >= 6
