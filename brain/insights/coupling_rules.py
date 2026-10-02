"""Declarative Subsystem Boundary Rules (COUPLING-001 through COUPLING-005)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List

from brain.graph.generation_manager import InMemoryGraphStore
from brain.insights.subsystem_config import SubsystemConfigManager, SubsystemSpec


@dataclass
class CouplingViolationFinding:
    rule_id: str
    rule_name: str
    severity: str
    source_node_id: str
    source_path: str
    source_subsystem: str
    target_node_id: str
    target_path: str
    target_subsystem: str
    description: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CouplingRulesEngine:
    """Evaluates Graphify v2 graph relationships against subsystem policy."""

    @staticmethod
    def evaluate(
        store: InMemoryGraphStore,
        subsystems: Dict[str, SubsystemSpec],
        subsystem_mgr: SubsystemConfigManager,
    ) -> List[CouplingViolationFinding]:

        findings: List[CouplingViolationFinding] = []

        for edge in store.relationships:
            src_node = store.get_node(edge.source_id)
            tgt_node = store.get_node(edge.target_id)
            if not src_node or not tgt_node:
                continue

            src_sub = subsystem_mgr.find_subsystem_for_file(src_node.normalized_path, subsystems)
            tgt_sub = subsystem_mgr.find_subsystem_for_file(tgt_node.normalized_path, subsystems)

            if not src_sub or not tgt_sub or src_sub == tgt_sub:
                continue

            spec = subsystems.get(src_sub)
            if not spec:
                continue

            # COUPLING-001: Forbidden Subsystem Dependency
            if tgt_sub in spec.forbidden_dependencies:
                findings.append(
                    CouplingViolationFinding(
                        rule_id="COUPLING-001",
                        rule_name="Forbidden Subsystem Dependency",
                        severity="critical",
                        source_node_id=src_node.qualified_id,
                        source_path=src_node.normalized_path,
                        source_subsystem=src_sub,
                        target_node_id=tgt_node.qualified_id,
                        target_path=tgt_node.normalized_path,
                        target_subsystem=tgt_sub,
                        description=f"Subsystem '{src_sub}' has forbidden dependency on '{tgt_sub}' via edge '{src_node.normalized_path}' -> '{tgt_node.normalized_path}'",
                    )
                )

            # COUPLING-004: Worker to UI/Static Coupling
            if src_sub == "workers" and ("ui" in tgt_sub or "static" in tgt_node.normalized_path):
                findings.append(
                    CouplingViolationFinding(
                        rule_id="COUPLING-004",
                        rule_name="Worker to UI/Static Coupling",
                        severity="critical",
                        source_node_id=src_node.qualified_id,
                        source_path=src_node.normalized_path,
                        source_subsystem=src_sub,
                        target_node_id=tgt_node.qualified_id,
                        target_path=tgt_node.normalized_path,
                        target_subsystem=tgt_sub,
                        description=f"Worker module '{src_node.normalized_path}' depends on UI/static asset '{tgt_node.normalized_path}'",
                    )
                )

        return findings
