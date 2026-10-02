"""Deep Change-Impact Traversal and Fusion Engine."""

from __future__ import annotations

import time
from pathlib import Path
from typing import List, Set, Tuple

from brain.graph.generation_manager import GraphGenerationManager
from brain.insights.git_diff import GitDiffEngine
from brain.insights.impact_models import ImpactedEntity, ImpactResult
from brain.insights.subsystem_config import SubsystemConfigManager
from brain.insights.test_impact import TestImpactSelector


class ImpactAnalysisEngine:
    """Performs bounded multi-channel graph traversal to analyze change blast radius."""

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path.resolve()
        self.gen_mgr = GraphGenerationManager(self.repo_path / ".brain")
        self.sub_mgr = SubsystemConfigManager(self.repo_path)

    def analyze_impact(
        self,
        base_rev: str = "HEAD~1",
        cand_rev: str = "HEAD",
        max_depth: int = 3,
        max_nodes: int = 500,
    ) -> ImpactResult:

        # 1. Get changed files via Git Diff
        diff_engine = GitDiffEngine(self.repo_path)
        changed_files = diff_engine.get_changed_files(base_rev, cand_rev)

        store = self.gen_mgr.get_active_graph_store()
        subsystems = self.sub_mgr.load_subsystems()

        impacted_entities: List[ImpactedEntity] = []
        visited_nodes: Set[str] = set()
        subsystems_affected: Set[str] = set()
        api_affected: Set[str] = set()
        worker_affected: Set[str] = set()
        risk_reasons: List[str] = []

        if not store:
            # Fallback mode when graph is unavailable
            for f in changed_files:
                sub_name = self.sub_mgr.find_subsystem_for_file(f, subsystems) or "unassigned"
                subsystems_affected.add(sub_name)
                impacted_entities.append(
                    ImpactedEntity(
                        entity_id=f"file:{f}",
                        normalized_path=f,
                        entity_type="File",
                        subsystem=sub_name,
                        evidence_category="direct",
                        confidence="confirmed",
                        confidence_reasons=["Directly modified in Git diff (graph unavailable)"],
                        traversal_path=[f],
                    )
                )
            risk_reasons.append("Graph unavailable; impact analysis running in direct Git fallback mode")
            suggested_tests = TestImpactSelector.select_tests(self.repo_path, impacted_entities)

            return ImpactResult(
                run_id=f"imp-{int(time.time())}",
                repository=self.repo_path.name,
                base_revision=base_rev,
                candidate_revision=cand_rev,
                created_at_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                directly_changed_files=changed_files,
                impacted_entities=impacted_entities,
                impacted_subsystems=sorted(list(subsystems_affected)),
                impacted_api_endpoints=[],
                impacted_worker_tasks=[],
                suggested_tests=suggested_tests,
                risk_reasons=risk_reasons,
            )

        # Build seed nodes from changed files
        seed_node_ids: List[str] = []
        for f in changed_files:
            sub_name = self.sub_mgr.find_subsystem_for_file(f, subsystems) or "unassigned"
            subsystems_affected.add(sub_name)

            for nid, node in store.nodes.items():
                if node.normalized_path == f:
                    seed_node_ids.append(nid)
                    if nid not in visited_nodes:
                        visited_nodes.add(nid)
                        impacted_entities.append(
                            ImpactedEntity(
                                entity_id=nid,
                                normalized_path=f,
                                entity_type=node.node_type.value,
                                subsystem=sub_name,
                                evidence_category="direct",
                                confidence="confirmed",
                                confidence_reasons=["Directly modified file"],
                                traversal_path=[f],
                            )
                        )

        # Bounded BFS traversal up to max_depth and max_nodes
        queue: List[Tuple[str, int, List[str]]] = []
        for nid in seed_node_ids:
            seed_node = store.get_node(nid)
            seed_path = seed_node.normalized_path if seed_node and seed_node.normalized_path else nid
            queue.append((nid, 0, [seed_path]))
        truncated = False

        while queue and len(visited_nodes) < max_nodes:
            curr_id, depth, path = queue.pop(0)
            if depth >= max_depth:
                continue

            # Check incoming and outgoing dependents
            dependents = store.get_incoming(curr_id) + store.get_outgoing(curr_id)
            for rel in dependents:
                target_id = rel.target_id if rel.source_id == curr_id else rel.source_id
                if target_id in visited_nodes:
                    continue

                tgt_node = store.get_node(target_id)
                if not tgt_node:
                    continue

                # Skip generic noisy builtins
                if tgt_node.normalized_path in {"", "builtins"}:
                    continue

                visited_nodes.add(target_id)
                tgt_sub = self.sub_mgr.find_subsystem_for_file(tgt_node.normalized_path, subsystems) or "unassigned"
                subsystems_affected.add(tgt_sub)

                new_path = path + [tgt_node.normalized_path or tgt_node.qualified_id]

                if tgt_node.node_type.value == "APIEndpoint":
                    api_affected.add(tgt_node.qualified_id)
                elif tgt_node.node_type.value == "WorkerTask":
                    worker_affected.add(tgt_node.qualified_id)

                conf = "high" if depth == 0 else "medium"
                ev_cat = "statically_referenced" if rel.confidence == "exact" else "graph_inferred"

                impacted_entities.append(
                    ImpactedEntity(
                        entity_id=target_id,
                        normalized_path=tgt_node.normalized_path,
                        entity_type=tgt_node.node_type.value,
                        subsystem=tgt_sub,
                        evidence_category=ev_cat,
                        confidence=conf,
                        confidence_reasons=[f"Traversed via relationship {rel.rel_type.value} (depth={depth+1})"],
                        traversal_path=new_path,
                    )
                )

                queue.append((target_id, depth + 1, new_path))

        if len(visited_nodes) >= max_nodes:
            truncated = True
            risk_reasons.append(f"Graph traversal truncated at maximum node limit ({max_nodes})")

        if len(subsystems_affected) > 1:
            risk_reasons.append(f"Changes impact multiple subsystems: {sorted(list(subsystems_affected))}")

        suggested_tests = TestImpactSelector.select_tests(self.repo_path, impacted_entities)

        return ImpactResult(
            run_id=f"imp-{int(time.time())}",
            repository=self.repo_path.name,
            base_revision=base_rev,
            candidate_revision=cand_rev,
            created_at_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            directly_changed_files=changed_files,
            impacted_entities=impacted_entities,
            impacted_subsystems=sorted(list(subsystems_affected)),
            impacted_api_endpoints=sorted(list(api_affected)),
            impacted_worker_tasks=sorted(list(worker_affected)),
            suggested_tests=suggested_tests,
            risk_reasons=risk_reasons,
            truncated=truncated,
        )
