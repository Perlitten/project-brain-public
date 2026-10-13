"""Keep the old import contract while making synthetic evidence unmistakable."""

import pytest

from brain.improvement.models import TrajectoryTask
from brain.improvement.replay.historical_fixture import IsolatedReplayRunner as HistoricalFixture
from brain.improvement.replay.runner import IsolatedReplayRunner


@pytest.mark.parametrize("bundle_id, expected_candidate", [("champion", None), ("challenger", "challenger")])
def test_legacy_replay_import_warns_and_preserves_record_contract(bundle_id, expected_candidate):
    assert IsolatedReplayRunner is HistoricalFixture
    task = TrajectoryTask(category="compatibility_fixture", repository_ids=[], source_revisions={})

    with pytest.warns(RuntimeWarning, match="no code executes.*synthetic, not benchmark evidence"):
        record = IsolatedReplayRunner().run_replay(bundle_id, "champion", task)

    assert record.agent_bundle_id == bundle_id
    assert record.champion_bundle_id == "champion"
    assert record.candidate_id == expected_candidate
    assert record.task == task
    assert record.execution_mode == "offline_replay"
    assert record.outcome.binary_success is True
    assert record.outcome.diagnostic_score == 92.0
    assert record.cost.model_input_tokens == 15000
    assert record.cost.model_output_tokens == 1200
    assert record.cost.wall_time_ms == 3500.0
    assert record.trajectory.tool_calls == []
    assert record.trajectory.test_runs == []
