"""Create a new curated corpus revision for the Next.js migration.

The historical v2 corpus and saved measurements remain byte-for-byte intact.
Run on the reviewed source commit: python eval/refreeze_web_corpus.py --pin SHA
This records ground truth, not a retrieval experiment or production benefit.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "eval"


def definitions(source: str) -> dict[str, tuple[int, int]]:
    result = {}
    def visit(nodes, prefix=""):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                result[prefix + node.name] = (node.lineno, node.end_lineno)
                if isinstance(node, ast.ClassDef):
                    visit(node.body, prefix + node.name + ".")
    visit(ast.parse(source).body)
    return result


def symbol_range(file: str, symbol: str) -> list:
    source = (ROOT / file).read_text()
    if file.endswith(".py"):
        start, end = definitions(source)[symbol]
        # Include router decorators, which carry the endpoint path assertion.
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol.rsplit(".", 1)[-1]:
                start = min([start] + [d.lineno for d in node.decorator_list])
                break
    else:
        lines = source.splitlines()
        start = next(i + 1 for i, line in enumerate(lines) if re.search(rf"(?:function|const)\s+{re.escape(symbol)}\b", line))
        end = next(i + 1 for i in range(start, len(lines)) if re.fullmatch(r"};?", lines[i]))
    return [file, start, end]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pin", required=True)
    args = parser.parse_args()
    manifest = json.loads((EVAL / "token_economy_tasks_v2.json").read_text())
    old_pin = manifest["repository"]["pinned_commit"]
    originals = {}
    cases = {task["id"]: task for file in ("golden_set.json", "lfm_eval_extension.json")
             for task in json.loads((EVAL / file).read_text())}
    auth = [("apps/web/lib/web-auth.ts", "authorizeWebRequest"),
            ("apps/web/proxy.ts", "proxy"), ("apps/web/app/api/brain/[...path]/route.ts", "GET")]
    analytics = [("apps/api/routers/web.py", "web_overview"), ("apps/api/helpers.py", "get_db_counts"),
                 ("apps/web/lib/data.ts", "toRepository")]
    jobs = [("apps/web/lib/data.ts", "getJobs"), ("apps/api/routers/jobs.py", "get_job_status"),
            ("brain/workers/queue.py", "JobQueue.enqueue")]
    changes = {"q012": (auth, ["WEB_BASIC_AUTH", "timingSafeEqual", "authorizeWebRequest"]),
               "q032": (auth, ["WEB_BASIC_AUTH", "timingSafeEqual", "authorizeWebRequest"]),
               "q013": (analytics, ["repository_id", "freshness"]),
               "q014": (jobs, ["getJobs", "job_id", "enqueue"]),
               "q033": (jobs, ["getJobs", "job_id", "enqueue"])}
    for task in manifest["tasks"]:
        task_id = task["id"]
        if task_id in changes:
            symbols, assertions = changes[task_id]
            task.update(question=cases[task_id]["question"], expect_files=cases[task_id]["expect_files"],
                        expect_symbols=[symbol.rsplit(".", 1)[-1] for _, symbol in symbols],
                        expect_ranges=[symbol_range(file, symbol) for file, symbol in symbols],
                        required_assertions=assertions)
        elif task_id == "q048":
            css = "apps/web/app/globals.css"
            css_lines = (ROOT / css).read_text().splitlines()
            media = next(i + 1 for i, line in enumerate(css_lines) if "@media" in line)
            test = "apps/web/tests/regressions.test.cjs"
            test_lines = (ROOT / test).read_text().splitlines()
            test_start = next(i + 1 for i, line in enumerate(test_lines) if "every advertised navigation" in line)
            task.update(question=cases[task_id]["question"], expect_files=cases[task_id]["expect_files"],
                        expect_symbols=["Shell", "allNavItems"],
                        expect_ranges=[[css, media, min(media + 60, len(css_lines))],
                                       symbol_range("apps/web/components/Shell.tsx", "Shell"),
                                       [test, test_start, len(test_lines)]],
                        required_assertions=["@media", "useSearchParams", "existsSync"])
        else:
            # Source reads contain method identifiers, not dotted Class.method
            # spellings. File + range requirements retain the enclosing context.
            task["expect_symbols"] = list(dict.fromkeys(symbol.rsplit(".", 1)[-1] for symbol in task["expect_symbols"]))
            ranges = []
            for file, start, end in task["expect_ranges"]:
                if file.endswith(".py"):
                    if file not in originals:
                        source = subprocess.check_output(["git", "show", f"{old_pin}:{file}"], cwd=ROOT, text=True)
                        originals[file] = definitions(source)
                    name = next((name for name, span in originals[file].items() if span == (start, end)), None)
                    current = definitions((ROOT / file).read_text())
                    if name in current:
                        start, end = current[name]
                ranges.append([file, start, end])
            task["expect_ranges"] = ranges
        task["provenance"]["production_real"] = False
        task["provenance"]["corpus_revision"] = "web-migration-2026-10-02"
        texts = [(ROOT / file).read_text() for file in task["expect_files"]]
        assert all(any(symbol in text for text in texts) for symbol in task["expect_symbols"]), task_id
        assert all(1 <= start <= end <= len((ROOT / file).read_text().splitlines()) for file, start, end in task["expect_ranges"]), task_id
    manifest["repository"].update(pinned_commit=args.pin, pinned_ref="reviewed web-migration source")
    manifest["description"] = "Current Next.js migration revision of the 50 curated tasks; schema v2, not observed production traffic."
    manifest["historical_manifest"] = "eval/token_economy_tasks_v2.json"
    target = EVAL / "token_economy_tasks_web.json"
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    freeze = EVAL / "frozen_datasets.json"
    data = json.loads(freeze.read_text())
    for file in (target, EVAL / "golden_set.json", EVAL / "lfm_eval_extension.json"):
        data["files"][file.name] = hashlib.sha256(file.read_bytes()).hexdigest()
    freeze.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print("Refroze 50 curated tasks at", args.pin, "; historical v2 and holdout unchanged")


if __name__ == "__main__":
    main()
