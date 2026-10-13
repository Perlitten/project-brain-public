"""Bounded, read-only working-tree evidence for an older repository index.

Diff diagnostics never authorize filesystem access. Reads resolve inside the
registered root, exclude generated/private material, reject escaping symlinks,
and discard files that change during the read. No Git command mutates the tree.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brain.search.filters import should_exclude_from_retrieval


@dataclass(frozen=True)
class SourceDelta:
    changed_files: tuple[str, ...] = ()
    head: str | None = None
    dirty: bool = False
    complete: bool = False
    source_present: bool = False
    reason: str | None = None

    def warning(self, max_bytes: int, *, reindex_queued: bool = False) -> dict[str, Any]:
        names = []
        used = 0
        for name in self.changed_files:
            cost = len(json.dumps(name, ensure_ascii=False).encode("utf-8")) + 1
            if used + cost > max(0, max_bytes):
                break
            names.append(name)
            used += cost
        if reindex_queued:
            message = "Indexed context may lag the working tree; background reindex requested."
        elif self.dirty:
            message = "Indexed context may lag the working tree; commit source changes before reindexing."
        else:
            message = "Indexed context may lag the working tree; automatic reindex was not queued."
        return {
            "message": message,
            "changed_files": names,
            "changed_files_truncated": len(names) < len(self.changed_files) or not self.complete,
            "working_tree_dirty": self.dirty,
            "diff_status": self.reason or "available",
        }


def collect_source_delta(root: Path, indexed_revision: str | None, *, limit: int, timeout: float) -> SourceDelta:
    root = root.resolve()
    if not root.is_dir():
        return SourceDelta(reason="source_unavailable")

    def git(*args: str):
        if args and args[0] == "diff":
            args = (args[0], "--no-ext-diff", "--no-textconv", *args[1:])
        try:
            command = ["git", "--no-pager", "-c", f"safe.directory={root}", "-c", "core.fsmonitor=false", *args]
            with subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
                assert process.stdout is not None
                with ThreadPoolExecutor(max_workers=1) as reader:
                    future = reader.submit(process.stdout.read, 65_537)
                    try:
                        raw = future.result(timeout=max(0.05, timeout))
                        if len(raw) > 65_536:
                            process.kill()
                            process.wait()
                            return None
                        process.wait(timeout=max(0.05, timeout))
                        return subprocess.CompletedProcess(command, process.returncode, raw.decode("utf-8", "replace"), "")
                    except (FutureTimeout, subprocess.TimeoutExpired):
                        process.kill()
                        process.wait()
                        return None
        except OSError:
            return None

    head_result = git("rev-parse", "HEAD")
    if head_result is None or head_result.returncode:
        return SourceDelta(source_present=True, reason="git_unavailable")
    head = head_result.stdout.strip()
    committed = None
    if indexed_revision and re.fullmatch(r"[0-9a-fA-F]{40,64}", indexed_revision):
        committed = git("diff", "--no-renames", "--name-only", "-z", f"{indexed_revision}..{head}", "--")
    working = git("diff", "--no-renames", "--name-only", "-z", "HEAD", "--")
    untracked = git("ls-files", "--others", "--exclude-standard", "-z")
    complete = all(result is not None and result.returncode == 0 for result in (committed, working, untracked))
    names = []
    dirty = False
    for result, is_working in ((committed, False), (working, True), (untracked, True)):
        if result is None or result.returncode:
            continue
        eligible = [name for name in result.stdout.split("\0") if name and not should_exclude_from_retrieval(name)]
        dirty = dirty or (is_working and bool(eligible))
        names.extend(eligible)
    ordered = tuple(dict.fromkeys(names))
    cap = max(1, min(limit, 512))
    return SourceDelta(
        changed_files=ordered[:cap], head=head, dirty=dirty,
        complete=complete and len(ordered) <= cap, source_present=True,
        reason=None if complete and len(ordered) <= cap else "diff_incomplete",
    )


@dataclass(frozen=True)
class LiveSource:
    lines: tuple[str, ...]
    digest: str
    truncated: bool


def read_live_source(root: Path, relative: str, max_bytes: int) -> LiveSource | None:
    if should_exclude_from_retrieval(relative):
        return None
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        return None
    root = root.resolve()
    try:
        requested = root / path
        resolved = requested.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return None
        # The requested name can be harmless while an in-repository symlink
        # resolves to .env, credentials or another excluded retrieval target.
        resolved_relative = resolved.relative_to(root).as_posix()
        if should_exclude_from_retrieval(resolved_relative):
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(resolved, flags)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            raw = stream.read(max(1, max_bytes) + 1)
            after = os.fstat(stream.fileno())
        final = (root / path).resolve(strict=True)
        current = final.stat()
        def identity(stat):
            return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns
        if final != resolved or identity(before) != identity(after) or identity(after) != identity(current):
            return None
        if b"\0" in raw:
            return None
        truncated = len(raw) > max(1, max_bytes)
        raw = raw[:max(1, max_bytes)]
        text = raw.decode("utf-8", errors="replace")
        return LiveSource(tuple(text.splitlines(keepends=True)), hashlib.sha256(raw).hexdigest(), truncated)
    except (OSError, ValueError):
        return None


def current_ranges(source: LiveSource, names: list[str]) -> list[tuple[int, int]]:
    """Find current symbol locations without trusting indexed line numbers."""
    wanted = {
        re.split(r"[.:#]+", name)[-1]
        for name in names
        if name and re.fullmatch(r"[A-Za-z_$][\w$]*(?:[.:#]+[A-Za-z_$][\w$]*)*", name)
    }
    ranges = []
    if wanted and not source.truncated:
        try:
            for node in ast.walk(ast.parse("".join(source.lines))):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in wanted:
                    ranges.append((node.lineno, node.end_lineno or node.lineno))
        except (SyntaxError, ValueError):
            pass
    total = len(source.lines)
    if not ranges and wanted:
        for line_number, line in enumerate(source.lines, start=1):
            for name in wanted:
                escaped = re.escape(name)
                declaration = re.search(
                    rf"\b(?:def|class|function|fn|interface|type|struct|enum|const|let|var)\s+{escaped}\b",
                    line,
                )
                go_method = re.search(rf"^\s*func\s+(?:\([^)]*\)\s*)?{escaped}\b", line)
                brace_method = re.search(
                    rf"^\s*(?:(?:export|public|private|protected|static|async|final|virtual|override)\s+)*"
                    rf"(?:[\w.<>,\[\]?]+\s+)*{escaped}\s*\([^)]*\)\s*(?::[^={{]+)?\s*{{",
                    line,
                )
                if declaration or go_method or brace_method:
                    ranges.append((max(1, line_number - 3), min(total, line_number + 40)))
    if not ranges and not names:
        ranges = [(1, min(80, total))]
    return list(dict.fromkeys((max(1, min(start, total)), min(total, max(start, end)))
                             for start, end in ranges if total and end >= 1))[:4]
