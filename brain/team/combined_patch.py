"""Combined-patch check for the integration queue (M3, docs/TEAM.md#team-coordination-m3).

Question answered: does a run's patch (``base..head``) still apply when the
patches already integrated on the same base are applied first? The check runs
in a throwaway bare repository with a temporary index whose object store
borrows (git alternates) from the repository checkout and the runs' working
copies. Nothing is written to any of them: no checkout, no ref, no merge, no
push. Git runs with ``--git-dir`` pointing at the throwaway repository, never
with a working directory inside a run's copy, and without system or global
configuration, external diff drivers or text conversion.

The result is ``clean`` (with the combined tree and a bounded patch for the
integrator), ``conflict`` (with the paths that do not apply), or
``unavailable`` (git or the objects could not be reached; callers fall back to
asking for a rebase).
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

SHA = re.compile(r"^[0-9a-f]{40}$")
MAX_PATCH_BYTES = 200_000  # the patch text returned to the integrator
MAX_DIFF_BYTES = 20_000_000  # a single applied diff; larger ones are not checked
MAX_FILES = 200
MAX_CONFLICTS = 50
GIT_TIMEOUT_SECONDS = 30.0
_CONFLICT_LINES = (re.compile(r"^error: patch failed: (.+):\d+$"),
                   re.compile(r"^error: (.+): (?:patch does not apply|already exists in index|"
                              r"does not exist in index|does not match index)$"))


@dataclass(frozen=True)
class Patch:
    """One run's change: ``base..head_sha``; ``key`` names it in the result (an entry id)."""
    key: str
    head_sha: str


class _Unavailable(Exception):
    pass


def object_store(path: str | os.PathLike | None) -> Path | None:
    """The object directory of a checkout, bare repository or linked worktree at ``path``, if readable."""
    if not path:
        return None
    root = Path(path)
    dot = root / ".git"
    try:
        if dot.is_file():  # a linked worktree or submodule: ``gitdir: <dir>``, objects in the common dir
            text = dot.read_text(encoding="utf-8", errors="replace").strip()
            if not text.startswith("gitdir:"):
                return None
            gitdir = Path(text[len("gitdir:"):].strip())
            gitdir = gitdir if gitdir.is_absolute() else (root / gitdir)
            common = gitdir / "commondir"
            if common.is_file():
                shared = Path(common.read_text(encoding="utf-8", errors="replace").strip())
                gitdir = shared if shared.is_absolute() else (gitdir / shared)
            candidates = [gitdir / "objects"]
        else:
            candidates = [dot / "objects", root / "objects"]
        for candidate in candidates:
            if candidate.is_dir():
                return candidate.resolve()
    except OSError:
        return None
    return None


def _env(index: Path) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", os.defpath), "GIT_INDEX_FILE": str(index),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
           "GIT_OPTIONAL_LOCKS": "0", "HOME": str(index.parent), "LANG": "C", "LC_ALL": "C"}
    for name in ("SYSTEMROOT", "WINDIR", "PATHEXT"):  # Windows basics for git to start
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def _run(git: str, args: list[str], env: dict[str, str], *, stdin: bytes | None = None
         ) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run([git, *args], env=env, input=stdin if stdin is not None else b"",
                              capture_output=True, timeout=GIT_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired as exc:
        raise _Unavailable("git timed out") from exc
    except OSError as exc:
        raise _Unavailable("git could not be run") from exc


def _ok(result: subprocess.CompletedProcess[bytes], what: str) -> bytes:
    if result.returncode != 0:
        raise _Unavailable(f"git {what} failed")
    return result.stdout


def conflict_paths(stderr: str) -> list[str]:
    """The paths named by ``git apply --check`` failures, in order, without duplicates."""
    paths: list[str] = []
    for line in stderr.splitlines():
        for pattern in _CONFLICT_LINES:
            match = pattern.match(line.strip())
            if match and match.group(1) not in paths:
                paths.append(match.group(1))
    return paths[:MAX_CONFLICTS]


def _numstat(raw: bytes) -> tuple[list[dict], int, int]:
    files: list[dict] = []
    added_total = deleted_total = 0
    for record in raw.decode("utf-8", "replace").split("\0"):
        parts = record.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        binary = added == "-" or deleted == "-"
        item = {"path": path, "added": None if binary else int(added), "deleted": None if binary else int(deleted)}
        if not binary:
            added_total += int(added)
            deleted_total += int(deleted)
        files.append(item)
    return files, added_total, deleted_total


def _result(status: str, base_sha: str, reason: str | None = None, **values) -> dict:
    return {"status": status, "reason": reason, "base_sha": base_sha, "applied": values.get("applied", []),
            "tree_sha": values.get("tree_sha"), "conflicts": values.get("conflicts", []),
            "files": values.get("files", []), "files_total": values.get("files_total", 0),
            "insertions": values.get("insertions", 0), "deletions": values.get("deletions", 0),
            "patch": values.get("patch"), "truncated": values.get("truncated", False)}


def combine(base_sha: str, integrated: Sequence[Patch], candidate: Patch, sources: Sequence[str | os.PathLike | None],
            *, git: str = "git") -> dict:
    """Apply ``integrated`` patches (oldest first), then ``candidate``, onto ``base_sha`` in a temporary index."""
    if not all(SHA.match(sha or "") for sha in (base_sha, candidate.head_sha, *(p.head_sha for p in integrated))):
        return _result("unavailable", base_sha, "A revision is not a full commit SHA")
    stores: list[Path] = []
    for source in sources:
        store = object_store(source)
        if store is not None and store not in stores:
            stores.append(store)
    if not stores:
        return _result("unavailable", base_sha, "No git object store is readable for this repository or its runs")
    try:
        with tempfile.TemporaryDirectory(prefix="brain-combine-") as tmp:
            work = Path(tmp)
            repo = work / "repo.git"
            env = _env(work / "index")
            _ok(_run(git, ["init", "--bare", "--quiet", "--template=", str(repo)], env), "init")
            info = repo / "objects" / "info"
            info.mkdir(parents=True, exist_ok=True)
            # Bytes, not text: a CR from Windows newline translation would end up in the path.
            (info / "alternates").write_bytes("".join(f"{store.as_posix()}\n" for store in stores).encode())
            base = ["--git-dir", str(repo)]
            for sha in dict.fromkeys((base_sha, *(p.head_sha for p in integrated), candidate.head_sha)):
                if _run(git, [*base, "cat-file", "-e", f"{sha}^{{commit}}"], env).returncode != 0:
                    raise _Unavailable(f"Commit {sha[:12]} is not in the repository or any run's working copy")
            _ok(_run(git, [*base, "read-tree", base_sha], env), "read-tree")

            def diff(head: str) -> bytes:
                out = _ok(_run(git, [*base, "diff", "--binary", "--full-index", "--no-renames", "--no-ext-diff",
                                     "--no-textconv", "--no-color", base_sha, head], env), "diff")
                if len(out) > MAX_DIFF_BYTES:
                    raise _Unavailable("A patch is too large to check")
                return out

            applied: list[str] = []
            for patch in integrated:
                text = diff(patch.head_sha)
                if text and _run(git, [*base, "apply", "--cached", "-"], env, stdin=text).returncode != 0:
                    raise _Unavailable(f"Patches already integrated on this base do not combine (entry {patch.key})")
                applied.append(patch.key)
            text = diff(candidate.head_sha)
            if text:
                check = _run(git, [*base, "apply", "--cached", "--check", "-"], env, stdin=text)
                if check.returncode != 0:
                    stderr = check.stderr.decode("utf-8", "replace")
                    conflicts = conflict_paths(stderr)
                    if not conflicts:
                        raise _Unavailable("git apply failed without naming a path")
                    return _result("conflict", base_sha, "The patch does not apply on top of the integrated patches",
                                   applied=applied, conflicts=conflicts)
                _ok(_run(git, [*base, "apply", "--cached", "-"], env, stdin=text), "apply")
            tree = _ok(_run(git, [*base, "write-tree"], env), "write-tree").decode().strip()
            files, insertions, deletions = _numstat(_ok(_run(git, [*base, "diff", "--numstat", "-z", "--no-renames",
                                                                   base_sha, tree], env), "diff --numstat"))
            shown = _ok(_run(git, [*base, "diff", "--no-renames", "--no-ext-diff", "--no-textconv", "--no-color",
                                   base_sha, tree], env), "diff")
            truncated = len(shown) > MAX_PATCH_BYTES
            if truncated:
                cut = shown.rfind(b"\n", 0, MAX_PATCH_BYTES)
                shown = shown[:cut + 1 if cut > 0 else MAX_PATCH_BYTES]
            return _result("clean", base_sha, applied=[*applied, candidate.key], tree_sha=tree,
                           files=files[:MAX_FILES], files_total=len(files), insertions=insertions,
                           deletions=deletions, patch=shown.decode("utf-8", "replace"),
                           truncated=truncated or len(files) > MAX_FILES)
    except _Unavailable as exc:
        return _result("unavailable", base_sha, str(exc))
    except OSError as exc:
        return _result("unavailable", base_sha, f"The temporary repository could not be prepared ({type(exc).__name__})")


def summary(result: dict) -> dict:
    """What goes into the activity feed: the result without the patch text and file list."""
    return {key: value for key, value in result.items() if key not in {"patch", "files"}}


__all__ = ["MAX_PATCH_BYTES", "Patch", "combine", "conflict_paths", "object_store", "summary"]
