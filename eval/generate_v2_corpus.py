#!/usr/bin/env python3
"""Generate a schema-v2 token economy corpus from the existing golden_set + lfm_extension.

For each of the 50 existing tasks, this script:
  1. Reads the expected files from the local codebase
  2. Extracts top-level symbols (classes, functions) via AST parsing
  3. Records their line ranges
  4. Identifies completion assertions (distinctive string literals / identifiers)
  5. Marks provenance.production_real = true (these are real codebase tasks)

Output: eval/token_economy_tasks_v2.json
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = PROJECT_ROOT / "eval"

CATEGORY_MAP = {
    # from token_economy_tasks.json categories
    "q001": "unknown_location", "q002": "unknown_location", "q003": "unknown_location",
    "q004": "unknown_location", "q006": "unknown_location", "q007": "unknown_location",
    "q008": "unknown_location", "q009": "unknown_location", "q010": "unknown_location",
    "q011": "unknown_location", "q012": "unknown_location", "q013": "unknown_location",
    "q014": "unknown_location", "q016": "unknown_location", "q017": "unknown_location",
    "q005": "known_symbol", "q015": "known_symbol", "q029": "known_symbol",
    "q037": "known_symbol", "q041": "known_symbol", "q044": "known_symbol",
    "q047": "known_symbol", "q049": "known_symbol", "q050": "known_symbol", "q033": "known_symbol",
    "q018": "cross_surface", "q019": "cross_surface", "q020": "cross_surface",
    "q034": "cross_surface", "q040": "cross_surface", "q043": "cross_surface",
    "q045": "cross_surface", "q046": "cross_surface", "q048": "cross_surface", "q035": "cross_surface",
    "q021": "architecture_memory", "q022": "architecture_memory",
    "q026": "architecture_memory", "q030": "architecture_memory", "q036": "architecture_memory",
    "q023": "ru_to_en", "q024": "ru_to_en", "q025": "ru_to_en", "q027": "ru_to_en", "q028": "ru_to_en",
    "q031": "stale_or_failure", "q032": "stale_or_failure", "q038": "stale_or_failure",
    "q039": "stale_or_failure", "q042": "stale_or_failure",
}


def _extract_symbols(file_path: Path) -> list[tuple[str, int, int]]:
    """Extract top-level classes and functions with their line ranges."""
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, OSError):
        return []
    symbols: list[tuple[str, int, int]] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            end_line = getattr(node, "end_lineno", node.lineno) or node.lineno
            symbols.append((node.name, node.lineno, end_line))
            # Also extract methods from classes
            if isinstance(node, ast.ClassDef):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        child_end = getattr(child, "end_lineno", child.lineno) or child.lineno
                        symbols.append((f"{node.name}.{child.name}", child.lineno, child_end))
    return symbols


def _extract_assertions(file_path: Path, max_count: int = 5) -> list[str]:
    """Extract distinctive string literals and identifiers from a file.

    Looks for:
    - String constants that look like config keys, error messages, or identifiers
    - Constant assignments (UPPER_CASE = ...)
    - Decorator names
    """
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, OSError):
        return []
    assertions: list[str] = []
    seen: set[str] = set()

    def _add(value: str) -> None:
        value = value.strip()
        if value and len(value) >= 4 and value not in seen and len(assertions) < max_count:
            seen.add(value)
            assertions.append(value)

    for node in ast.walk(tree):
        # Constant assignments: FOO = "bar" or FOO = 42
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    _add(target.id)
        # String constants that look like identifiers/messages
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            val = node.value.strip()
            if 4 <= len(val) <= 80 and not val.startswith("http"):
                # Skip generic strings
                if any(c.isalpha() for c in val) and not val.startswith(" "):
                    _add(val)
        # Decorators (on function/class defs)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Name):
                    _add(dec.id)
                elif isinstance(dec, ast.Attribute):
                    _add(dec.attr)
                elif isinstance(dec, ast.Call):
                    if isinstance(dec.func, ast.Name):
                        _add(dec.func.id)
                    elif isinstance(dec.func, ast.Attribute):
                        _add(dec.func.attr)

    return assertions


def _normalize_path(path: str) -> str:
    path = path.replace("\\", "/").strip()
    if path.startswith("/app/"):
        path = path[5:]
    return path.lstrip("/")


def _enrich_task(raw: dict, category: str) -> dict | None:
    task_id = raw.get("id", "")
    question = raw.get("question", "")
    expect_files = raw.get("expect_files", raw.get("expected_files", []))
    if not expect_files:
        return None

    expected_symbols: list[str] = []
    expected_ranges: list[list[int]] = []  # [path_idx, start, end] — but schema wants [path, start, end]
    expected_ranges_typed: list[list] = []  # [[path, start, end], ...]
    required_assertions: list[str] = []

    for rel_path in expect_files:
        norm = _normalize_path(rel_path)
        file_path = PROJECT_ROOT / norm
        if not file_path.is_file():
            # File doesn't exist locally — still include with empty enrichment
            continue
        symbols = _extract_symbols(file_path)
        for name, start, end in symbols[:4]:  # Top 4 symbols per file
            if name not in expected_symbols:
                expected_symbols.append(name)
            expected_ranges_typed.append([norm, start, end])
        assertions = _extract_assertions(file_path, max_count=3)
        for a in assertions:
            if a not in required_assertions:
                required_assertions.append(a)

    # Ensure at least one symbol, range, and assertion per task
    if not expected_symbols and expect_files:
        # Fallback: use the filename stem as a "symbol"
        first_file = _normalize_path(expect_files[0])
        stem = Path(first_file).stem
        expected_symbols.append(stem)
    if not expected_ranges_typed and expect_files:
        first_file = _normalize_path(expect_files[0])
        file_path = PROJECT_ROOT / first_file
        if file_path.is_file():
            try:
                lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
                expected_ranges_typed.append([first_file, 1, min(30, len(lines))])
            except OSError:
                pass
    if not required_assertions and expect_files:
        # Fallback: use a distinctive part of the question
        words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", question)
        if words:
            required_assertions.append(words[0])

    return {
        "id": task_id,
        "question": question,
        "expect_files": list(expect_files),
        "expect_symbols": expected_symbols[:6],
        "expect_ranges": expected_ranges_typed[:8],
        "required_assertions": required_assertions[:6],
        "category": category,
        "language": raw.get("language", "en"),
        "provenance": {
            "kind": "production_enriched",
            "source": f"Enriched from {raw.get('provenance', {}).get('source', 'curated baseline')}",
            "production_real": True,
            "original_provenance": raw.get("provenance", {}),
        },
    }


def main() -> int:
    golden = json.loads((EVAL_DIR / "golden_set.json").read_text(encoding="utf-8"))
    lfm = json.loads((EVAL_DIR / "lfm_eval_extension.json").read_text(encoding="utf-8"))
    all_tasks = list(golden) + list(lfm)

    # Verify we have 50 tasks
    if len(all_tasks) != 50:
        print(f"WARNING: Expected 50 tasks, got {len(all_tasks)}")

    # Verify category map covers all tasks
    missing_cats = [t["id"] for t in all_tasks if t["id"] not in CATEGORY_MAP]
    if missing_cats:
        print(f"ERROR: Missing categories for: {missing_cats}")
        return 1

    # Enrich each task
    enriched: list[dict] = []
    for raw in all_tasks:
        task_id = raw["id"]
        category = CATEGORY_MAP.get(task_id, "unknown_location")
        enriched_task = _enrich_task(raw, category)
        if enriched_task:
            enriched.append(enriched_task)
        else:
            print(f"WARNING: Could not enrich task {task_id}")

    if len(enriched) != 50:
        print(f"WARNING: Expected 50 enriched tasks, got {len(enriched)}")

    # Verify category split
    cat_counts: dict[str, int] = {}
    for t in enriched:
        cat_counts[t["category"]] = cat_counts.get(t["category"], 0) + 1
    expected_counts = {"unknown_location": 15, "known_symbol": 10, "cross_surface": 10, "architecture_memory": 5, "ru_to_en": 5, "stale_or_failure": 5}
    for cat, expected in expected_counts.items():
        actual = cat_counts.get(cat, 0)
        if actual != expected:
            print(f"WARNING: Category {cat}: expected {expected}, got {actual}")

    # Sort by id
    enriched.sort(key=lambda t: t["id"])

    manifest = {
        "schema_version": 2,
        "seed": 20260810,
        "description": "Schema-v2 production corpus: 50 real codebase tasks with expected files, symbols, line ranges, and completion assertions. Enriched from golden_set + lfm_extension with AST-extracted symbols and assertions from the actual codebase.",
        "tasks": enriched,
    }

    output_path = EVAL_DIR / "token_economy_tasks_v2.json"
    output_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Written {len(enriched)} tasks to {output_path}")

    # Print summary
    total_symbols = sum(len(t["expect_symbols"]) for t in enriched)
    total_ranges = sum(len(t["expect_ranges"]) for t in enriched)
    total_assertions = sum(len(t["required_assertions"]) for t in enriched)
    print(f"  Total symbols: {total_symbols}, ranges: {total_ranges}, assertions: {total_assertions}")
    print(f"  Categories: {cat_counts}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
