"""Cycle Detection and Strongly Connected Component (SCC) Analysis."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any, Dict, List

from brain.graph.generation_manager import InMemoryGraphStore


@dataclass
class DirectedCycle:
    cycle_fingerprint: str
    nodes: List[str]
    length: int
    crosses_subsystems: bool
    subsystems_involved: List[str]
    severity: str  # 'low', 'medium', 'high', 'critical'

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CycleDetector:
    """Detects cycles and strongly connected components using Tarjan's algorithm."""

    @staticmethod
    def detect_cycles(store: InMemoryGraphStore, node_subsystem_map: Dict[str, str]) -> List[DirectedCycle]:
        # Build adjacency list
        adj: Dict[str, List[str]] = {}
        for edge in store.relationships:
            adj.setdefault(edge.source_id, []).append(edge.target_id)

        index = 0
        indices: Dict[str, int] = {}
        lowlink: Dict[str, int] = {}
        on_stack: Dict[str, bool] = {}
        stack: List[str] = []
        sccs: List[List[str]] = []

        def strongconnect(node_id: str):
            nonlocal index
            indices[node_id] = index
            lowlink[node_id] = index
            index += 1
            stack.append(node_id)
            on_stack[node_id] = True

            for neighbor in adj.get(node_id, []):
                if neighbor not in indices:
                    strongconnect(neighbor)
                    lowlink[node_id] = min(lowlink[node_id], lowlink[neighbor])
                elif on_stack.get(neighbor, False):
                    lowlink[node_id] = min(lowlink[node_id], indices[neighbor])

            if lowlink[node_id] == indices[node_id]:
                scc = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    scc.append(w)
                    if w == node_id:
                        break
                if len(scc) > 1:
                    sccs.append(scc)

        for n_id in store.nodes:
            if n_id not in indices:
                strongconnect(n_id)

        cycles: List[DirectedCycle] = []
        for scc in sccs:
            sorted_nodes = sorted(scc)
            fp = hashlib.sha256(":".join(sorted_nodes).encode()).hexdigest()[:16]

            subs_involved = sorted(list({node_subsystem_map.get(nid, "unassigned") for nid in scc}))
            crosses_subs = len(subs_involved) > 1

            if crosses_subs:
                severity = "critical" if len(subs_involved) >= 3 else "high"
            else:
                severity = "medium" if len(scc) >= 4 else "low"

            cycles.append(
                DirectedCycle(
                    cycle_fingerprint=fp,
                    nodes=scc,
                    length=len(scc),
                    crosses_subsystems=crosses_subs,
                    subsystems_involved=subs_involved,
                    severity=severity,
                )
            )

        return cycles
