"""Tests for Agent Integration V2."""

from pathlib import Path
from benchmarks.utility_pilot.harness.live_agent_adapter import LiveAgentAdapter
from benchmarks.utility_pilot.harness.telemetry import TelemetryTracker
from benchmarks.utility_pilot.schemas.run_model import ExecutionMode


def test_agent_integration_automatic_routed(tmp_path: Path):
    (tmp_path / "brain" / "graph").mkdir(parents=True, exist_ok=True)
    (tmp_path / "brain" / "graph" / "identity.py").write_text("def foo(): pass")

    telemetry = TelemetryTracker(run_id="run-test")
    task_def = {
        "task_id": "task_01",
        "prompt": "Refactor architecture boundary in brain/graph/identity.py",
    }
    adapter = LiveAgentAdapter(tmp_path, ExecutionMode.TREATMENT_AUTOMATIC_ROUTED, telemetry, task_def)
    assert adapter.mode == ExecutionMode.TREATMENT_AUTOMATIC_ROUTED
