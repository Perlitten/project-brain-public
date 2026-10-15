"""Validate and combine whole-package shard evidence without trusting stored scores."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from eval.project_mutation import assign_shards, candidate_hash, file_hash, load_inventory, outcome
from eval.mutation_types import read_type_exclusions


CATEGORIES = ("killed", "survived", "timed_out", "equivalent", "type_invalid", "pending", "inconclusive")


def aggregate(inventory: dict, reports: list[dict], invalid: dict, proof_hash: str,
              classifications: dict | None = None) -> dict:
    """Recompute counts, requiring an exact shard union and matching test provenance."""
    classifications = classifications or {}
    modules = {row["module"]: row for row in inventory["modules"]}
    codes: dict[str, int | None] = {}
    by_module: dict[str, dict] = {}
    test_hashes = None
    shard_count = None
    shard_ids = set()
    verified = True
    seconds = 0.0
    for report in reports:
        if report["base"] != inventory["base"] or report["tool"] != inventory["tool"]:
            raise ValueError("Report source base or tool differs from the inventory")
        if report["type_proof_sha256"] != proof_hash:
            raise ValueError("Report uses a different compiler proof")
        count, shard = report["shard_count"], report["shard"]
        if type(count) is not int or count < 1 or type(shard) is not int or not 0 <= shard < count:
            raise ValueError("Invalid shard identity")
        if shard in shard_ids or (shard_count is not None and count != shard_count):
            raise ValueError("Duplicate shard or inconsistent shard count")
        shard_ids.add(shard)
        shard_count = count
        expected = {row["module"] for row in assign_shards(list(modules.values()), count)[shard]}
        if {row["module"] for row in report["modules"]} != expected or len(report["modules"]) != len(expected):
            raise ValueError("Shard module membership differs from the frozen plan")
        if not report["test_sha256"] or (test_hashes is not None and report["test_sha256"] != test_hashes):
            raise ValueError("Shards used different or missing test fingerprints")
        test_hashes = report["test_sha256"]
        raw = report["exit_code_by_key"]
        consumed = set()
        for row in report["modules"]:
            path = row["module"]
            frozen = modules[path]
            if path in by_module or any(row.get(field) != value for field, value in frozen.items()):
                raise ValueError("Duplicate module or changed source/candidate fingerprint")
            prefix = path.removesuffix(".py").replace("/", ".") + "."
            selected = {key: code for key, code in raw.items() if key.startswith(prefix)}
            if len(selected) != frozen["candidates"] or candidate_hash(selected) != frozen["candidate_keys_sha256"]:
                raise ValueError("Raw candidate identities differ from the inventory")
            by_module[path] = selected
            consumed.update(selected)
        if consumed != set(raw) or codes.keys() & raw.keys():
            raise ValueError("Extra or duplicate candidate outcomes")
        codes.update(raw)
        verified &= (report["verified"] is True and report["runner_exit_code"] == 0
                     and report["measurement_exit_code"] == 0 and not report.get("error"))
        seconds += report["seconds"]

    candidate_modules = {key: path for path, values in by_module.items() for key in values}
    for key, decision in classifications.items():
        if key not in codes or key in invalid or outcome(codes[key]) != "survived":
            raise ValueError("Classifications must refer to measured, type-valid survivors")
        if decision.get("classification") not in {"real_gap", "equivalent", "untestable"} or not decision.get("reason", "").strip():
            raise ValueError("Survivor decisions need a classification and reason")
        if decision.get("source_sha256") != modules[candidate_modules[key]]["sha256"]:
            raise ValueError("Survivor decision is not bound to the frozen source")

    complete_union = set(by_module) == set(modules) and shard_ids == set(range(shard_count or 0))
    rows = []
    total: Counter = Counter()
    unclassified = []
    for path, frozen in modules.items():
        counts = Counter({category: 0 for category in CATEGORIES})
        counts.update(total=frozen["candidates"], no_tests=0, unclassified=0)
        if path not in by_module:
            counts["pending"] = frozen["candidates"]
        else:
            for key, code in by_module[path].items():
                category = "type_invalid" if key in invalid else outcome(code)
                if category == "survived":
                    decision = classifications.get(key)
                    if decision is None:
                        counts["unclassified"] += 1
                        unclassified.append(key)
                    elif decision["classification"] == "equivalent":
                        category = "equivalent"
                    if code in (5, 33):
                        counts["no_tests"] += 1
                counts[category] += 1
        denominator = counts["total"] - counts["equivalent"] - counts["type_invalid"]
        complete = verified and path in by_module and not (counts["pending"] or counts["inconclusive"])
        rows.append({**frozen, **counts, "complete": complete,
                     "score": counts["killed"] / denominator if complete and denominator else None})
        total.update(counts)
    complete = complete_union and verified and not (total["pending"] or total["inconclusive"])
    denominator = total["total"] - total["equivalent"] - total["type_invalid"]
    return {"schema": 1, "base": inventory["base"], "tool": inventory["tool"],
            "formula": "killed / (total - equivalent - type_invalid)",
            "overall": {**total, "complete": complete,
                        "score": total["killed"] / denominator if complete and denominator else None},
            "modules": rows, "unclassified_survivors": unclassified,
            "shards_received": sorted(shard_ids), "shards_expected": shard_count,
            "worker_seconds": seconds, "type_proof_sha256": proof_hash, "test_sha256": test_hashes,
            "limitations": ["Zero-candidate modules have an undefined score.",
                            "A raw score is not acceptance: survivor review and security-module thresholds are separate requirements."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--type-proof", required=True, type=Path)
    parser.add_argument("--classifications", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    inventory = load_inventory(args.inventory)
    invalid = read_type_exclusions(args.type_proof, inventory["modules"])
    decisions = json.loads(args.classifications.read_text()) if args.classifications else {}
    report = aggregate(inventory, [json.loads(path.read_text()) for path in args.reports], invalid,
                       file_hash(args.type_proof), decisions)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["overall"]))
    raise SystemExit(0 if report["overall"]["complete"] else 2)


if __name__ == "__main__":
    main()
