"""Independent Brain Evidence Quality Scorer.

Scores EvidencePackV2 quality against gold manifests WITHOUT invoking LLMs:
- Required file recall
- Useful file precision
- Irrelevant file rate
- Useful token ratio
"""

from __future__ import annotations

from typing import Any, Dict
from brain.evidence.models import EvidencePackV2
from benchmarks.utility_pilot.schemas.run_model import GoldManifest


class EvidenceQualityScorer:
    """Evaluates evidence quality against gold standards without model involvement."""

    @staticmethod
    def score_pack(pack: EvidencePackV2, gold: GoldManifest) -> Dict[str, Any]:
        rec_files = pack.agent_summary.recommended_files if pack.agent_summary else pack.likely_relevant_files
        req_files = gold.required_files
        use_files = gold.useful_files

        true_req = [f for f in rec_files if f in req_files]
        true_use = [f for f in rec_files if f in use_files or f in req_files]
        irrelevant = [f for f in rec_files if f not in req_files and f not in use_files]

        req_recall = len(true_req) / max(1, len(req_files))
        use_precision = len(true_use) / max(1, len(rec_files))
        irrelevant_rate = len(irrelevant) / max(1, len(rec_files))

        return {
            "pack_id": pack.pack_id,
            "route": pack.route,
            "sufficiency": pack.sufficiency.value,
            "recommended_files_count": len(rec_files),
            "required_files_recall": round(req_recall, 4),
            "useful_files_precision": round(use_precision, 4),
            "irrelevant_file_rate": round(irrelevant_rate, 4),
            "true_required_files": true_req,
            "irrelevant_files": irrelevant,
        }
