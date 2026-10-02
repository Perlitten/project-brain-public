"""Trend Analytics Engine for Architectural Drift Intelligence (v0.3.0 Milestone).

Records scan historical snapshots and computes time-series statistics:
- Total violations over time
- New vs resolved counts
- Breakdown by rule and severity
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, List


@dataclass
class DriftTrendPoint:
    timestamp_utc: str
    repository_id: str
    total_violations: int
    new_count: int
    persistent_count: int
    moved_count: int
    resolved_count: int
    severity_breakdown: dict[str, int]
    rule_breakdown: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DriftTrendTracker:
    """Manages append-only JSONL time-series storage for architectural drift trends."""

    def __init__(self, trends_file_path: Path):
        self.file_path = trends_file_path.resolve()

    def record_scan_event(
        self,
        repository_id: str,
        evaluated_deltas: List[Any],
    ) -> DriftTrendPoint:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

        severity_breakdown = {"info": 0, "warning": 0, "critical": 0}
        rule_breakdown: dict[str, int] = {}
        delta_counts = {"new": 0, "persistent": 0, "moved": 0, "resolved": 0}

        for item in evaluated_deltas:
            finding = item.finding
            delta = item.delta_state

            if delta in delta_counts:
                delta_counts[delta] += 1

            if delta != "resolved":
                sev = finding.severity
                if sev in severity_breakdown:
                    severity_breakdown[sev] += 1
                rule_id = finding.rule_id
                rule_breakdown[rule_id] = rule_breakdown.get(rule_id, 0) + 1

        total_active = sum(1 for item in evaluated_deltas if item.delta_state != "resolved")

        point = DriftTrendPoint(
            timestamp_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            repository_id=repository_id,
            total_violations=total_active,
            new_count=delta_counts["new"],
            persistent_count=delta_counts["persistent"],
            moved_count=delta_counts["moved"],
            resolved_count=delta_counts["resolved"],
            severity_breakdown=severity_breakdown,
            rule_breakdown=rule_breakdown,
        )

        with open(self.file_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(point.to_dict()) + "\n")

        return point

    def get_history(self, limit: int = 100) -> List[dict[str, Any]]:
        if not self.file_path.exists():
            return []
        points: List[dict[str, Any]] = []
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        points.append(json.loads(line))
        except Exception:
            return points
        return points[-limit:]
