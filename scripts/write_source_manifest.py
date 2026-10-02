#!/usr/bin/env python3
"""Create a deterministic content manifest for a non-Git source snapshot."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_NAME = ".brain-source-manifest.json"
CONTENT_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
IGNORED_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
    ".next",
}
IGNORED_FILES = {MANIFEST_NAME, ".env", ".mcp.json"}


def _iter_files(root: Path, includes: list[str] | None = None):
    roots = [root / value for value in includes] if includes else [root]
    for selected_root in sorted(roots):
        if selected_root.is_file():
            if not selected_root.is_symlink():
                yield selected_root
            continue
        if not selected_root.is_dir() or selected_root.is_symlink():
            continue
        for current, dirs, files in os.walk(selected_root):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS)
            current_path = Path(current)
            for name in sorted(files):
                if name in IGNORED_FILES or name.startswith(".env."):
                    continue
                path = current_path / name
                if path.is_symlink() or not path.is_file():
                    continue
                yield path


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_manifest(
    root: Path,
    source_revision: str | None = None,
    *,
    includes: list[str] | None = None,
) -> dict:
    aggregate = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    paths = sorted(
        _iter_files(root, includes),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in paths:
        relative = path.relative_to(root).as_posix()
        file_hash = _file_digest(path)
        aggregate.update(relative.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(file_hash.encode("ascii"))
        aggregate.update(b"\0")
        file_count += 1
        total_bytes += path.stat().st_size
    content_digest = aggregate.hexdigest()
    source_revision = (source_revision or "").strip() or None
    revision = (
        f"snapshot:{source_revision}:{content_digest}"
        if source_revision
        else f"manifest:{content_digest}"
    )
    return {
        "schema_version": 1,
        "revision": revision,
        "source_revision": source_revision,
        "content_digest": content_digest,
        "file_count": file_count,
        "total_bytes": total_bytes,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def validate_release_identity(
    manifest: dict,
    *,
    expected_content_digest: str | None,
    require_release_identity: bool,
) -> None:
    """Validate build arguments against the manifest computed from copied files."""
    source_revision = str(manifest.get("source_revision") or "").strip()
    content_digest = str(manifest.get("content_digest") or "").strip()
    expected = (expected_content_digest or "").strip()

    if require_release_identity and source_revision.lower() in {"", "unknown"}:
        raise ValueError("production source revision must be set and must not be unknown")
    if require_release_identity and CONTENT_DIGEST_RE.fullmatch(expected) is None:
        raise ValueError(
            "production expected content digest must be a 64-character SHA-256"
        )
    if expected:
        if CONTENT_DIGEST_RE.fullmatch(expected) is None:
            raise ValueError("expected content digest must be a 64-character SHA-256")
        if not hmac.compare_digest(content_digest.lower(), expected.lower()):
            raise ValueError(
                "expected content digest does not match the copied build inputs"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("repository")
    parser.add_argument("--source-revision")
    parser.add_argument("--output")
    parser.add_argument("--include", action="append")
    parser.add_argument("--expected-content-digest")
    parser.add_argument("--require-release-identity", action="store_true")
    args = parser.parse_args()

    root = Path(args.repository).resolve()
    if not root.is_dir():
        raise SystemExit(f"Repository is not a directory: {root}")
    output = Path(args.output).resolve() if args.output else root / MANIFEST_NAME
    payload = build_manifest(root, args.source_revision, includes=args.include)
    try:
        validate_release_identity(
            payload,
            expected_content_digest=args.expected_content_digest,
            require_release_identity=args.require_release_identity,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"{output}: revision={payload['revision']} "
        f"files={payload['file_count']} bytes={payload['total_bytes']}"
    )


if __name__ == "__main__":
    main()
