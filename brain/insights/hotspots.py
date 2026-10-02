"""Architecture Hotspots Detection Model."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List

from brain.graph.generation_manager import InMemoryGraphStore
from brain.insights.coupling_metrics import NodeCouplingMetrics
from brain.insights.cycle_detector import DirectedCycle


@dataclass
class ArchitectureHotspot:
    node_id: str
    label: str
    subsystem: str
    hotspot_score: float  # 0.0 to 100.0
    category: str  # 'god_object_risk', 'accidental_hub', 'unowned_boundary', 'legitimate_central_hub'
    reasons: List[str]
    cycle_participation_count: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class HotspotAnalyzer:
    """Analyzes graph nodes to identify architecture hotspots."""

    @staticmethod
    def analyze_hotspots(
        store: InMemoryGraphStore,
        node_metrics: Dict[str, NodeCouplingMetrics],
        cycles: List[DirectedCycle],
    ) -> List[ArchitectureHotspot]:

        cycle_nodes: Dict[str, int] = {}
        for c in cycles:
            for nid in c.nodes:
                cycle_nodes[nid] = cycle_nodes.get(nid, 0) + 1

        hotspots: List[ArchitectureHotspot] = []

        for node_id, metrics in node_metrics.items():
            node = store.get_node(node_id)
            if not node:
                continue

            score = 0.0
            reasons: List[str] = []

            # 1. Degree Centrality / High Fan-in & Fan-out
            degree = metrics.afferent_coupling + metrics.efferent_coupling
            if degree > 10:
                score += min(35.0, degree * 1.5)
                reasons.append(f"High degree centrality: {degree} total edges")

            # 2. Instability extremity
            if metrics.instability > 0.8 and metrics.efferent_coupling > 5:
                score += 20.0
                reasons.append(f"High instability: {metrics.instability:.2f} (Ce={metrics.efferent_coupling})")
            elif metrics.instability < 0.2 and metrics.afferent_coupling > 8:
                score += 15.0
                reasons.append(f"High afferent coupling hub (Ca={metrics.afferent_coupling})")

            # 3. Cycle participation
            cp = cycle_nodes.get(node_id, 0)
            if cp > 0:
                score += min(30.0, cp * 15.0)
                reasons.append(f"Participates in {cp} dependency cycle(s)")

            # 4. Unassigned subsystem
            if metrics.subsystem == "unassigned" and degree > 3:
                score += 15.0
                reasons.append("Unassigned subsystem boundary")

            score = min(100.0, score)

            if score >= 35.0:
                if cp > 0 and metrics.efferent_coupling > 5:
                    cat = "god_object_risk"
                elif metrics.subsystem == "unassigned":
                    cat = "unowned_boundary"
                elif metrics.afferent_coupling > 10:
                    cat = "legitimate_central_hub"
                else:
                    cat = "accidental_hub"

                hotspots.append(
                    ArchitectureHotspot(
                        node_id=node_id,
                        label=metrics.label,
                        subsystem=metrics.subsystem,
                        hotspot_score=round(score, 1),
                        category=cat,
                        reasons=reasons,
                        cycle_participation_count=cp,
                    )
                )

        return sorted(hotspots, key=lambda h: h.hotspot_score, reverse=True)
