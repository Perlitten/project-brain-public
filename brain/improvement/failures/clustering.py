"""Three-Layer Hybrid Failure Clustering Engine."""

from typing import Dict, List
from brain.improvement.models import TrajectoryRecord
from brain.improvement.failures.signatures import extract_deterministic_signature


class HybridClusterer:
    """Layer 1: Deterministic Signatures -> Layer 2: Structured Features -> Layer 3: Semantic Summaries."""

    def cluster_trajectories(self, records: List[TrajectoryRecord]) -> Dict[str, List[TrajectoryRecord]]:
        """Groups trajectory records by deterministic signature and category."""
        clusters: Dict[str, List[TrajectoryRecord]] = {}
        for r in records:
            sig = extract_deterministic_signature(r)
            if sig not in clusters:
                clusters[sig] = []
            clusters[sig].append(r)
        return clusters
