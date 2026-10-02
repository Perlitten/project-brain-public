"""Incremental Graph Planner — Phase C1.

Plans incremental updates from a real Git diff and decides when incremental
derivation is unsafe and a full rebuild must be used instead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

from brain.freshness.models import IncrementalPlan, RebuildDecision
from brain.graph.generation_manager import InMemoryGraphStore

# Files whose content changes the shape of the whole graph, not just one module.
_INFRASTRUCTURE_FILES = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "docker-compose.yml",
    "docker-compose.yaml",
}

_EXTRACTED_SUFFIXES = {".py", ".yaml", ".yml", ".json"}


class IncrementalPlanner:
    """Plans incremental graph updates from Git diff."""

    # Guardrails: beyond these a derived generation is no cheaper — and much
    # riskier — than a clean full rebuild.
    MAX_CHANGED_FILES = 200
    MAX_RENAMES = 20

    def plan_update(
        self,
        repo_path: Path,
        base_revision: str,
        candidate_revision: str,
        repository_id: str = "",
        base_store: Optional[InMemoryGraphStore] = None,
    ) -> IncrementalPlan:
        """Plan an incremental update from base to candidate revision.

        ``base_store`` is the generation-N graph; when supplied the plan reports
        the exact node/relationship counts that will be invalidated.
        """
        plan = IncrementalPlan(
            base_revision=base_revision,
            candidate_revision=candidate_revision,
            repository_id=repository_id,
        )

        try:
            result = subprocess.run(
                [
                    "git",
                    "diff",
                    "--name-status",
                    "-M",  # detect renames
                    base_revision,
                    candidate_revision,
                ],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                plan.fallback_reason = f"git diff failed: {result.stderr.strip()[:200]}"
                plan.decision = RebuildDecision.FULL_REBUILD.value
                return plan

            for line in result.stdout.splitlines():
                if not line.strip():
                    continue
                parts = line.split("\t")
                status = parts[0][0]  # A, M, D, R, C, T
                if status == "A" and len(parts) >= 2:
                    plan.added_files.append(parts[1])
                elif status in {"M", "T"} and len(parts) >= 2:
                    plan.modified_files.append(parts[1])
                elif status == "D" and len(parts) >= 2:
                    plan.deleted_files.append(parts[1])
                elif status in {"R", "C"} and len(parts) >= 3:
                    plan.renamed_files.append((parts[1], parts[2]))

        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            plan.fallback_reason = f"git diff error: {exc}"
            plan.decision = RebuildDecision.FULL_REBUILD.value
            return plan

        # Re-extraction scope: only files the extractors actually consume, and
        # only those that still exist at the candidate revision.
        live_paths = list(plan.added_files) + list(plan.modified_files)
        live_paths += [new for _old, new in plan.renamed_files]
        plan.re_extraction_scope = sorted(
            {p for p in live_paths if Path(p).suffix in _EXTRACTED_SUFFIXES}
        )

        if base_store is not None:
            self._annotate_from_store(plan, base_store)

        should_fallback, reason = self.should_fallback(plan)
        if should_fallback:
            plan.fallback_reason = reason
            plan.decision = RebuildDecision.FULL_REBUILD.value
        elif plan.total_changes == 0:
            plan.decision = RebuildDecision.NO_CHANGE.value
        else:
            plan.decision = RebuildDecision.INCREMENTAL.value

        return plan

    @staticmethod
    def _annotate_from_store(plan: IncrementalPlan, store: InMemoryGraphStore) -> None:
        """Count the generation-N entities the plan invalidates."""
        affected = set(plan.affected_paths)
        stale_node_ids = {
            node.qualified_id
            for node in store.nodes.values()
            if node.normalized_path in affected
        }
        plan.stale_nodes = len(stale_node_ids)
        plan.affected_symbols = sorted(
            node.qualified_id
            for node in store.nodes.values()
            if node.normalized_path in affected
            and node.node_type.value in {"Class", "Function", "Method"}
        )

        stale_rels = 0
        dependents: set[str] = set()
        for rel in store.relationships:
            src_stale = rel.source_id in stale_node_ids
            tgt_stale = rel.target_id in stale_node_ids
            if src_stale or tgt_stale:
                stale_rels += 1
                # A surviving node on the other end is a dependent entity: its
                # edges change even though its own file did not.
                if src_stale and not tgt_stale:
                    dependents.add(rel.target_id)
                elif tgt_stale and not src_stale:
                    dependents.add(rel.source_id)

        plan.stale_relationships = stale_rels
        plan.affected_relationships = stale_rels
        plan.dependent_entities = sorted(dependents)

    @classmethod
    def should_fallback(cls, plan: IncrementalPlan) -> Tuple[bool, str]:
        """Determine if incremental derivation is unsafe for this plan."""
        if plan.fallback_reason:
            return True, plan.fallback_reason

        total = plan.total_changes
        if total == 0:
            return False, ""

        if total > cls.MAX_CHANGED_FILES:
            return True, f"Too many changes ({total} files > {cls.MAX_CHANGED_FILES})"

        if len(plan.renamed_files) > cls.MAX_RENAMES:
            return True, f"Too many renames ({len(plan.renamed_files)} > {cls.MAX_RENAMES})"

        touched = set(plan.added_files) | set(plan.modified_files) | set(plan.deleted_files)
        infra = sorted(f for f in touched if Path(f).name in _INFRASTRUCTURE_FILES)
        if infra:
            return True, f"Infrastructure files changed: {infra}"

        return False, ""

    @staticmethod
    def derivation_preview(plan: IncrementalPlan) -> Dict[str, object]:
        """Cheap preview of what deriving N+1 from this plan would do.

        This is a projection of the plan only — the authoritative numbers come
        from :class:`brain.freshness.generation_deriver.GenerationDeriver`.
        """
        return {
            "decision": plan.decision,
            "add_count": len(plan.added_files),
            "replace_count": len(plan.modified_files),
            "delete_count": len(plan.deleted_files),
            "tombstone_count": len(plan.deleted_files) + len(plan.renamed_files),
            "rename_count": len(plan.renamed_files),
            "stale_nodes": plan.stale_nodes,
            "stale_relationships": plan.stale_relationships,
            "fallback_to_full": plan.decision == RebuildDecision.FULL_REBUILD.value,
            "fallback_reason": plan.fallback_reason or "",
            "re_extraction_scope": plan.re_extraction_scope,
        }
