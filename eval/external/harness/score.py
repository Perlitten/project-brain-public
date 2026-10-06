"""Score retrieval-eval JSONL rows: recall@k, MRR@10, nDCG@10, symbol recall,
bootstrap 95% CIs. Protocol: ../PROTOCOL.md section 5.

Usage:
  python score.py results/swebench60_*.jsonl [--label stratified60]
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import random
import sys
from pathlib import Path

SEED = 20261006
BOOT_B = 10_000
KS = (1, 5, 10, 30)


def recall_at(rank: list[str], gold: set[str], k: int) -> float:
    if not gold:
        return 0.0
    return len(set(rank[:k]) & gold) / len(gold)


def mrr(rank: list[str], gold: set[str], k: int = 10) -> float:
    for i, p in enumerate(rank[:k], 1):
        if p in gold:
            return 1.0 / i
    return 0.0


def ndcg(rank: list[str], gold: set[str], k: int = 10) -> float:
    dcg = sum(1.0 / math.log2(i + 1) for i, p in enumerate(rank[:k], 1) if p in gold)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(gold), k) + 1))
    return dcg / ideal if ideal else 0.0


def symbol_recall(rank: list[str], gold_syms: list[str], k: int) -> float:
    """Fraction of gold symbols whose file is in top-k."""
    if not gold_syms:
        return float("nan")
    top = set(rank[:k])
    return sum(1 for gs in gold_syms if gs.split("::")[0] in top) / len(gold_syms)


def bootstrap_ci(vals: list[float], seed: int = SEED) -> tuple[float, float, float]:
    vals = [v for v in vals if v == v]  # drop nan
    if not vals:
        return (float("nan"),) * 3
    n = len(vals)
    mean = sum(vals) / n
    rng = random.Random(seed)
    boots = sorted(
        sum(rng.choices(vals, k=n)) / n for _ in range(BOOT_B)
    )
    return mean, boots[int(0.025 * BOOT_B)], boots[int(0.975 * BOOT_B)]


def ndcg_graded(rank: list[str], rels: dict[str, int], k: int = 10) -> float:
    dcg = sum(rels.get(p, 0) / math.log2(i + 1) for i, p in enumerate(rank[:k], 1))
    ideal = sum(r / math.log2(i + 1) for i, r in enumerate(sorted(rels.values(), reverse=True)[:k], 1))
    return dcg / ideal if ideal else 0.0


def score_coir_rows(rows: list[dict]) -> dict:
    """CoIR rows: graded qrels, arm orders in 'ids' (corpus ids)."""
    per_arm: dict[str, dict[str, list[float]]] = {}
    for row in rows:
        rels = row.get("qrels", {})
        if not rels:
            continue
        for arm_name, arm in row.get("arms", {}).items():
            if not isinstance(arm, dict) or "ids" not in arm:
                continue
            rank = arm["ids"]
            m = per_arm.setdefault(arm_name, {k: [] for k in ("ndcg10", "mrr10", "r10", "r30", "ms")})
            m["ndcg10"].append(ndcg_graded(rank, rels, 10))
            gold = set(rels)
            m["mrr10"].append(mrr(rank, gold, 10))
            m["r10"].append(recall_at(rank, gold, 10))
            m["r30"].append(recall_at(rank, gold, 30))
            m["ms"].append(float(arm.get("ms") or 0.0))
    return _emit(per_arm, rows)


def score_repobench_rows(rows: list[dict]) -> dict:
    """RepoBench rows: arm 'order' is candidate-index order; gold_index."""
    per_arm: dict[str, dict[str, list[float]]] = {}
    for row in rows:
        gold = row.get("gold_index")
        if gold is None:
            continue
        for arm_name, arm in row.get("arms", {}).items():
            if not isinstance(arm, dict) or "order" not in arm:
                continue
            order = arm["order"]
            m = per_arm.setdefault(arm_name, {k: [] for k in ("acc1", "acc3", "acc5", "ms")})
            top = set(order[:1]); m["acc1"].append(1.0 if gold in top else 0.0)
            top = set(order[:3]); m["acc3"].append(1.0 if gold in top else 0.0)
            top = set(order[:5]); m["acc5"].append(1.0 if gold in top else 0.0)
            m["ms"].append(float(arm.get("ms") or 0.0))
    return _emit(per_arm, rows)


def _emit(per_arm: dict, rows: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for arm, metrics in sorted(per_arm.items()):
        out[arm] = {}
        for metric, vals in metrics.items():
            if metric == "ms":
                s = sorted(v for v in vals if v > 0)
                if s:
                    out[arm]["ms_p50"] = s[len(s) // 2]
                    out[arm]["ms_p95"] = s[min(len(s) - 1, int(len(s) * 0.95))]
                continue
            mean, lo, hi = bootstrap_ci(vals)
            out[arm][metric] = {"mean": round(mean, 4), "lo": round(lo, 4), "hi": round(hi, 4), "n": len(vals)}
    return {"arms": out, "scored_rows": len(rows)}


def score_rows(rows: list[dict]) -> dict:
    """rows: list of result dicts. Returns {arm: {metric: (mean, lo, hi, n)}}."""
    per_arm: dict[str, dict[str, list[float]]] = {}
    skipped = 0
    for row in rows:
        gold = set(row.get("gold_files", []))
        gold_syms = row.get("gold_symbols", [])
        if row.get("error") or not gold:
            skipped += 1
            continue
        for arm_name, arm in row.get("arms", {}).items():
            if not isinstance(arm, dict) or "paths" not in arm:
                continue
            rank = arm["paths"]
            m = per_arm.setdefault(arm_name, {k: [] for k in (
                "r1", "r5", "r10", "r30", "mrr10", "ndcg10", "symr10", "ms")})
            m["r1"].append(recall_at(rank, gold, 1))
            m["r5"].append(recall_at(rank, gold, 5))
            m["r10"].append(recall_at(rank, gold, 10))
            m["r30"].append(recall_at(rank, gold, 30))
            m["mrr10"].append(mrr(rank, gold, 10))
            m["ndcg10"].append(ndcg(rank, gold, 10))
            m["symr10"].append(symbol_recall(rank, gold_syms, 10))
            m["ms"].append(float(arm.get("ms", 0.0)))
    out: dict[str, dict] = {}
    for arm, metrics in sorted(per_arm.items()):
        out[arm] = {}
        for metric, vals in metrics.items():
            if metric == "ms":
                s = sorted(v for v in vals if v > 0)
                if s:
                    out[arm]["ms_p50"] = s[len(s) // 2]
                    out[arm]["ms_p95"] = s[min(len(s) - 1, int(len(s) * 0.95))]
                continue
            mean, lo, hi = bootstrap_ci(vals)
            out[arm][metric] = {"mean": round(mean, 4), "lo": round(lo, 4), "hi": round(hi, 4), "n": len(vals)}
    return {"arms": out, "skipped_rows": skipped, "scored_rows": len(rows) - skipped}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--label", default="")
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    rows = []
    for pat in args.inputs:
        for f in glob.glob(pat):
            for line in Path(f).read_text().splitlines():
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    sample = rows[0] if rows else {}
    if "qrels" in sample:
        res = score_coir_rows(rows)
    elif "gold_index" in sample:
        res = score_repobench_rows(rows)
    else:
        res = score_rows(rows)
    res["label"] = args.label
    res["n_rows"] = len(rows)
    print(json.dumps(res, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
