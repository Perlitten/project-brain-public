"""Waiver and Exception Workflow for Architectural Change Guard (Phase 5)."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import yaml

from brain.insights.drift_analyzer import DriftFinding


@dataclass
class DriftWaiver:
    id: str
    rule_id: str
    owner: str
    reason: str
    created_at: str  # 'YYYY-MM-DD'
    expires_at: str  # 'YYYY-MM-DD'
    fingerprint: Optional[str] = None
    reference: Optional[str] = None
    file_path: Optional[str] = None

    def is_expired(self, current_date: Optional[datetime.date] = None) -> bool:
        today = current_date or datetime.date.today()
        try:
            exp_date = datetime.date.fromisoformat(self.expires_at)
            return today > exp_date
        except ValueError:
            return True

    def matches(self, finding: DriftFinding) -> bool:
        if self.rule_id != finding.rule_id:
            return False
        if self.fingerprint and self.fingerprint != finding.fingerprint:
            return False
        if self.file_path and self.file_path not in finding.file_path:
            return False
        return True


class DriftWaiverManager:
    """Manages parsing and matching of architectural drift waivers."""

    def __init__(self, waiver_file_path: Path):
        self.file_path = waiver_file_path.resolve()

    def load_waivers(self) -> List[DriftWaiver]:
        if not self.file_path.exists():
            return []

        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as exc:
            raise ValueError(f"Malformed drift waivers YAML in {self.file_path}: {exc}")

        if not isinstance(data, dict):
            raise ValueError(f"Invalid waiver schema in {self.file_path}: Root must be a dict")

        waiver_list = data.get("waivers") or []
        waivers: List[DriftWaiver] = []

        for item in waiver_list:
            if not isinstance(item, dict):
                continue
            w_id = str(item.get("id", ""))
            rule_id = str(item.get("rule_id", ""))
            owner = str(item.get("owner", ""))
            reason = str(item.get("reason", ""))
            created_at = str(item.get("created_at", ""))
            expires_at = str(item.get("expires_at", ""))

            if not w_id or not rule_id or not expires_at:
                raise ValueError(f"Waiver entry missing required fields (id, rule_id, expires_at): {item}")

            fp = str(item["fingerprint"]) if item.get("fingerprint") is not None else None
            ref = str(item["reference"]) if item.get("reference") is not None else None
            fpath = str(item["file_path"]) if item.get("file_path") is not None else None

            waivers.append(
                DriftWaiver(
                    id=w_id,
                    rule_id=rule_id,
                    owner=owner,
                    reason=reason,
                    created_at=created_at,
                    expires_at=expires_at,
                    fingerprint=fp,
                    reference=ref,
                    file_path=fpath,
                )
            )

        return waivers

    def evaluate_finding_waiver(
        self,
        finding: DriftFinding,
        waivers: List[DriftWaiver],
        current_date: Optional[datetime.date] = None,
    ) -> tuple[str, Optional[DriftWaiver]]:
        """Classify finding waiver status: 'active', 'waived', or 'waiver_expired'."""
        matching_waivers = [w for w in waivers if w.matches(finding)]
        if not matching_waivers:
            return ("active", None)

        for w in matching_waivers:
            if not w.is_expired(current_date):
                return ("waived", w)

        # If matching waivers exist but all are expired
        return ("waiver_expired", matching_waivers[0])
