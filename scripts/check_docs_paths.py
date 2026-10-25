"""Docs-drift check: every repo path referenced in CLAUDE.md must exist.

CLAUDE.md is the agent navigation map — when modules move, stale paths make
the map lie. Run: python scripts/check_docs_paths.py  (exit 1 on stale refs).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "CLAUDE.md"
PATH_RE = re.compile(r"`((?:brain|apps|tests|docs|scripts|rules|deploy)/[A-Za-z0-9_./#-]+?)`")


def iter_refs() -> list[str]:
    refs = []
    for match in PATH_RE.finditer(DOC.read_text(encoding="utf-8")):
        path = match.group(1).split("#")[0].rstrip("/")
        if path:
            refs.append(path)
    return sorted(set(refs))


def main() -> int:
    missing = [p for p in iter_refs() if not (ROOT / p).exists()]
    for p in missing:
        print(f"stale CLAUDE.md reference: {p}")
    if missing:
        print(f"{len(missing)} stale path(s) in CLAUDE.md")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
