"""Deterministic Order Randomizer for Benchmark Tasks.

Generates reproducible execution order (Control first vs Treatment first) for each task.
"""

from __future__ import annotations

import random
from typing import Dict, List, Tuple
from benchmarks.utility_pilot.schemas.run_model import ExecutionMode


class ExecutionOrderRandomizer:
    """Randomizes mode order for each task deterministically given a seed."""

    def __init__(self, seed: int = 42):
        self.seed = seed
        self._rng = random.Random(seed)

    def determine_order_for_task(self, task_id: str) -> Tuple[ExecutionMode, ExecutionMode]:
        """Return (first_mode, second_mode) deterministically based on task_id and seed."""
        # Use deterministic hash of seed + task_id
        local_rng = random.Random(f"{self.seed}_{task_id}")
        if local_rng.choice([True, False]):
            return (ExecutionMode.CONTROL, ExecutionMode.TREATMENT)
        else:
            return (ExecutionMode.TREATMENT, ExecutionMode.CONTROL)

    def generate_schedule(self, task_ids: List[str]) -> Dict[str, List[ExecutionMode]]:
        """Generate full execution schedule for all task IDs."""
        schedule = {}
        for tid in task_ids:
            first, second = self.determine_order_for_task(tid)
            schedule[tid] = [first, second]
        return schedule
