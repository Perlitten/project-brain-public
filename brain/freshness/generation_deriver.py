"""Generation derivation — Phase C2.

Derives graph generation N+1 from an active generation N plus an
:class:`~brain.freshness.models.IncrementalPlan`, without ever mutating the
active generation in place.

Invariants enforced here:

* generation N is opened read-only; N+1 is built as a separate store;
* every copied entity has its generation-scoped qualified identity rewritten,
  so no cross-generation edge can survive;
* entities from deleted or renamed-away paths are tombstoned, never copied;
* relationships whose surviving endpoint disappeared are dropped;
* N+1 is validated before activation, so a failed derivation cannot partially
  activate;
* when the plan says incremental derivation is unsafe, a full rebuild runs and
  the reason is recorded.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import List, Optional

from brain.freshness.models import DerivationReport, IncrementalPlan, RebuildDecision
from brain.graph.builder_v2 import GraphBuilderV2
from brain.graph.extractors_v2 import ConfigExtractor, PythonStaticExtractor
from brain.graph.generation_manager import (
    GenerationMetadata,
    GraphGenerationManager,
    InMemoryGraphStore,
)
from brain.graph.quality_gate import (
    GraphQualityValidationError,
    GraphQualityValidator,
)
from brain.graph.schema_v2 import GraphNodeV2, GraphRelationshipV2

_CONFIG_SUFFIXES = {".yaml", ".yml", ".json"}


class GenerationDeriver:
    """Derives generation N+1 from generation N."""

    def __init__(self, repo_path: Path, repository_id: Optional[str] = None):
        self.repo_path = repo_path.resolve()
        self.repository_id = repository_id or self.repo_path.name
        self.brain_dir = self.repo_path / ".brain"
        self.mgr = GraphGenerationManager(self.brain_dir)

    # ── public API ──

    def derive(
        self,
        plan: IncrementalPlan,
        base_generation_id: Optional[str] = None,
        activate: bool = True,
    ) -> DerivationReport:
        """Derive the next generation according to ``plan``."""
        started = time.monotonic()
        base_generation_id = base_generation_id or self.mgr.get_active_generation_id() or ""

        base_store = (
            self.mgr.load_graph_store(base_generation_id) if base_generation_id else None
        )
        if base_store is None:
            return self._full_rebuild(
                plan,
                base_generation_id,
                reason=f"No loadable base generation '{base_generation_id}'",
                started=started,
                activate=activate,
            )

        base_owner = self._base_repository_id(base_generation_id, base_store)
        if base_owner and base_owner != self.repository_id:
            # Copying entities across repository identities would produce a graph
            # whose nodes disagree with their own generation — a blocking quality
            # violation. Rebuild from source under the requested identity instead.
            return self._full_rebuild(
                plan,
                base_generation_id,
                reason=(
                    f"Base generation belongs to repository '{base_owner}', "
                    f"not '{self.repository_id}'"
                ),
                started=started,
                activate=activate,
            )

        if plan.decision == RebuildDecision.FULL_REBUILD.value:
            return self._full_rebuild(
                plan,
                base_generation_id,
                reason=plan.fallback_reason or "planner requested full rebuild",
                started=started,
                activate=activate,
            )

        mismatch = self._revision_mismatch(plan.candidate_revision)
        if mismatch:
            return self._full_rebuild(
                plan, base_generation_id, reason=mismatch, started=started, activate=activate
            )

        return self._incremental(plan, base_generation_id, base_store, started, activate)

    # ── incremental path ──

    def _incremental(
        self,
        plan: IncrementalPlan,
        base_generation_id: str,
        base_store: InMemoryGraphStore,
        started: float,
        activate: bool,
    ) -> DerivationReport:
        meta = self.mgr.create_generation(self.repository_id, plan.candidate_revision)
        new_store = InMemoryGraphStore(meta.generation_id)

        report = DerivationReport(
            repository_id=self.repository_id,
            base_generation_id=base_generation_id,
            new_generation_id=meta.generation_id,
            decision=RebuildDecision.INCREMENTAL.value,
            base_revision=plan.base_revision,
            candidate_revision=plan.candidate_revision,
            replaced_paths=len(plan.re_extraction_scope),
            renamed_paths=len(plan.renamed_files),
        )

        affected = set(plan.affected_paths)
        tombstoned: List[str] = []

        # 1. Copy every entity whose file was untouched, rewriting identities.
        for node in base_store.nodes.values():
            if node.normalized_path in affected:
                tombstoned.append(node.qualified_id)
                continue
            new_store.add_node(self._rewrite_node(node, base_generation_id, meta.generation_id))
        report.copied_nodes = len(new_store.nodes)
        report.tombstoned_nodes = len(tombstoned)

        # 2. Re-extract every affected file that still exists.
        python_extractor = PythonStaticExtractor(
            self.repository_id, meta.generation_id, self.repo_path
        )
        config_extractor = ConfigExtractor(
            self.repository_id, meta.generation_id, self.repo_path
        )
        fresh_rels: List[GraphRelationshipV2] = []
        added_nodes = 0
        for rel_path in plan.re_extraction_scope:
            abs_path = self.repo_path / rel_path
            if not abs_path.is_file():
                # Planned for re-extraction but gone from the tree: treat as a
                # deletion rather than silently keeping generation-N entities.
                continue
            suffix = abs_path.suffix
            if suffix == ".py":
                nodes, rels = python_extractor.extract_file(abs_path)
            elif suffix in _CONFIG_SUFFIXES and not abs_path.name.endswith(".jsonl"):
                nodes, rels = config_extractor.extract_config(abs_path)
            else:
                continue
            for n in nodes:
                new_store.add_node(n)
                added_nodes += 1
            fresh_rels.extend(rels)
        report.added_nodes = added_nodes

        # 3. Copy generation-N relationships.
        #
        # A relationship belongs to the file of its source node: re-extracting
        # that file regenerates it, so copying it too would duplicate the edge.
        # Everything else is copied with rewritten endpoints, except edges that
        # would point at an entity whose file disappeared.
        removed_paths = set(plan.deleted_files) | {old for old, _new in plan.renamed_files}
        dropped = 0
        for rel in base_store.relationships:
            source_node = base_store.get_node(rel.source_id)
            if source_node is None or source_node.normalized_path in affected:
                dropped += 1
                continue
            target_node = base_store.get_node(rel.target_id)
            if target_node is not None and target_node.normalized_path in removed_paths:
                dropped += 1
                continue
            new_source = self._rewrite_id(rel.source_id, base_generation_id, meta.generation_id)
            new_target = self._rewrite_id(rel.target_id, base_generation_id, meta.generation_id)
            new_store.add_relationship(
                GraphRelationshipV2(
                    rel_type=rel.rel_type,
                    source_id=new_source,
                    target_id=new_target,
                    provenance=rel.provenance,
                    extractor=rel.extractor,
                    confidence=rel.confidence,
                    source_location=dict(rel.source_location),
                )
            )
        report.copied_relationships = len(new_store.relationships)
        report.dropped_relationships = dropped

        for rel in fresh_rels:
            new_store.add_relationship(rel)

        # 4. Validate before activation.
        return self._finalize(new_store, meta, report, tombstoned, plan, started, activate)

    # ── full rebuild path ──

    def _full_rebuild(
        self,
        plan: IncrementalPlan,
        base_generation_id: str,
        reason: str,
        started: float,
        activate: bool,
    ) -> DerivationReport:
        report = DerivationReport(
            repository_id=self.repository_id,
            base_generation_id=base_generation_id,
            new_generation_id="",
            decision=RebuildDecision.FULL_REBUILD.value,
            base_revision=plan.base_revision,
            candidate_revision=plan.candidate_revision,
            fallback_reason=reason,
        )
        builder = GraphBuilderV2(self.repo_path)
        builder.repo_id = self.repository_id
        try:
            meta, quality = builder.build_generation(plan.candidate_revision)
        except GraphQualityValidationError as exc:
            report.validation_errors.append(str(exc))
            report.duration_seconds = round(time.monotonic() - started, 3)
            return report

        report.new_generation_id = meta.generation_id
        report.added_nodes = meta.node_count
        report.copied_relationships = 0
        if activate:
            report.activated = self.mgr.activate_generation(meta.generation_id)
        report.duration_seconds = round(time.monotonic() - started, 3)
        return report

    # ── helpers ──

    def _finalize(
        self,
        store: InMemoryGraphStore,
        meta: GenerationMetadata,
        report: DerivationReport,
        tombstoned: List[str],
        plan: IncrementalPlan,
        started: float,
        activate: bool,
    ) -> DerivationReport:
        try:
            quality = GraphQualityValidator.validate(store, self.repository_id)
        except GraphQualityValidationError as exc:
            report.validation_errors.append(str(exc))
            meta.status = "failed"
            meta.error_message = str(exc)[:500]
            self.mgr.update_generation_metadata(meta)
            report.duration_seconds = round(time.monotonic() - started, 3)
            return report

        meta.metrics = quality.to_dict()
        meta.metrics["derivation"] = {
            "base_generation_id": report.base_generation_id,
            "decision": report.decision,
            "tombstoned": tombstoned[:1000],
            "tombstoned_total": len(tombstoned),
            "plan_fingerprint": plan.semantic_fingerprint(),
        }
        self.mgr.save_graph_store(store, meta)

        if activate:
            report.activated = self.mgr.activate_generation(meta.generation_id)
        report.duration_seconds = round(time.monotonic() - started, 3)
        return report

    def _base_repository_id(
        self, base_generation_id: str, base_store: InMemoryGraphStore
    ) -> str:
        """Repository identity recorded for the base generation, if any."""
        for gen in self.mgr.list_generations():
            if gen.generation_id == base_generation_id and gen.repository_id:
                return gen.repository_id
        for node in base_store.nodes.values():
            if node.repository_id:
                return node.repository_id
        return ""

    def _revision_mismatch(self, candidate_revision: str) -> str:
        """Incremental extraction reads the working tree; verify it is the candidate."""
        if not candidate_revision or candidate_revision in {"HEAD", "WORKTREE"}:
            return ""
        try:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                timeout=30,
            )
            candidate = subprocess.run(
                ["git", "rev-parse", candidate_revision],
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            return f"Cannot verify working-tree revision: {exc}"

        if head.returncode != 0 or candidate.returncode != 0:
            return "Cannot resolve working-tree or candidate revision"
        if head.stdout.strip() != candidate.stdout.strip():
            return (
                "Working tree is not at the candidate revision "
                f"({head.stdout.strip()[:12]} != {candidate.stdout.strip()[:12]})"
            )
        return ""

    @staticmethod
    def _rewrite_id(qualified_id: str, old_generation: str, new_generation: str) -> str:
        if not old_generation or old_generation == new_generation:
            return qualified_id
        return qualified_id.replace(f":{old_generation}:", f":{new_generation}:", 1)

    @classmethod
    def _rewrite_node(
        cls, node: GraphNodeV2, old_generation: str, new_generation: str
    ) -> GraphNodeV2:
        return GraphNodeV2(
            node_type=node.node_type,
            qualified_id=cls._rewrite_id(node.qualified_id, old_generation, new_generation),
            repository_id=node.repository_id,
            generation_id=new_generation,
            normalized_path=node.normalized_path,
            language=node.language,
            properties=dict(node.properties),
            source_evidence=dict(node.source_evidence),
        )
