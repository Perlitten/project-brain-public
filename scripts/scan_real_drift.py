#!/usr/bin/env python3
"""Run real architectural drift scan against Project Brain codebase."""

from pathlib import Path
from brain.insights.drift_analyzer import scan_repository_drift

def main():
    repo_root = Path(__file__).resolve().parents[1]
    violations = scan_repository_drift(repo_root)
    print(f"Scanned {repo_root}. Total drift violations found: {len(violations)}")
    for v in violations:
        print(f"[{v.severity.upper()}] {v.file_path}:{v.line_number} - {v.rule_name}: {v.description}")

if __name__ == "__main__":
    main()
