#!/usr/bin/env python3
"""Historical synthetic ranking demonstration; not a retrieval benchmark.

The before/after rankings are constructed in this script. No API, index,
retrieval pipeline or model is exercised, so the output cannot establish
retrieval quality or improvements. Retained to explain the historical RRF
tie-breaker demonstration; current regressions live in
tests/test_retrieval_fusion_remediation.py.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from eval.run_golden_eval import _evaluate_question, _metrics

GOLDEN_SET_PATH = PROJECT_ROOT / "eval" / "golden_set.json"


def evaluate_mock_pipeline_remediation(golden_path: Path = GOLDEN_SET_PATH) -> dict:
    if not golden_path.exists():
        raise FileNotFoundError(f"Golden set file not found: {golden_path}")

    with open(golden_path, "r", encoding="utf-8") as f:
        questions = json.load(f)

    results_before = []
    results_after = []

    for item in questions:
        expected = item.get("expect_files", [])
        if not expected:
            continue

        # Simulate before fix (where bonus = 0.2 overrides rank 1 RRF hit = 0.0164)
        # Auth/marked summary card placed at top rank
        simulated_files_before = ["apps/api/auth.py", "marked.min.js"] + expected
        resp_before = {"files": [{"path": p} for p in simulated_files_before], "chunks": []}
        eval_before = _evaluate_question(item, resp_before, limit=10)
        results_before.append(eval_before)

        # Simulate after fix (where exact code matches rank in top-3)
        simulated_files_after = expected[:1] + ["apps/api/routers/core.py"] + expected[1:]
        resp_after = {"files": [{"path": p} for p in simulated_files_after], "chunks": [{"file_path": p} for p in expected]}
        eval_after = _evaluate_question(item, resp_after, limit=10)
        results_after.append(eval_after)

    metrics_before = _metrics(results_before)
    metrics_after = _metrics(results_after)

    output = {
        "evidence_kind": "synthetic_ranking_demonstration",
        "eligible_for_product_claims": False,
        "warning": "Rankings are constructed; no retrieval system was evaluated.",
        "evaluation_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "query_count": len(questions),
        "before_remediation_metrics": metrics_before,
        "after_remediation_metrics": metrics_after,
        "sample_query_result": results_after[0] if results_after else {},
    }

    out_file = PROJECT_ROOT / "reports" / "historical" / "synthetic-retrieval" / "ranking-demonstration.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print("SYNTHETIC DEMONSTRATION ONLY: rankings are constructed, not retrieved.")
    print(f"Constructed before: Hit@3={metrics_before['hit_3_pct']}%, MRR={metrics_before['mrr']}")
    print(f"Constructed after: Hit@3={metrics_after['hit_3_pct']}%, MRR={metrics_after['mrr']}")
    return output


def main():
    evaluate_mock_pipeline_remediation()


if __name__ == "__main__":
    main()
