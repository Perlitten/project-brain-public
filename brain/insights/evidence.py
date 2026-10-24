"""Read code only from file evidence that carries an explicit repository identity."""
from __future__ import annotations

from pathlib import PurePosixPath


def code_target(evidence: object) -> tuple[int, str, int] | None:
    if not isinstance(evidence, list):
        return None
    entries = [item for item in evidence[:100] if isinstance(item, dict)]
    labels = {item.get("label"): item.get("value") for item in entries if isinstance(item.get("label"), str)}
    candidates = [*entries, labels]
    for item in candidates:
        repository_id = item.get("repository_id")
        path = item.get("file") or item.get("file_path")
        if type(repository_id) is not int or not 0 < repository_id <= 2_147_483_647 or not isinstance(path, str):
            continue
        path = path.replace("\\", "/")
        if not path or len(path) > 1024 or any(ord(char) < 32 for char in path) or ":" in path:
            continue
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or ".." in parsed.parts or not parsed.parts:
            continue
        line = item.get("line") or item.get("line_number")
        if type(line) is int and not 0 < line <= 2_147_483_000:
            continue
        return repository_id, parsed.as_posix(), line if type(line) is int else 1
    return None
