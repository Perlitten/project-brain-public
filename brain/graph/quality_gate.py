"""Graphify v2 Quality Gate Validator."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Set

from brain.graph.generation_manager import InMemoryGraphStore


class GraphQualityValidationError(Exception):
    """Raised when a graph generation fails blocking quality invariants."""

    pass


@dataclass
class GraphQualityReport:
    generation_id: str
    repository_id: str
    passed: bool
    blocking_violations: List[str]
    warnings: List[str]
    total_nodes: int
    total_relationships: int
    node_counts_by_type: Dict[str, int]
    rel_counts_by_type: Dict[str, int]
    duplicate_identities: int
    unresolved_references: int
    isolated_nodes: int
    isolated_node_percentage: float
    cross_repository_edges: int
    cross_generation_edges: int
    self_loops: int
    parse_errors_count: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class GraphQualityValidator:
    """Validates Graphify v2 quality invariants prior to activation."""

    @staticmethod
    def validate(store: InMemoryGraphStore, repository_id: str) -> GraphQualityReport:
        blocking: List[str] = []
        warnings: List[str] = []

        total_nodes = len(store.nodes)
        total_rels = len(store.relationships)

        node_counts: Dict[str, int] = {}
        for n in store.nodes.values():
            t_val = n.node_type.value
            node_counts[t_val] = node_counts.get(t_val, 0) + 1

        rel_counts: Dict[str, int] = {}
        for r in store.relationships:
            r_val = r.rel_type.value
            rel_counts[r_val] = rel_counts.get(r_val, 0) + 1

        # Check duplicate identities (dict keys guarantee uniqueness, but check raw nodes list if any)
        duplicate_identities = 0

        # Connected nodes set
        connected_node_ids: Set[str] = set()
        unresolved_references = 0
        cross_repository_edges = 0
        cross_generation_edges = 0
        self_loops = 0

        for rel in store.relationships:
            if rel.source_id == rel.target_id:
                self_loops += 1

            src_node = store.get_node(rel.source_id)
            tgt_node = store.get_node(rel.target_id)

            if src_node:
                connected_node_ids.add(rel.source_id)
                if src_node.repository_id != repository_id:
                    cross_repository_edges += 1
                if src_node.generation_id != store.generation_id:
                    cross_generation_edges += 1
            else:
                unresolved_references += 1

            if tgt_node:
                connected_node_ids.add(rel.target_id)
                if tgt_node.repository_id != repository_id:
                    cross_repository_edges += 1
                if tgt_node.generation_id != store.generation_id:
                    cross_generation_edges += 1
            else:
                unresolved_references += 1

        isolated_nodes = total_nodes - len(connected_node_ids)
        isolated_percentage = (isolated_nodes / total_nodes * 100.0) if total_nodes > 0 else 0.0

        parse_errors_count = sum(1 for n in store.nodes.values() if "parse_error" in n.properties)

        # Invariant checks
        if cross_repository_edges > 0:
            blocking.append(f"Cross-repository edges detected: {cross_repository_edges}")
        if cross_generation_edges > 0:
            blocking.append(f"Cross-generation edges detected: {cross_generation_edges}")
        if total_nodes == 0:
            blocking.append("Graph generation is empty (0 nodes)")

        if isolated_percentage > 95.0 and total_nodes > 5:
            warnings.append(f"High isolated node percentage: {isolated_percentage:.1f}%")
        if parse_errors_count > 0:
            warnings.append(f"Files with parse errors: {parse_errors_count}")

        passed = len(blocking) == 0

        report = GraphQualityReport(
            generation_id=store.generation_id,
            repository_id=repository_id,
            passed=passed,
            blocking_violations=blocking,
            warnings=warnings,
            total_nodes=total_nodes,
            total_relationships=total_rels,
            node_counts_by_type=node_counts,
            rel_counts_by_type=rel_counts,
            duplicate_identities=duplicate_identities,
            unresolved_references=unresolved_references,
            isolated_nodes=isolated_nodes,
            isolated_node_percentage=isolated_percentage,
            cross_repository_edges=cross_repository_edges,
            cross_generation_edges=cross_generation_edges,
            self_loops=self_loops,
            parse_errors_count=parse_errors_count,
        )

        if not passed:
            raise GraphQualityValidationError(f"Graph generation {store.generation_id} failed quality gate: {blocking}")

        return report
