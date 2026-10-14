"""Compare captured, unchanged product tasks without altering benchmark inputs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from eval.run_golden_eval import _evaluate_question, _metrics


def task_metrics(rows: list[dict]) -> dict[str, float]:
    positive = [row for row in rows if row.get("expected")]
    count = len(positive) or 1
    ranks = [next((i for i, path in enumerate(row["paths"], 1) if path in row["expected"]), 0) for row in positive]
    return {"hit1":sum(rank == 1 for rank in ranks)/count,
            "hit3":sum(1 <= rank <= 3 for rank in ranks)/count,
            "mrr":sum(1/rank if rank else 0 for rank in ranks)/count,
            "recall":sum(sum(path in row["paths"] for path in row["expected"])/len(row["expected"])
                         for row in positive)/count}


def golden_metrics(rows: list[dict]) -> dict:
    """Recompute derived scores from captured paths, never trusted booleans."""
    evaluated = []
    for row in rows:
        files = row.get("response_files") or []
        chunks = row.get("response_chunk_files") or []
        evaluated.append(_evaluate_question(
            {"id": row.get("id"), "question": row.get("question"),
             "expect_files": row.get("expected_files") or []},
            {"files": [{"path": path} for path in files],
             "chunks": [{"file_path": path} for path in chunks]},
            max(5, len(files), len(chunks)),
        ))
    return _metrics(evaluated)


def compare(before: dict, after: dict) -> dict:
    failures = []
    if before.get("phase") != "before" or after.get("phase") != "after":
        failures.append("invalid comparison phases")
    for key in ("provider", "corpus_source", "input_hash"):
        if not before.get(key) or before.get(key) != after.get(key):
            failures.append(f"changed comparison control: {key}")
    for key, count in (("goldens",20),("tasks",11)):
        old_ids = {row["id"] for row in before[key]}
        new_ids = {row["id"] for row in after[key]}
        if old_ids != new_ids or len(old_ids) != count or len(before[key]) != count or len(after[key]) != count:
            failures.append(f"incomplete or changed {key}")
        old_rows = {row["id"]:row for row in before[key]}
        for row in after[key]:
            old = old_rows.get(row["id"], {})
            controls = ("question", "expected_files") if key == "goldens" else ("type", "input", "expected")
            if any(old.get(control) != row.get(control) for control in controls):
                failures.append(f"changed task input: {row['id']}")
    summaries = {}
    for kind, count in (("context",7),("impact",4)):
        old = [row for row in before["tasks"] if row["type"] == kind]
        new = [row for row in after["tasks"] if row["type"] == kind]
        if len(old) != count or len(new) != count:
            failures.append(f"incomplete {kind} tasks")
        old_metrics, new_metrics = task_metrics(old), task_metrics(new)
        summaries[kind] = {"before":old_metrics,"after":new_metrics}
        for metric in ("hit1","hit3","mrr","recall"):
            if new_metrics[metric] + 1e-9 < old_metrics[metric]:
                failures.append(f"{kind} {metric} regressed")
        for row in new:
            bounds = {"context-007":3, "impact-003":19, "impact-004":75}
            if row["id"] in bounds and len(row["paths"]) > bounds[row["id"]]:
                failures.append(f"{row['id']} adversarial check failed")
    old_goldens, new_goldens = golden_metrics(before["goldens"]), golden_metrics(after["goldens"])
    for metric in ("hit_1_pct","hit_3_pct","mrr"):
        if new_goldens[metric] + 1e-9 < old_goldens[metric]:
            failures.append(f"golden {metric} regressed")
    return {"pass":not failures,"failures":failures,"goldens":{"before":old_goldens,
            "after":new_goldens},"tasks":summaries,
            "scope":"Aggregate non-regression on all 31 unchanged tasks; individual misses remain explicit"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before",type=Path)
    parser.add_argument("after",type=Path)
    parser.add_argument("--output",type=Path)
    args = parser.parse_args()
    report = compare(json.loads(args.before.read_text()),json.loads(args.after.read_text()))
    encoded = json.dumps(report,indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded)
    raise SystemExit(0 if report["pass"] else 1)


if __name__ == "__main__":
    main()
