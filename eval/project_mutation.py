"""Run and report deterministic shards of a frozen whole-package mutmut inventory.

Tooling only: install the locked application dev dependencies and mutmut==3.8.0.
Each shard needs its own disposable services and runs at most one mutant at a
time. No module is removed from the combined denominator by sharding.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.metadata
import json
import os
import signal
import subprocess
import sys
import sysconfig
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL_VERSION = "3.8.0"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def candidate_hash(keys) -> str:
    return hashlib.sha256(json.dumps(sorted(keys), separators=(",", ":")).encode()).hexdigest()


def prepare_git_pointer(root: Path) -> None:
    """Let the generated tree read history without copying read-only packfiles."""
    git_dir = subprocess.check_output(["git", "rev-parse", "--absolute-git-dir"], cwd=root, text=True).strip()
    target = root / "mutants/.git"
    target.parent.mkdir(exist_ok=True)
    content = f"gitdir: {git_dir}\n"
    if target.is_dir() or target.is_symlink():
        raise ValueError("Preserve the existing mutants Git directory outside the checkout and start a fresh sandbox")
    if target.exists() and target.read_text(encoding="utf-8") != content:
        raise ValueError("Refusing to replace unrelated Git metadata in the mutation sandbox")
    target.write_text(content, encoding="utf-8")


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
    prepare_git_pointer(ROOT)
    stats = create_mutants(workers)
    rows = []
    for path in paths:
        data = json.loads((ROOT / "mutants" / (path + ".meta")).read_text(encoding="utf-8"))
        keys = data["exit_code_by_key"]
        rows.append({"module": path, "sha256": file_hash(ROOT / path),
                     "candidates": len(keys), "candidate_keys_sha256": candidate_hash(keys),
                     "generated_sha256": file_hash(ROOT / "mutants" / path)})
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


def summarize(modules: list[dict], directory: Path, invalid: dict | None = None) -> dict:
    invalid = invalid or {}
    rows = []
    total: Counter[str] = Counter()
    results = {}
    for item in modules:
        path = directory / (item["module"] + ".meta")
        if path.exists():
            if file_hash(directory / item["module"]) != item["generated_sha256"]:
                raise ValueError(f"Generated source changed: {item['module']}")
            meta = json.loads(path.read_text(encoding="utf-8"))
            codes = meta["exit_code_by_key"]
            if len(codes) != item["candidates"]:
                raise ValueError(f"Candidate universe changed: {item['module']}")
            if candidate_hash(codes) != item["candidate_keys_sha256"]:
                raise ValueError(f"Candidate identities changed: {item['module']}")
            if results.keys() & codes.keys():
                raise ValueError("A candidate appears in multiple modules")
            counts = Counter(outcome(code) for key, code in codes.items() if key not in invalid)
            counts["type_invalid"] = sum(key in invalid for key in codes)
            counts["no_tests"] = sum(code in (5, 33) for key, code in codes.items() if key not in invalid)
            results.update(codes)
        else:
            counts = Counter(pending=item["candidates"])
        counts.update(total=item["candidates"], equivalent=0)
        counts.setdefault("type_invalid", 0)
        complete = not (counts["pending"] or counts["inconclusive"])
        denominator = item["candidates"] - counts["type_invalid"]
        for key in ("killed", "survived", "timed_out", "pending", "inconclusive", "no_tests"):
            counts.setdefault(key, 0)
        rows.append({**item, **counts, "complete": complete,
                     "score": counts["killed"] / denominator if complete and denominator else None})
        total.update(counts)
    complete = not (total["pending"] or total["inconclusive"])
    denominator = total["total"] - total["type_invalid"]
    return {"overall": {**total, "complete": complete,
                        "score": total["killed"] / denominator if complete and denominator else None},
            "modules": rows, "exit_code_by_key": results,
            "formula": "killed / (total - equivalent - type_invalid)"}


@contextmanager
def subprocess_guard():
    """Keep the guard active when CLI tests replace PYTHONPATH.

    The temporary startup hook acts only when campaign environment variables
    are set. Normal provider execution in the same venv stays unaffected.
    """
    if sys.prefix == sys.base_prefix:
        raise ValueError("Run the campaign in a dedicated virtual environment")
    path = Path(sysconfig.get_path("purelib")) / "brain-mutation-session.pth"
    content = ("import os,sys; os.getenv('BRAIN_MUTATION_CONFIG_ROOT') and "
               "sys.path.insert(0, os.environ['BRAIN_MUTATION_SUPPORT_PATH'])\n")
    existed = path.exists()
    if existed and path.read_text(encoding="utf-8") != content:
        raise ValueError("Refusing to overwrite an existing Python startup hook")
    if not existed:
        path.write_text(content, encoding="utf-8")
    try:
        yield
    finally:
        if not existed:
            path.unlink(missing_ok=True)


@subprocess_guard()
def run_shard(modules: list[dict], invalid: dict | None = None) -> None:
    invalid = invalid or {}
    if importlib.metadata.version("mutmut") != TOOL_VERSION:
        raise ValueError(f"This inventory requires mutmut=={TOOL_VERSION}")
    if os.getenv("NVIDIA_API_KEY"):
        raise ValueError("Unset provider credentials before starting the offline campaign")
    # This is tool configuration, not a production-source exclusion. The union
    # of the deterministic shards is checked against the full inventory.
    from mutmut.configuration import config
    from mutmut.__main__ import _run

    config().only_mutate = [item["module"] for item in modules]
    prepare_git_pointer(ROOT)
    inputs = {str(path.relative_to(ROOT)): file_hash(path)
              for base in ("brain", "apps", "scripts", "tests") for path in sorted((ROOT / base).rglob("*.py"))
              if "node_modules" not in path.parts}
    for path in [ROOT / "pyproject.toml", Path(__file__), *(ROOT / "eval/mutation_support").glob("*.py"),
                 *ROOT.glob("requirements*.lock"), *ROOT.glob("eval/*requirements.lock")]:
        label = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else "campaign-runner"
        inputs[label] = file_hash(path)
    context = {"inputs": inputs, "modules": sorted(item["module"] for item in modules),
               "python": sys.version, "versions": sorted([dist.metadata["Name"], dist.version]
                                                         for dist in importlib.metadata.distributions())}
    context_file = ROOT / "mutants/campaign-context.json"
    try:
        reset_outcomes = json.loads(context_file.read_text()) != context
    except (FileNotFoundError, json.JSONDecodeError):
        reset_outcomes = True
    # A file copied raw when another shard was selected can have a newer mtime
    # than its source. mutmut mistakes that for generated code. Force generation
    # of this selection once; unchanged candidate outcomes can still be resumed.
    for item in modules:
        target = ROOT / "mutants" / item["module"]
        if target.exists():
            source = (ROOT / item["module"]).stat()
            os.utime(target, ns=(source.st_atime_ns, source.st_mtime_ns))
    # mutmut's incremental cache can retain test durations while losing all
    # associations after a scope change. A baseline always recollects the full
    # suite; candidate outcome metadata remains available for exact-head resume.
    (ROOT / "mutants/mutmut-stats.json").unlink(missing_ok=True)
    os.environ["BRAIN_MUTATION_CONFIG_ROOT"] = str(ROOT)
    os.environ["BRAIN_MUTATION_SUPPORT_PATH"] = str(ROOT / "eval/mutation_support")
    child_stats = ROOT / ".mutation-results/subprocess-stats"
    child_stats.mkdir(exist_ok=True, parents=True)
    for path in child_stats.glob("*.json"):
        path.unlink()
    os.environ["BRAIN_MUTATION_CHILD_STATS"] = str(child_stats)
    control = ROOT / ".mutation-results/stats-control.json"
    control.parent.mkdir(exist_ok=True)
    control.unlink(missing_ok=True)
    os.environ["BRAIN_MUTATION_STATS_CONTROL"] = str(control)
    if modules:
        from mutmut.__main__ import create_mutants
        from mutmut.utils.file_utils import copy_also_copy_files, copy_src_dir

        copy_src_dir()
        copy_also_copy_files()
        create_mutants(1)
        for item in modules:
            if "generated_sha256" in item and file_hash(ROOT / "mutants" / item["module"]) != item["generated_sha256"]:
                raise ValueError(f"Generated source changed: {item['module']}")
            path = ROOT / "mutants" / (item["module"] + ".meta")
            data = json.loads(path.read_text(encoding="utf-8"))
            if "candidate_keys_sha256" in item and candidate_hash(data["exit_code_by_key"]) != item["candidate_keys_sha256"]:
                raise ValueError(f"Candidate identities changed: {item['module']}")
            for key in data["exit_code_by_key"]:
                if key in invalid:
                    data["exit_code_by_key"][key] = 37
                    data["type_check_error_by_key"][key] = json.dumps(invalid[key], sort_keys=True)
                elif reset_outcomes:
                    data["exit_code_by_key"][key] = None
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    context_file.write_text(json.dumps(context, sort_keys=True) + "\n", encoding="utf-8")
    try:
        _run((), max_children=1)
    except SystemExit:
        # mutmut refuses a stats run with zero associations. Keep all these
        # candidates in the denominator as uncovered; only accept a completed,
        # passing full clean suite, never a collection/tool failure.
        if not control.exists():
            raise
        proof = json.loads(control.read_text(encoding="utf-8"))
        if (proof.get("phase") != "stats" or proof["exit_code"] != 0
                or proof["collected"] < 1 or proof["associated_functions"] != 0):
            raise
        for item in modules:
            path = ROOT / "mutants" / (item["module"] + ".meta")
            data = json.loads(path.read_text(encoding="utf-8"))
            codes = data["exit_code_by_key"]
            if candidate_hash(codes) != item["candidate_keys_sha256"]:
                raise ValueError(f"Candidate identities changed: {item['module']}")
            data["exit_code_by_key"] = {key: 37 if key in invalid else 33 for key in codes}
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print("Completed clean suite with no associated tests; all shard candidates remain uncovered in the denominator")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("generate", "types", "run", "report", "plan"))
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--type-proof", type=Path, default=ROOT / "docs/testing/evidence/project-mutation-type-proof.json.gz")
    parser.add_argument("--diagnostics", type=Path)
    parser.add_argument("--generated-root", type=Path, default=ROOT / "mutants")
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
    if args.action == "types":
        if args.diagnostics is None:
            parser.error("--diagnostics is required for types")
        from eval.mutation_types import build_type_proof

        proof = build_type_proof(inventory, args.generated_root, args.diagnostics)
        args.output.write_bytes(gzip.compress((json.dumps(proof, indent=2) + "\n").encode(), mtime=0))
        return
    shards = assign_shards(inventory["modules"], args.shards)
    if not 0 <= args.shard < len(shards):
        parser.error("Shard index must be in [0, shards)")
    if args.action == "plan":
        args.output.write_text(json.dumps({"base": inventory["base"], "shards": shards}, indent=2) + "\n")
        return
    from eval.mutation_types import read_type_exclusions

    invalid = read_type_exclusions(args.type_proof, shards[args.shard])
    started = datetime.now(timezone.utc).isoformat()
    timer = time.monotonic()
    exit_code = 0
    error = None
    verified = False
    interrupted = False
    previous_handler = None

    def mark_interrupted(signum, frame):
        nonlocal interrupted
        interrupted = True
        raise KeyboardInterrupt

    try:
        if args.action == "run":
            previous_handler = signal.signal(signal.SIGINT, mark_interrupted)
            run_shard(shards[args.shard], invalid)
            # mutmut catches Ctrl-C while draining workers. Completed/cached
            # outcomes must not make that interrupted verification look clean.
            if interrupted:
                raise KeyboardInterrupt
            verified = True
    except KeyboardInterrupt:
        exit_code = 130
        error = "KeyboardInterrupt: mutation measurement interrupted"
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        error = "Mutation runner exited before verification completed"
        control = ROOT / ".mutation-results/stats-control.json"
        if exit_code and control.exists():
            error = "Mutation runner control failure: " + control.read_text(encoding="utf-8").strip()
    except Exception as exc:
        exit_code = 1
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if previous_handler is not None:
            signal.signal(signal.SIGINT, previous_handler)
        report = summarize(shards[args.shard], ROOT / "mutants", invalid)
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
                      type_proof_sha256=file_hash(args.type_proof),
                      test_sha256={str(path.relative_to(ROOT / "mutants")): file_hash(path)
                                   for path in sorted((ROOT / "mutants/tests").rglob("*.py"))})
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report["overall"], sort_keys=True))
    sys.exit(measurement_exit)


if __name__ == "__main__":
    main()
