"""Compare captured, unchanged product tasks without altering benchmark inputs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from eval.run_golden_eval import _evaluate_question, _metrics
from eval.product_upgrade_capture import ROOT, input_hash


def golden_metrics(rows: list[dict], failures: list[str], phase: str) -> dict:
    """Re-score captured paths; stored ranks, hits and scores are untrusted."""
    scored = []
    for row in rows:
        if row.get("contract") == "locator-v2":
            response = {"results": [{"path": path} for path in row["response_files"]]}
            if row["response_chunk_files"] or len(row["response_files"]) != len(set(row["response_files"])):
                failures.append(f"{phase} {row['id']} inconsistent locator channels")
        else:
            response = {"files": [{"path": path} for path in row["response_files"]],
                        "chunks": [{"file_path": path} for path in row["response_chunk_files"]]}
        question = {"id": row["id"], "question": row["question"], "expect_files": row["expected_files"]}
        computed = _evaluate_question(question, response, 10)
        for key in ("rank", "rank_chunks", "rank_any", "hit_at_1", "hit_at_3", "hit_at_5",
                    "hit_at_3_chunks", "hit_at_3_any", "mrr", "status"):
            if row.get(key) != computed[key]:
                failures.append(f"{phase} {row['id']} inconsistent saved {key}")
        scored.append(computed)
    return _metrics(scored)


def task_metrics(rows: list[dict]) -> dict[str, float]:
    positive = [row for row in rows if row.get("expected")]
    count = len(positive) or 1
    ranks = [next((i for i, path in enumerate(row["paths"], 1) if path in row["expected"]), 0) for row in positive]
    return {"hit1":sum(rank == 1 for rank in ranks)/count,
            "hit3":sum(1 <= rank <= 3 for rank in ranks)/count,
            "mrr":sum(1/rank if rank else 0 for rank in ranks)/count,
            "recall":sum(sum(path in row["paths"] for path in row["expected"])/len(row["expected"])
                         for row in positive)/count}


def _compare(before: dict, after: dict) -> dict:
    failures = []
    if before.get("phase") != "before" or after.get("phase") != "after":
        failures.append("capture phases must be before, after in that order")
    for key in ("provider", "corpus_source", "input_hash", "requested_budgets", "retrieval_contract"):
        if not before.get(key) or before.get(key) != after.get(key):
            failures.append(f"changed comparison control: {key}")
    canonical_goldens = {row["id"]: row for row in json.loads((ROOT / "eval/golden_set.json").read_text(encoding="utf-8"))}
    canonical_tasks = {}
    for path in (ROOT / "eval/harness_benchmark/tasks").glob("*.json"):
        row = json.loads(path.read_text(encoding="utf-8"))
        if row["type"] in {"context", "impact"}:
            canonical_tasks[row["id"]] = row
    for phase, capture in (("before", before), ("after", after)):
        if capture.get("requested_budgets") != {"search_limit": 10, "context_max_tokens": 3500, "impact_fast": True}:
            failures.append(f"{phase} invalid or missing fixed capture budgets")
        allowed = {"search": {"locator-v2", "legacy-files-chunks"},
                   "context": {"runtime-v2", "legacy-context-pack"},
                   "impact": {"bounded-impact", "legacy-impact"}}
        for kind, choices in allowed.items():
            recorded = (capture.get("retrieval_contract") or {}).get(kind, [])
            if len(recorded) != 1 or recorded[0] not in choices:
                failures.append(f"{phase} missing, mixed or invalid {kind} contract")
        if capture.get("input_hash") != input_hash():
            failures.append(f"{phase} frozen input hash mismatch")
        if {row["id"] for row in capture["goldens"]} != set(canonical_goldens):
            failures.append(f"{phase} golden IDs do not match frozen inputs")
        if {row["id"] for row in capture["tasks"]} != set(canonical_tasks):
            failures.append(f"{phase} task IDs do not match frozen inputs")
        for row in capture["goldens"]:
            canonical = canonical_goldens.get(row["id"], {})
            if row["question"] != canonical.get("question") or row["expected_files"] != canonical.get("expect_files"):
                failures.append(f"{phase} {row['id']} changed frozen golden input")
            if row.get("contract") not in (capture.get("retrieval_contract") or {}).get("search", []):
                failures.append(f"{phase} {row['id']} missing or inconsistent retrieval contract")
        for row in capture["tasks"]:
            canonical = canonical_tasks.get(row["id"], {})
            if row.get("input") != canonical or row["expected"] != canonical.get("must_have_files", []):
                failures.append(f"{phase} {row['id']} changed frozen task input")
            if row.get("contract") not in (capture.get("retrieval_contract") or {}).get(row["type"], []):
                failures.append(f"{phase} {row['id']} missing or inconsistent retrieval contract")
            if row["expected"]:
                computed = task_metrics([row])
                if any(row.get(key) != computed[key] for key in ("hit1", "hit3", "mrr", "recall")):
                    failures.append(f"{phase} {row['id']} inconsistent saved task metrics")
            if any(path not in row["paths"] for path in row.get("slice_paths", [])):
                failures.append(f"{phase} {row['id']} slice/candidate mismatch")
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
            if row.get("effective_max_bytes") != old.get("effective_max_bytes"):
                failures.append(f"changed effective byte budget: {row['id']}")
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
            old_row = next((old_row for old_row in old if old_row["id"] == row["id"]), None)
            if row.get("expected") and old_row and task_metrics([row])["recall"] + 1e-9 < task_metrics([old_row])["recall"]:
                failures.append(f"{row['id']} required-file recall regressed")
            bounds = {"context-007":3, "impact-003":19, "impact-004":75}
            if row["id"] in bounds and len(row["paths"]) > bounds[row["id"]]:
                failures.append(f"{row['id']} adversarial check failed")
    old_goldens, new_goldens = golden_metrics(before["goldens"], failures, "before"), golden_metrics(after["goldens"], failures, "after")
    for phase, capture, computed in (("before", before, old_goldens), ("after", after, new_goldens)):
        if capture.get("golden_metrics") != computed:
            failures.append(f"{phase} inconsistent saved golden aggregate")
    for metric in ("hit_1_pct","hit_3_pct","hit_5_pct","mrr"):
        if new_goldens[metric] + 1e-9 < old_goldens[metric]:
            failures.append(f"golden {metric} regressed")
    return {"pass":not failures,"failures":failures,"goldens":{"before":old_goldens,
            "after":new_goldens},"tasks":summaries,
            "scope":"Aggregate non-regression on all 31 unchanged tasks; individual misses remain explicit"}


def compare(before: dict, after: dict) -> dict:
    try:
        return _compare(before, after)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        return {"pass": False, "failures": [f"incomplete or invalid evidence: {type(exc).__name__}"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before",type=Path)
    parser.add_argument("after",type=Path)
    parser.add_argument("--output",type=Path)
    args = parser.parse_args()
    report = compare(json.loads(args.before.read_text(encoding="utf-8")),json.loads(args.after.read_text(encoding="utf-8")))
    encoded = json.dumps(report,indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded)
    raise SystemExit(0 if report["pass"] else 1)


if __name__ == "__main__":
    main()
