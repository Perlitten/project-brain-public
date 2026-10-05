#!/usr/bin/env python3
"""CI regression gate for the harness benchmark.

Fails the workflow when:
  - any task crashed (has an "error" outcome), or
  - the overall score falls below the hard floor in the baseline file.

Per-type score drops vs the baseline are reported as warnings, not
failures, until mock-provider scores are calibrated over several runs.
"""

import argparse
import json
import sys

HARD_FLOOR_FALLBACK = 0.5


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--baseline", required=True)
    args = ap.parse_args()

    with open(args.results) as f:
        payload = json.load(f)
    with open(args.baseline) as f:
        baseline = json.load(f)

    results = payload.get("results", [])
    scores = payload.get("summary", {})
    failures = []

    for r in results:
        if r.get("error"):
            failures.append(f"task {r['id']} crashed: {r['error']}")
        elif r.get("skipped"):
            print(f"::warning::task {r['id']} skipped: {r['skipped']}")

    overall = scores.get("overall")
    floor = baseline.get("overall_floor", HARD_FLOOR_FALLBACK)
    if overall is None:
        failures.append("no overall score in benchmark output")
    elif overall < floor:
        failures.append(f"overall score {overall} below floor {floor}")

    for ttype, min_score in baseline.get("type_minimums", {}).items():
        actual = scores.get(ttype)
        if actual is not None and actual < min_score:
            print(
                f"::warning::'{ttype}' score {actual} below baseline {min_score} "
                "(signal only, not a failure)"
            )

    if failures:
        for msg in failures:
            print(f"::error::{msg}")
        return 1
    print(f"benchmark gate passed (overall={overall})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
