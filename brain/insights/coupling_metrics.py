"""Deterministic Subsystem and File Coupling Metrics Engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Tuple

from brain.graph.generation_manager import InMemoryGraphStore
from brain.insights.subsystem_config import SubsystemConfigManager, SubsystemSpec


@dataclass
class NodeCouplingMetrics:
    node_id: str
    label: str
    subsystem: str
    afferent_coupling: int  # Fan-in (Ca)
    efferent_coupling: int  # Fan-out (Ce)
    instability: float  # Ce / (Ca + Ce)
    internal_edges: int
    external_edges: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SubsystemCouplingMetrics:
    subsystem_name: str
    owners: List[str]
    file_count: int
    internal_edge_count: int
    external_edge_count: int
    afferent_coupling: int  # Incoming from other subsystems
    efferent_coupling: int  # Outgoing to other subsystems
    instability: float  # Ce / (Ca + Ce)
    forbidden_edge_count: int = 0
    cycle_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CouplingMetricsCalculator:
    """Calculates deterministic coupling metrics across files and subsystems."""

    @staticmethod
    def calculate(
        store: InMemoryGraphStore,
        subsystems: Dict[str, SubsystemSpec],
        subsystem_mgr: SubsystemConfigManager,
    ) -> Tuple[Dict[str, NodeCouplingMetrics], Dict[str, SubsystemCouplingMetrics]]:

        node_metrics: Dict[str, NodeCouplingMetrics] = {}
        subsystem_files: Dict[str, List[str]] = {name: [] for name in subsystems}

        # Map each file node to a subsystem
        for node_id, node in store.nodes.items():
            if node.normalized_path:
                sub_name = subsystem_mgr.find_subsystem_for_file(node.normalized_path, subsystems) or "unassigned"
                if sub_name not in subsystem_files:
                    subsystem_files[sub_name] = []
                subsystem_files[sub_name].append(node_id)

        # Calculate per-node Ca, Ce, internal vs external
        for node_id, node in store.nodes.items():
            outgoing = store.get_outgoing(node_id)
            incoming = store.get_incoming(node_id)

            sub_name = subsystem_mgr.find_subsystem_for_file(node.normalized_path, subsystems) or "unassigned"

            ca = len(incoming)
            ce = len(outgoing)
            instability = (ce / (ca + ce)) if (ca + ce) > 0 else 0.0

            internal = 0
            external = 0
            for edge in outgoing:
                target_node = store.get_node(edge.target_id)
                target_sub = (
                    subsystem_mgr.find_subsystem_for_file(target_node.normalized_path, subsystems) if target_node else "unassigned"
                )
                if target_sub == sub_name:
                    internal += 1
                else:
                    external += 1

            node_metrics[node_id] = NodeCouplingMetrics(
                node_id=node_id,
                label=node.properties.get("name") or node.normalized_path or node_id,
                subsystem=sub_name,
                afferent_coupling=ca,
                efferent_coupling=ce,
                instability=instability,
                internal_edges=internal,
                external_edges=external,
            )

        # Calculate per-subsystem metrics
        subsystem_metrics: Dict[str, SubsystemCouplingMetrics] = {}
        for sub_name, spec in subsystems.items():
            files = subsystem_files.get(sub_name, [])
            file_ids = set(files)

            internal_edges = 0
            incoming_external = 0
            outgoing_external = 0

            for edge in store.relationships:
                src_in = edge.source_id in file_ids
                tgt_in = edge.target_id in file_ids

                if src_in and tgt_in:
                    internal_edges += 1
                elif src_in and not tgt_in:
                    outgoing_external += 1
                elif not src_in and tgt_in:
                    incoming_external += 1

            ca = incoming_external
            ce = outgoing_external
            instability = (ce / (ca + ce)) if (ca + ce) > 0 else 0.0

            subsystem_metrics[sub_name] = SubsystemCouplingMetrics(
                subsystem_name=sub_name,
                owners=spec.owners,
                file_count=len(files),
                internal_edge_count=internal_edges,
                external_edge_count=ca + ce,
                afferent_coupling=ca,
                efferent_coupling=ce,
                instability=instability,
            )

        return node_metrics, subsystem_metrics
