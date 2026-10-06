"""Shared helpers for the external retrieval eval harness.

Importing this module sets eval env vars BEFORE brain modules load — keep the
os.environ block at the top. Protocol: ../PROTOCOL.md
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres_password@localhost:5433/brain_db_eval")
os.environ.setdefault("DEFAULT_EMBEDDING_PROVIDER", "openai_compatible")
os.environ.setdefault("EMBEDDING_BASE_URL", "http://127.0.0.1:18099/v1")
os.environ.setdefault("EMBEDDING_MODEL", "jina-code")
os.environ.setdefault("EMBEDDING_DIMENSION", "768")
os.environ.setdefault("EMBEDDING_API_KEY", "eval-local")
os.environ.setdefault("INDEX_EMBEDDING_BATCH_SIZE", "64")
os.environ.setdefault("DEFAULT_LLM_PROVIDER", "mock")
os.environ.setdefault("PYTHONHASHSEED", "0")

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

EVAL_DIR = Path(os.environ.get("EVAL_DIR", "/home/ubuntu/eval_external"))
REPOS_DIR = EVAL_DIR / "repos"
RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
SEED = 20261006

DEV_FOLD = {"django__django-11019", "sympy__sympy-13647", "psf__requests-2317"}

_TOKEN_RE = re.compile(r"\w+")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def parse_gold_patch(patch: str) -> tuple[list[str], dict[str, list[tuple[int, int]]]]:
    """Return (gold file list, {file: [(old_start, old_len), ...]}) from a unified diff."""
    files: list[str] = []
    hunks: dict[str, list[tuple[int, int]]] = {}
    cur: str | None = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                cur = parts[3]
                if cur.startswith("b/"):
                    cur = cur[2:]
                if cur not in files:
                    files.append(cur)
        elif line.startswith("@@") and cur:
            m = re.search(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                hunks.setdefault(cur, []).append((int(m.group(1)), int(m.group(2) or 1)))
    return files, hunks


def repo_local_name(gh_repo: str) -> str:
    return gh_repo.replace("/", "_")


def git(repo_dir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args], check=True, capture_output=True, text=True
    ).stdout


def commit_timestamp(repo_dir: Path, sha: str) -> int:
    return int(git(repo_dir, "show", "-s", "--format=%ct", sha).strip())


def grep_baseline(worktree: Path, keywords: list[str], top_k: int = 30) -> list[str]:
    """Plain-agent-grep: sum per-file match counts over issue keywords."""
    kws = [k for k in keywords if 0 < len(k) <= 200][:40]
    if not kws:
        return []
    args = ["rg", "-i", "-c", "--fixed-strings", "--no-messages"]
    for k in kws:
        args += ["-e", k]
    args.append(str(worktree))
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=120).stdout
    except Exception:
        return []
    scores: dict[str, int] = {}
    for line in out.splitlines():
        path, _, cnt = line.rpartition(":")
        if not path or not cnt.isdigit():
            continue
        rel = str(Path(path).relative_to(worktree)) if path.startswith(str(worktree)) else path
        scores[rel] = scores.get(rel, 0) + int(cnt)
    return [p for p, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]]


class JsonlWriter:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "a", buffering=1)

    def write(self, row: dict[str, Any]) -> None:
        self._fh.write(json.dumps(row, default=str) + "\n")

    def close(self) -> None:
        self._fh.close()


def done_ids(path: Path) -> set[str]:
    """Instance ids already present in a JSONL results file (for resume)."""
    if not path.exists():
        return set()
    ids = set()
    for line in path.read_text().splitlines():
        try:
            ids.add(json.loads(line)["instance_id"])
        except Exception:
            continue
    return ids


def perf_ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000.0, 2)
