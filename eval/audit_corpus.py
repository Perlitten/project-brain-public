#!/usr/bin/env python3
"""Static audit of a token-economy corpus against a repository tree.

Reports, per task and in aggregate: expected files missing at the tree,
symbols absent from the expected files, expected ranges that fall beyond EOF
(stale ground truth), and for each assertion whether it is literal
(substring-checkable) or behavioral (never scorable), and where each literal
sits relative to the expected ranges — inside them (reachable by a correct
locator) or outside (unreachable under the targeted-read contract).

Usage: python eval/audit_corpus.py --tasks <manifest> --root <repo> [--json out]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from token_economy_schema import is_literal_assertion, load_tasks


def audit(tasks_path: Path, root: Path) -> dict:
    tasks, corpus = load_tasks(tasks_path)
    per_task: dict[str, dict] = {}
    totals = Counter()
    for task in tasks:
        issues: list[str] = []
        missing_files = [f for f in task.expected_files if not (root / f).is_file()]
        if missing_files:
            issues.append(f"files_missing:{','.join(missing_files)}")
            totals["files_missing"] += len(missing_files)
        file_lines: dict[str, list[str]] = {}
        file_texts: dict[str, str] = {}
        for f in task.expected_files:
            path = root / f
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                file_lines[f] = text.splitlines()
                file_texts[f] = text
        missing_symbols = [
            s for s in task.expected_symbols
            if not any(s in line for lines in file_lines.values() for line in lines)
        ]
        if missing_symbols:
            issues.append(f"symbols_absent:{','.join(missing_symbols)}")
            totals["symbols_absent"] += len(missing_symbols)
        stale = 0
        range_text = ""
        for f, start, end in task.expected_ranges:
            lines = file_lines.get(f, [])
            if not lines:
                stale += 1
                continue
            if start > len(lines):
                stale += 1
            range_text += "\n".join(lines[start - 1:min(end, len(lines))]) + "\n"
        if stale:
            issues.append(f"ranges_beyond_eof:{stale}")
            totals["ranges_beyond_eof"] += stale
        literals = [a for a in task.required_assertions if is_literal_assertion(a, file_texts)]
        unscored = len(task.required_assertions) - len(literals)
        if unscored:
            totals["unscored_assertions"] += unscored
            issues.append(f"unscored_assertions:{unscored}")
        for a in literals:
            if a in range_text:
                totals["literal_in_expected_range"] += 1
            else:
                totals["literal_outside_expected_ranges"] += 1
        if issues:
            per_task[task.id] = {"issues": issues, "category": task.category}
    return {
        "tasks": len(tasks),
        "corpus": corpus,
        "totals": dict(totals),
        "task_issues": per_task,
        "tasks_with_issues": len(per_task),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    report = audit(args.tasks, args.root.resolve())
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(text + "\n", encoding="utf-8")
    totals = report["totals"]
    print(
        f"{report['tasks']} tasks; {report['tasks_with_issues']} with issues — "
        f"files_missing={totals.get('files_missing', 0)} "
        f"symbols_absent={totals.get('symbols_absent', 0)} "
        f"ranges_beyond_eof={totals.get('ranges_beyond_eof', 0)} "
        f"unscored={totals.get('unscored_assertions', 0)} "
        f"literal_in_range={totals.get('literal_in_expected_range', 0)} "
        f"literal_outside={totals.get('literal_outside_expected_ranges', 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
