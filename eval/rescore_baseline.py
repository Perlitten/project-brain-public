#!/usr/bin/env python3
"""Re-score a saved eval run with the CURRENT metric.

A metric that changes between runs makes before/after comparisons meaningless.
This applies today's scoring function to a stored run (which must have been
written with --full, so the raw /search responses are present), so a baseline
captured before a change can be compared against a run captured after it
without re-querying a index that no longer exists in that state.

Usage: python eval/rescore_baseline.py reports/golden_eval_results.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.run_golden_eval import _evaluate_question, _metrics  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("results", [])

    rescored = []
    skipped = 0
    for row in rows:
        response = row.get("response")
        if not isinstance(response, dict):
            skipped += 1
            continue
        question = {"id": row.get("id"), "question": row.get("question"), "expect_files": row.get("expected_files", [])}
        rescored.append(_evaluate_question(question, response, payload.get("limit", 5)))

    if skipped:
        print(f"warning: {skipped} row(s) had no stored raw response and were skipped (run with --full to keep them)")
    if not rescored:
        print("nothing to re-score")
        return 1

    m = _metrics(rescored)
    print(f"re-scored {len(rescored)} question(s) from {path}")
    print(f"  HIT@3     (lexical files channel) : {m['hit_3_pct']:.2f}%")
    print(f"  HIT@3_ANY (files + vector chunks) : {m['hit_3_any_pct']:.2f}%")
    print(f"  HIT@1={m['hit_1_pct']:.2f}%  HIT@5={m['hit_5_pct']:.2f}%  MRR={m['mrr']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
