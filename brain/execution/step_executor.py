"""Incremental Multi-File Step Executor for Project Brain v0.5.2."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict
from brain.execution.models import ExecutionContract
from brain.execution.planner import ActionablePlan


@dataclass
class StepCheckpoint:
    step_idx: int
    target_file: str
    before_hash: str
    after_hash: str
    diff_content: str
    success: bool
    error_message: str = ""


class StepExecutor:
    """Executes logical file-level steps incrementally with rollback checkpoints."""

    @staticmethod
    def calculate_file_hash(file_path: Path) -> str:
        if not file_path.exists():
            return "NON_EXISTENT"
        content = file_path.read_bytes()
        return hashlib.sha256(content).hexdigest()

    @classmethod
    def execute_step(
        cls,
        workspace_dir: Path,
        plan: ActionablePlan,
        step_idx: int,
        step_def: Dict[str, str],
        contract: ExecutionContract,
        apply_func,
    ) -> StepCheckpoint:
        target_rel_file = step_def.get("file", "")
        if not target_rel_file:
            return StepCheckpoint(step_idx=step_idx, target_file="", before_hash="", after_hash="", diff_content="", success=False, error_message="Step missing file target")

        # Plan-to-tool binding & Scope guard
        if target_rel_file not in plan.confirmed_files:
            return StepCheckpoint(step_idx=step_idx, target_file=target_rel_file, before_hash="", after_hash="", diff_content="", success=False, error_message=f"File '{target_rel_file}' is not in accepted plan confirmed_files.")

        if target_rel_file.startswith(".git") or ".git/" in target_rel_file:
            return StepCheckpoint(step_idx=step_idx, target_file=target_rel_file, before_hash="", after_hash="", diff_content="", success=False, error_message="Modification of .git is strictly prohibited.")

        target_abs_path = workspace_dir / target_rel_file
        before_hash = cls.calculate_file_hash(target_abs_path)

        try:
            diff_content = apply_func(target_abs_path)
            after_hash = cls.calculate_file_hash(target_abs_path)
            return StepCheckpoint(
                step_idx=step_idx,
                target_file=target_rel_file,
                before_hash=before_hash,
                after_hash=after_hash,
                diff_content=diff_content,
                success=True,
            )
        except Exception as exc:
            after_hash = cls.calculate_file_hash(target_abs_path)
            return StepCheckpoint(
                step_idx=step_idx,
                target_file=target_rel_file,
                before_hash=before_hash,
                after_hash=after_hash,
                diff_content="",
                success=False,
                error_message=str(exc),
            )
