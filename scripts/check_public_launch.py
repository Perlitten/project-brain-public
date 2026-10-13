"""Offline guard against broken public repository identities in launch surfaces."""

from pathlib import Path
import re
import subprocess
import sys


LEGACY_REPOSITORY = re.compile(r"\bperlitten/project-brain(?![A-Za-z0-9_-])", re.IGNORECASE)
PUBLIC_SUFFIXES = {".md", ".toml", ".yml", ".yaml", ".tsx", ".ts", ".json"}


def obsolete_references(root: Path, paths: list[str]) -> list[str]:
    """Inspect public docs/config/UI, not historical Python provenance fixtures."""
    findings = []
    for name in paths:
        path = root / name
        if path.suffix not in PUBLIC_SUFFIXES and not name.endswith(".example"):
            continue
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if LEGACY_REPOSITORY.search(line):
                findings.append(f"{name}:{number}: obsolete public repository identity")
    return findings


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True)
    paths = [name for name in result.stdout.decode().split("\0") if name]
    findings = obsolete_references(root, paths)
    if findings:
        print("\n".join(findings), file=sys.stderr)
        return 1
    print("Public repository identity check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
