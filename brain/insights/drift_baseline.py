"""Baseline Storage & Delta Analysis Engine for Architectural Drift Intelligence v2 (Phase 4).

Features:
1. Baseline schema serialization/deserialization.
2. Delta comparison: classifies findings into new, persistent, moved, or resolved states.
3. Atomic baseline JSON writes (temp file -> fsync -> os.replace).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from brain.insights.drift_analyzer import DriftFinding

_BASELINE_LOCKS: Dict[str, threading.Lock] = defaultdict(threading.Lock)


@dataclass(frozen=True)
class EvaluatedFinding:
    """Drift finding enriched with delta state relative to previous baseline."""

    finding: DriftFinding
    delta_state: str  # 'new', 'persistent', 'moved', 'resolved'
    previous_line_number: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        res = asdict(self)
        res["finding"] = self.finding.to_dict()
        return res


@dataclass
class DriftBaseline:
    """Snapshot of previous architectural drift findings."""

    repository_id: str
    source_revision: Optional[str]
    created_at_utc: str
    rule_registry_version: str
    findings: List[dict[str, Any]]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DriftBaseline:
        return cls(
            repository_id=data.get("repository_id", "project-brain"),
            source_revision=data.get("source_revision"),
            created_at_utc=data.get("created_at_utc", ""),
            rule_registry_version=data.get("rule_registry_version", "1.0.0"),
            findings=data.get("findings", []),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DriftBaselineManager:
    """Manages baseline storage and delta state classification."""

    def __init__(self, baseline_path: Path):
        self.baseline_path = baseline_path.resolve()

    def load_baseline(self) -> Optional[DriftBaseline]:
        if not self.baseline_path.exists():
            return None
        try:
            with open(self.baseline_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return DriftBaseline.from_dict(data)
        except Exception:
            return None

    def save_baseline_atomic(self, baseline: DriftBaseline) -> None:
        """Atomic write protocol with lock: write temporary file -> fsync -> os.replace."""
        lock = _BASELINE_LOCKS[str(self.baseline_path)]
        with lock:
            self.baseline_path.parent.mkdir(parents=True, exist_ok=True)
            dir_path = self.baseline_path.parent

            with tempfile.NamedTemporaryFile("w", dir=dir_path, delete=False, encoding="utf-8") as tf:
                temp_name = tf.name
                json.dump(baseline.to_dict(), tf, indent=2)
                tf.flush()
                os.fsync(tf.fileno())

            os.replace(temp_name, self.baseline_path)

    def compute_deltas(
        self,
        current_findings: List[DriftFinding],
        baseline: Optional[DriftBaseline],
    ) -> List[EvaluatedFinding]:
        evaluated: List[EvaluatedFinding] = []

        if not baseline or not baseline.findings:
            # All findings are new
            for f in current_findings:
                evaluated.append(EvaluatedFinding(finding=f, delta_state="new"))
            return evaluated

        # Build map of baseline findings by fingerprint
        base_map: dict[str, dict[str, Any]] = {
            b.get("fingerprint", ""): b for b in baseline.findings if isinstance(b, dict)
        }
        current_fps = {f.fingerprint for f in current_findings}

        # Classify current findings: new, persistent, or moved
        for f in current_findings:
            if f.fingerprint not in base_map:
                evaluated.append(EvaluatedFinding(finding=f, delta_state="new"))
            else:
                prev = base_map[f.fingerprint]
                prev_line = prev.get("line_number")
                if prev_line == f.line_number:
                    evaluated.append(EvaluatedFinding(finding=f, delta_state="persistent", previous_line_number=prev_line))
                else:
                    evaluated.append(EvaluatedFinding(finding=f, delta_state="moved", previous_line_number=prev_line))

        # Classify resolved findings (present in baseline, missing from current scan)
        for fp, prev in base_map.items():
            if fp and fp not in current_fps:
                res_finding = DriftFinding(
                    fingerprint=fp,
                    rule_id=prev.get("rule_id", "DRIFT-UNKNOWN"),
                    rule_name=prev.get("rule_name", "unknown"),
                    file_path=prev.get("file_path", ""),
                    line_number=prev.get("line_number", 0),
                    containing_symbol=prev.get("containing_symbol", "global"),
                    imported_module=prev.get("imported_module", ""),
                    severity=prev.get("severity", "info"),
                    description=prev.get("description", "Violation resolved."),
                    remediation_guidance=prev.get("remediation_guidance", ""),
                )
                evaluated.append(EvaluatedFinding(finding=res_finding, delta_state="resolved", previous_line_number=res_finding.line_number))

        return evaluated
