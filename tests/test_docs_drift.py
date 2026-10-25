"""CLAUDE.md must not reference paths that no longer exist."""
import subprocess
import sys
from pathlib import Path


def test_claude_md_paths_exist():
    script = Path(__file__).resolve().parents[1] / "scripts" / "check_docs_paths.py"
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
