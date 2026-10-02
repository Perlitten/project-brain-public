"""Coupling Intelligence Time-Series Trend Logger."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List


@dataclass
class CouplingTrendEntry:
    timestamp_utc: str
    generation_id: str
    git_revision: str
    subsystem_count: int
    cross_subsystem_edge_count: int
    forbidden_edge_count: int
    cycle_count: int
    largest_scc_size: int
    average_instability: float
    hotspot_count: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CouplingTrendTracker:
    """Manages appending and retrieving time-series coupling analytics entries."""

    def __init__(self, brain_dir: Path):
        self.brain_dir = brain_dir.resolve()
        self.log_file = self.brain_dir / "coupling_trends.jsonl"

    def record_snapshot(
        self,
        generation_id: str,
        git_revision: str,
        subsystem_count: int,
        cross_subsystem_edge_count: int,
        forbidden_edge_count: int,
        cycle_count: int,
        largest_scc_size: int,
        average_instability: float,
        hotspot_count: int,
    ) -> CouplingTrendEntry:
        self.brain_dir.mkdir(parents=True, exist_ok=True)
        now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        entry = CouplingTrendEntry(
            timestamp_utc=now_str,
            generation_id=generation_id,
            git_revision=git_revision,
            subsystem_count=subsystem_count,
            cross_subsystem_edge_count=cross_subsystem_edge_count,
            forbidden_edge_count=forbidden_edge_count,
            cycle_count=cycle_count,
            largest_scc_size=largest_scc_size,
            average_instability=average_instability,
            hotspot_count=hotspot_count,
        )

        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")

        return entry

    def get_trends(self, limit: int = 50) -> List[CouplingTrendEntry]:
        if not self.log_file.exists():
            return []
        entries: List[CouplingTrendEntry] = []
        try:
            with open(self.log_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entries.append(CouplingTrendEntry(**json.loads(line)))
        except Exception:
            return []
        return entries[-limit:]
