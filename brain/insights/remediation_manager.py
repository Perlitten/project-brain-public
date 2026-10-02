"""Remediation Plan Lifecycle and Persistence Manager."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import List, Optional, Tuple

from brain.insights.remediation_models import RemediationOption, RemediationPlan


class RemediationPlanManager:
    """Manages remediation plan storage, retrieval, state transitions, and freshness validation."""

    def __init__(self, brain_dir: Path):
        self.brain_dir = brain_dir.resolve()
        self.plans_file = self.brain_dir / "remediation_plans.jsonl"

    def _load_all(self) -> List[RemediationPlan]:
        if not self.plans_file.exists():
            return []
        plans: List[RemediationPlan] = []
        try:
            with open(self.plans_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        d = json.loads(line)
                        opts = [RemediationOption(**o) for o in d.get("remediation_options", [])]
                        d["remediation_options"] = opts
                        plans.append(RemediationPlan(**d))
        except Exception:
            return []
        return plans

    def _save_all(self, plans: List[RemediationPlan]) -> None:
        self.brain_dir.mkdir(parents=True, exist_ok=True)
        temp_file = self.plans_file.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            for plan in plans:
                f.write(json.dumps(plan.to_dict()) + "\n")
        temp_file.replace(self.plans_file)

    def save_plan(self, plan: RemediationPlan) -> RemediationPlan:
        plans = self._load_all()
        # Supersede existing proposed plans for the same affected files
        for p in plans:
            if p.affected_files == plan.affected_files and p.state in {"proposed", "acknowledged"}:
                p.state = "superseded"
                p.updated_at_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        plans.append(plan)
        self._save_all(plans)
        return plan

    def get_plan(self, plan_id: str) -> Optional[RemediationPlan]:
        for p in self._load_all():
            if p.plan_id == plan_id:
                return p
        return None

    def list_plans(self, state: Optional[str] = None) -> List[RemediationPlan]:
        plans = self._load_all()
        if state:
            return [p for p in plans if p.state == state]
        return plans

    def update_state(self, plan_id: str, new_state: str) -> Optional[RemediationPlan]:
        plans = self._load_all()
        target: Optional[RemediationPlan] = None
        now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        for p in plans:
            if p.plan_id == plan_id:
                p.state = new_state
                p.updated_at_utc = now_str
                target = p
                break

        if target:
            self._save_all(plans)
        return target

    def validate_plan_freshness(self, plan_id: str, repo_path: Path) -> Tuple[bool, str]:
        plan = self.get_plan(plan_id)
        if not plan:
            return False, f"Plan '{plan_id}' not found"

        for rel_path, expected_hash in plan.file_hashes.items():
            target_file = repo_path / rel_path
            if not target_file.exists():
                self.update_state(plan_id, "invalidated")
                return False, f"File '{rel_path}' no longer exists; plan invalidated"
            current_hash = hashlib.sha256(target_file.read_bytes()).hexdigest()
            if current_hash != expected_hash:
                self.update_state(plan_id, "invalidated")
                return False, f"File '{rel_path}' content hash changed; plan invalidated as stale"

        return True, "Plan is fresh and valid"
