"""Tracked source is plain UTF-8 text: no NUL bytes, no CRLF line endings.

A literal NUL in a .tsx file made Git, grep and the graph indexer treat the
file as binary (its imports were silently skipped).
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".cjs", ".mjs", ".css", ".md", ".json",
    ".yml", ".yaml", ".toml", ".sh", ".sql", ".html", ".txt", ".cfg", ".ini",
}


def _tracked_text_files() -> list[Path]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True).stdout
    paths = [ROOT / p for p in out.decode().split("\0") if p]
    return [p for p in paths if p.suffix in TEXT_SUFFIXES and p.is_file()]


def test_tracked_text_has_no_nul_or_crlf():
    bad = []
    for path in _tracked_text_files():
        data = path.read_bytes()
        if b"\0" in data:
            bad.append(f"{path.relative_to(ROOT)}: NUL byte")
        if b"\r\n" in data:
            bad.append(f"{path.relative_to(ROOT)}: CRLF line endings")
    assert not bad, "\n".join(bad)
