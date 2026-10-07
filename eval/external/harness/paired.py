"""Paired comparison between two arms on the same SWE-bench rows.

Per-instance R@k diffs are bootstrap-resampled (row-level, keeps pairing) for a
95% CI on the mean difference, plus an exact two-sided sign test. Used for:
  - dev150 ablation evidence (each v2 fix vs brain_v5 / dense)
  - STEP 3: frozen v2 config vs dense on Lite n=297 (paired significance test).

Usage:
  python paired.py results.jsonl ARM_A ARM_B [--k 10]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score import SEED, recall_at  # noqa: E402

BOOT_B = 10_000


def per_row_recall(row: dict, arm: str, k: int) -> float:
    gold = set(row.get("gold_files", []))
    a = row.get("arms", {}).get(arm, {})
    if not isinstance(a, dict) or "paths" not in a or a.get("error"):
        return float("nan")
    return recall_at(a["paths"], gold, k)


def paired_diff(rows: list[dict], arm_a: str, arm_b: str, k: int = 10) -> dict:
    diffs: list[float] = []
    wins = ties = losses = 0
    for row in rows:
        if row.get("error") or not row.get("gold_files"):
            continue
        va = per_row_recall(row, arm_a, k)
        vb = per_row_recall(row, arm_b, k)
        if va != va or vb != vb:
            continue
        d = va - vb
        diffs.append(d)
        if d > 1e-9:
            wins += 1
        elif d < -1e-9:
            losses += 1
        else:
            ties += 1
    if not diffs:
        return {"error": "no paired rows"}
    n = len(diffs)
    mean = sum(diffs) / n
    rng = random.Random(SEED)
    boots = sorted(sum(rng.choices(diffs, k=n)) / n for _ in range(BOOT_B))
    lo, hi = boots[int(0.025 * BOOT_B)], boots[int(0.975 * BOOT_B)]
    # exact two-sided sign test on non-tied pairs
    m = wins + losses
    if m:
        tail = sum(math.comb(m, i) for i in range(0, min(wins, losses) + 1)) / (2 ** m)
        p_sign = min(1.0, 2.0 * tail)
    else:
        p_sign = 1.0
    return {
        "arm_a": arm_a, "arm_b": arm_b, "k": k,
        "n_pairs": n, "mean_diff": round(mean, 4),
        "ci_lo": round(lo, 4), "ci_hi": round(hi, 4),
        "ci_excludes_zero": bool(lo > 0 or hi < 0),
        "wins": wins, "losses": losses, "ties": ties,
        "p_sign_test": round(p_sign, 6),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("arm_a")
    ap.add_argument("arm_b")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    rows = [json.loads(l) for l in Path(args.input).read_text().splitlines() if l.strip()]
    res = paired_diff(rows, args.arm_a, args.arm_b, args.k)
    res["n_rows"] = len(rows)
    print(json.dumps(res, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
