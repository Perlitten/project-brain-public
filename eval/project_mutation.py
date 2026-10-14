"""Run and report deterministic shards of a frozen whole-package mutmut inventory.

Tooling only: install the locked application dev dependencies and mutmut==3.8.0.
Each shard needs its own disposable services and runs at most one mutant at a
time. No module is removed from the combined denominator by sharding.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL_VERSION = "3.8.0"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def candidate_hash(keys) -> str:
    return hashlib.sha256(json.dumps(sorted(keys), separators=(",", ":")).encode()).hexdigest()


def generate_inventory(workers: int) -> dict:
    if importlib.metadata.version("mutmut") != TOOL_VERSION:
        raise ValueError(f"Inventory generation requires mutmut=={TOOL_VERSION}")
    if (ROOT / "mutants").exists():
        raise ValueError("Preserve existing mutants/results outside the checkout before generating a fresh inventory")
    from mutmut.__main__ import create_mutants
    from mutmut.configuration import config
    from mutmut.utils.file_utils import copy_also_copy_files, copy_src_dir, walk_mutatable_files

    tracked = subprocess.check_output(["git", "ls-files", "brain", "apps", "scripts"], cwd=ROOT, text=True).splitlines()
    paths = sorted(path for path in tracked if path.endswith(".py"))
    cfg = config()
    if cfg.only_mutate or cfg.max_stack_depth != -1 or cfg.mutate_only_covered_lines:
        raise ValueError("Whole-package generation cannot use a scope/coverage/stack-depth filter")
    if {str(path) for path in walk_mutatable_files()} != set(paths):
        raise ValueError("Mutation configuration does not cover exactly the tracked production modules")
    copy_src_dir()
    copy_also_copy_files()
    stats = create_mutants(workers)
    rows = []
    for path in paths:
        data = json.loads((ROOT / "mutants" / (path + ".meta")).read_text(encoding="utf-8"))
        keys = data["exit_code_by_key"]
        rows.append({"module": path, "sha256": file_hash(ROOT / path),
                     "candidates": len(keys), "candidate_keys_sha256": candidate_hash(keys)})
    return {"base": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "tool": f"mutmut=={TOOL_VERSION}", "modules": rows, "module_count": len(rows),
            "candidate_count": sum(row["candidates"] for row in rows), "generation": vars(stats)}


def load_inventory(path: Path) -> dict:
    inventory = json.loads(path.read_text(encoding="utf-8"))
    modules = inventory["modules"]
    paths = [item["module"] for item in modules]
    if len(paths) != len(set(paths)):
        raise ValueError("Duplicate module in frozen inventory")
    tracked = subprocess.check_output(
        ["git", "ls-files", "brain", "apps", "scripts"], cwd=ROOT, text=True,
    ).splitlines()
    actual = {path for path in tracked if path.endswith(".py")}
    if set(paths) != actual:
        raise ValueError("Frozen inventory does not cover every tracked production Python module")
    for item in modules:
        if file_hash(ROOT / item["module"]) != item["sha256"]:
            raise ValueError(f"Source changed since inventory generation: {item['module']}")
        if type(item["candidates"]) is not int or item["candidates"] < 0:
            raise ValueError("Invalid candidate count")
    if inventory["candidate_count"] != sum(item["candidates"] for item in modules):
        raise ValueError("Inventory total does not match its modules")
    if inventory["module_count"] != len(modules) or inventory["tool"] != f"mutmut=={TOOL_VERSION}":
        raise ValueError("Inventory module count or tool version changed")
    return inventory


def assign_shards(modules: list[dict], count: int) -> list[list[dict]]:
    if count < 1:
        raise ValueError("Shard count must be positive")
    shards: list[list[dict]] = [[] for _ in range(count)]
    weights = [0] * count
    for item in sorted(modules, key=lambda row: (-row["candidates"], row["module"])):
        target = min(range(count), key=lambda index: (weights[index], len(shards[index]), index))
        shards[target].append(item)
        weights[target] += item["candidates"]
    return shards


def outcome(exit_code: int | None) -> str:
    if exit_code is None:
        return "pending"
    if type(exit_code) is not int:
        raise ValueError("Mutation exit codes must be integers or null")
    if exit_code == 1:
        return "killed"
    if exit_code in (0, 5, 33):
        return "survived"
    if exit_code in (36, -24, 24, 152, 255):
        return "timed_out"
    # A pytest internal error, crash, skip or type-check result needs review.
    # It cannot silently improve the detection score.
    return "inconclusive"


def summarize(modules: list[dict], directory: Path) -> dict:
    rows = []
    total: Counter[str] = Counter()
    results = {}
    for item in modules:
        path = directory / (item["module"] + ".meta")
        if path.exists():
            meta = json.loads(path.read_text(encoding="utf-8"))
            codes = meta["exit_code_by_key"]
            if len(codes) != item["candidates"]:
                raise ValueError(f"Candidate universe changed: {item['module']}")
            if candidate_hash(codes) != item["candidate_keys_sha256"]:
                raise ValueError(f"Candidate identities changed: {item['module']}")
            if results.keys() & codes.keys():
                raise ValueError("A candidate appears in multiple modules")
            counts = Counter(outcome(code) for code in codes.values())
            counts["no_tests"] = sum(code in (5, 33) for code in codes.values())
            results.update(codes)
        else:
            counts = Counter(pending=item["candidates"])
        counts.update(total=item["candidates"], equivalent=0, type_invalid=0)
        complete = not (counts["pending"] or counts["inconclusive"])
        denominator = item["candidates"]
        rows.append({**item, **counts, "complete": complete,
                     "score": counts["killed"] / denominator if complete and denominator else None})
        for key in ("killed", "survived", "timed_out", "pending", "inconclusive", "no_tests"):
            counts.setdefault(key, 0)
        total.update(counts)
    complete = not (total["pending"] or total["inconclusive"])
    return {"overall": {**total, "complete": complete,
                        "score": total["killed"] / total["total"] if complete and total["total"] else None},
            "modules": rows, "exit_code_by_key": results,
            "formula": "killed / (total - equivalent - type_invalid)"}


def run_shard(modules: list[dict]) -> None:
    if importlib.metadata.version("mutmut") != TOOL_VERSION:
        raise ValueError(f"This inventory requires mutmut=={TOOL_VERSION}")
    if os.getenv("NVIDIA_API_KEY"):
        raise ValueError("Unset provider credentials before starting the offline campaign")
    # This is tool configuration, not a production-source exclusion. The union
    # of the deterministic shards is checked against the full inventory.
    from mutmut.configuration import config
    from mutmut.__main__ import _run

    config().only_mutate = [item["module"] for item in modules]
    control = ROOT / ".mutation-results/stats-control.json"
    control.parent.mkdir(exist_ok=True)
    control.unlink(missing_ok=True)
    os.environ["BRAIN_MUTATION_STATS_CONTROL"] = str(control)
    try:
        _run((), max_children=1)
    except SystemExit:
        # mutmut refuses a stats run with zero associations. Keep all these
        # candidates in the denominator as uncovered; only accept a completed,
        # passing full clean suite, never a collection/tool failure.
        if not control.exists():
            raise
        proof = json.loads(control.read_text(encoding="utf-8"))
        if proof["exit_code"] != 0 or proof["collected"] < 1 or proof["associated_functions"] != 0:
            raise
        for item in modules:
            path = ROOT / "mutants" / (item["module"] + ".meta")
            data = json.loads(path.read_text(encoding="utf-8"))
            codes = data["exit_code_by_key"]
            if candidate_hash(codes) != item["candidate_keys_sha256"]:
                raise ValueError(f"Candidate identities changed: {item['module']}")
            data["exit_code_by_key"] = dict.fromkeys(codes, 33)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print("Completed clean suite with no associated tests; all shard candidates remain uncovered in the denominator")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("generate", "run", "report", "plan"))
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--shards", type=int, default=32)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.action == "generate":
        if args.workers < 1:
            parser.error("Worker count must be positive")
        args.output.write_text(json.dumps(generate_inventory(args.workers), indent=2) + "\n", encoding="utf-8")
        return
    if args.inventory is None:
        parser.error("--inventory is required for run, report and plan")
    inventory = load_inventory(args.inventory)
    shards = assign_shards(inventory["modules"], args.shards)
    if not 0 <= args.shard < len(shards):
        parser.error("Shard index must be in [0, shards)")
    if args.action == "plan":
        args.output.write_text(json.dumps({"base": inventory["base"], "shards": shards}, indent=2) + "\n")
        return
    started = datetime.now(timezone.utc).isoformat()
    timer = time.monotonic()
    exit_code = 0
    error = None
    try:
        if args.action == "run":
            run_shard(shards[args.shard])
    except SystemExit as exc:
        exit_code = int(exc.code or 0)
    except Exception as exc:
        exit_code = 1
        error = f"{type(exc).__name__}: {exc}"
    finally:
        report = summarize(shards[args.shard], ROOT / "mutants")
        verified = args.action == "run" and exit_code == 0
        if not verified:
            report["overall"]["complete"] = False
            report["overall"]["score"] = None
            for row in report["modules"]:
                row["complete"] = False
                row["score"] = None
        measurement_exit = exit_code or (0 if report["overall"]["complete"] else 2)
        report.update(schema=1, base=inventory["base"], tool=f"mutmut=={TOOL_VERSION}",
                      shard=args.shard, shard_count=args.shards, started_utc=started,
                      seconds=time.monotonic() - timer, runner_exit_code=exit_code, error=error,
                      verified=verified, measurement_exit_code=measurement_exit,
                      test_sha256={str(path.relative_to(ROOT / "mutants")): file_hash(path)
                                   for path in sorted((ROOT / "mutants/tests").rglob("*.py"))})
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report["overall"], sort_keys=True))
    sys.exit(measurement_exit)


if __name__ == "__main__":
    main()
