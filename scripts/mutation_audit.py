"""Bounded, offline AST mutation campaign; never edits the working checkout.

Generate a manifest once, then reuse it to compare test suites. Results distinguish
test failures, collection/tool errors, timeouts and source sites removed by fixes.
This is deliberately not a repository-wide mutation score.
"""
from __future__ import annotations

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

SCOPE = {
    "brain/harness/attestation.py": {"calculate_hash", "verify_integrity", "verify_hmac_attestation"},
    "brain/harness/nonce.py": {"_elapsed_time", "__init__", "generate_nonce", "verify_and_consume"},
    "brain/harness/verifier.py": {"verify_claim", "_set_claim_status"},
    "brain/context/budget.py": {
        "utf8_bytes", "estimated_tokens", "truncate_utf8", "__post_init__", "remaining_bytes",
        "exclude", "add_text", "snapshot", "bounded_join",
    },
}
TESTS = [
    "tests/test_harness_attestation.py", "tests/test_property_and_mutation.py",
    "tests/test_adversarial_harness_suite.py", "tests/test_remote_observe_drill.py",
    "tests/test_context_budget.py", "tests/test_context_budget_evidence.py",
    "tests/test_fault_injection.py", "tests/test_chaos_v06.py",
]


def sites(source: str, functions: set[str]):
    """Stable identifiers bind each expression to its function and occurrence."""
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, ast.FunctionDef) or function.name not in functions:
            continue
        occurrences: dict[str, int] = {}
        for node in ast.walk(function):
            shape = ast.dump(node, include_attributes=False)
            occurrence = occurrences.get(shape, 0)
            occurrences[shape] = occurrence + 1
            yield function.name, shape, occurrence, node


def replacements(node: ast.AST, function: str) -> list[tuple[str, str]]:
    value = ast.unparse(node)
    if isinstance(node, ast.Return) and function == "truncate_utf8" and not isinstance(node.value, ast.Constant):
        return [("empty_prefix", 'return ""')]
    if isinstance(node, ast.Compare):
        results = [("negate_comparison", f"not ({value})")]
        swaps = {ast.Lt: "<=", ast.LtE: "<", ast.Gt: ">=", ast.GtE: ">"}
        if len(node.ops) == 1 and type(node.ops[0]) in swaps:
            results.append(("comparison_boundary", f"{ast.unparse(node.left)} {swaps[type(node.ops[0])]} {ast.unparse(node.comparators[0])}"))
        return results
    if isinstance(node, ast.BoolOp):
        separator = " or " if isinstance(node.op, ast.And) else " and "
        return [("boolean_operator", separator.join(f"({ast.unparse(v)})" for v in node.values))]
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Div)):
        operator = {ast.Add: "-", ast.Sub: "+", ast.Div: "*"}[type(node.op)]
        return [("arithmetic_operator", f"({ast.unparse(node.left)}) {operator} ({ast.unparse(node.right)})")]
    if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, bool):
        return [("boolean_return", f"return {not node.value.value}")]
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr == "startswith":
            return [("negate_prefix_check", f"not ({value})")]
        if ast.unparse(node.func) in {"time.time", "time.monotonic", "time.clock_gettime", "_elapsed_time"}:
            return [("freeze_clock", "0.0")]
        if isinstance(node.func, ast.Name) and node.func.id in {"min", "max"}:
            return [("limit_operator", f"{'max' if node.func.id == 'min' else 'min'}({', '.join(ast.unparse(a) for a in node.args)})")]
        if ast.unparse(node.func) == "math.ceil":
            return [("rounding", f"math.floor({ast.unparse(node.args[0])})")]
    if isinstance(node, ast.Delete) or (
        isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute) and node.value.func.attr in {"add", "append"}
    ):
        return [("remove_state_update", "pass")]
    return []


def replace(source: str, node: ast.AST, replacement: str) -> str:
    # AST columns count UTF-8 bytes, not Python characters.
    assert isinstance(node, (ast.expr, ast.stmt))
    assert node.end_lineno is not None and node.end_col_offset is not None
    lines = source.encode().splitlines(keepends=True)
    start = sum([len(line) for line in lines[:node.lineno - 1]], 0) + node.col_offset
    end = sum([len(line) for line in lines[:node.end_lineno - 1]], 0) + node.end_col_offset
    raw = source.encode()
    return (raw[:start] + replacement.encode() + raw[end:]).decode()


def manifest(root: Path) -> dict:
    mutations: list[dict] = []
    hashes = {}
    for relative, functions in SCOPE.items():
        source = (root / relative).read_text(encoding="utf-8")
        hashes[relative] = hashlib.sha256(source.encode()).hexdigest()
        for function, shape, occurrence, node in sites(source, functions):
            for operator, replacement in replacements(node, function):
                mutated = replace(source, node, replacement)
                compile(mutated, relative, "exec")
                mutations.append({
                    "id": len(mutations) + 1, "file": relative, "function": function,
                    "shape": shape, "occurrence": occurrence, "line": node.lineno,
                    "original": ast.get_source_segment(source, node),
                    "replacement": replacement, "operator": operator,
                })
    return {"scope": {p: sorted(f) for p, f in SCOPE.items()}, "source_sha256": hashes, "mutations": mutations}


def run(
    root: Path, frozen: dict, output: Path, extra_tests: list[str], workers: int,
    timeout: float, replay: bool = False,
) -> None:
    tested_hashes = {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in frozen["source_sha256"]}
    mismatches = [p for p, actual in tested_hashes.items() if actual != frozen["source_sha256"][p]]
    if mismatches and not replay:
        raise ValueError("Manifest source hashes differ; generate a matching manifest or use explicit --replay: " + ", ".join(mismatches))
    output.mkdir(parents=True, exist_ok=True)
    # Contract tests first and stop on the first failure. A broken truncator
    # can otherwise hang a later builder test after already failing its contract.
    tests = extra_tests + TESTS
    command = [sys.executable, "-m", "pytest", *tests, "-q", "-x", "--tb=short", "-p", "no:cacheprovider"]

    def worker(batch: list[dict]) -> list[dict]:
        with tempfile.TemporaryDirectory(prefix="brain-mutations-") as directory:
            sandbox = Path(directory)
            ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules", ".next", ".venv")
            for name in ("brain", "apps", "tests", "rules", "eval"):
                shutil.copytree(root / name, sandbox / name, ignore=ignore)
            shutil.copy(root / "pyproject.toml", sandbox / "pyproject.toml")
            env = dict(os.environ, PYTHONPATH=str(sandbox), PYTHONDONTWRITEBYTECODE="1")
            for key in list(env):
                if key.endswith("API_KEY") or key in {"LLM_API_KEY", "LLM_BASE_URL", "EMBEDDING_BASE_URL"}:
                    env[key] = ""
            env.update(DEFAULT_LLM_PROVIDER="mock", DEFAULT_EMBEDDING_PROVIDER="mock")
            baseline = subprocess.run(command, cwd=sandbox, env=env, capture_output=True, text=True, timeout=timeout)
            if baseline.returncode:
                raise RuntimeError(f"Unmutated test selection failed:\n{baseline.stdout[-8000:]}\n{baseline.stderr[-2000:]}")
            results = []
            for mutation in batch:
                path = sandbox / mutation["file"]
                source = path.read_text(encoding="utf-8")
                node = next((node for function, shape, occurrence, node in sites(source, SCOPE[mutation["file"]])
                             if (function, shape, occurrence) == (mutation["function"], mutation["shape"], mutation["occurrence"])), None)
                if node is None:
                    if not replay:
                        raise ValueError(f"Mutation site {mutation['id']} absent from validated source")
                    results.append({"id": mutation["id"], "status": "removed_by_source_change"})
                    continue
                start = time.monotonic()
                try:
                    path.write_text(replace(source, node, mutation["replacement"]), encoding="utf-8")
                    result = subprocess.run(command, cwd=sandbox, env=env, capture_output=True, text=True, timeout=timeout)
                    log = result.stdout + result.stderr
                    (output / f"{mutation['id']}.log").write_text(log, encoding="utf-8")
                    status = {0: "survived", 1: "killed", 2: "collection_error"}.get(result.returncode, "tool_error")
                    results.append({"id": mutation["id"], "status": status, "exit_code": result.returncode,
                                    "seconds": round(time.monotonic() - start, 3)})
                except subprocess.TimeoutExpired:
                    results.append({"id": mutation["id"], "status": "timeout"})
                finally:
                    path.write_text(source, encoding="utf-8")
            return results

    with ThreadPoolExecutor(max_workers=workers) as executor:
        batches = [frozen["mutations"][i::workers] for i in range(workers)]
        results = sorted([r for batch in executor.map(worker, batches) for r in batch], key=lambda r: r["id"])
    summary = {status: sum(r["status"] == status for r in results) for status in sorted({r["status"] for r in results})}
    record = {
        "tests": tests, "command": ["python", *command[1:]], "timeout_seconds": timeout,
        "results": results, "summary": summary, "replay": replay,
        "tested_source_sha256": tested_hashes, "source_mismatches": mismatches,
    }
    (output / "results.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--replay", action="store_true", help="Allow explicit cross-version replay and record source mismatches")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--extra-test", action="append", default=[])
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if args.generate:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest(args.root), indent=2) + "\n", encoding="utf-8")
    else:
        if args.output is None or args.workers < 1 or args.timeout <= 0:
            parser.error("run requires --output, positive --workers and --timeout")
        run(args.root, json.loads(args.manifest.read_text()), args.output, args.extra_test, args.workers, args.timeout, args.replay)


if __name__ == "__main__":
    main()
