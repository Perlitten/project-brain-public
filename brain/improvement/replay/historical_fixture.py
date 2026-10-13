"""Historical synthetic trajectory fixture, retained for import compatibility.

This helper constructs a record with fixed demonstration metrics. It never
executes code, creates a sandbox, runs tests or measures a model. These values
are not benchmark evidence. The return schema and numbers are retained so
existing callers and historical reproductions remain compatible.
"""

import hashlib
import time
import warnings
from brain.improvement.models import TrajectoryRecord, TrajectoryTask, TrajectoryRouting, TrajectoryBody, TrajectoryOutcome, TrajectoryCost


class IsolatedReplayRunner:
    """Construct historical synthetic records; the name is a compatibility API."""

    def run_replay(
        self,
        bundle_id: str,
        champion_bundle_id: str,
        task: TrajectoryTask,
        execution_mode: str = "offline_replay",
    ) -> TrajectoryRecord:
        """Return a synthetic fixture without executing or measuring a task."""
        warnings.warn(
            "IsolatedReplayRunner is a historical fixture: no code executes; "
            "outcomes, token counts and time are synthetic, not benchmark evidence.",
            RuntimeWarning,
            stacklevel=2,
        )
        traj_id = f"trj-replay-{hashlib.sha256(f'{bundle_id}-{task.category}-{time.time()}'.encode('utf-8')).hexdigest()[:10]}"

        return TrajectoryRecord(
            trajectory_id=traj_id,
            task_id=f"tsk-{task.category}",
            session_id="sess-replay",
            agent_bundle_id=bundle_id,
            champion_bundle_id=champion_bundle_id,
            candidate_id=bundle_id if bundle_id != champion_bundle_id else None,
            execution_mode=execution_mode,
            task=task,
            routing=TrajectoryRouting(selected_route="PHASED_BRAIN_SELECTIVE", confidence=0.9),
            trajectory=TrajectoryBody(checkpoints=["plan_accepted", "implementation_done", "tests_passed"]),
            outcome=TrajectoryOutcome(binary_success=True, diagnostic_score=92.0),
            cost=TrajectoryCost(model_input_tokens=15000, model_output_tokens=1200, wall_time_ms=3500.0),
        )
