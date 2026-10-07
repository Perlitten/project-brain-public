"""Deploy-time sanity check for the repository-path sandbox.

Run inside the API/worker container::

    python -m brain.config.repo_root_check

Prints the effective allowed roots, then validates every candidate repo path
the deployment is expected to serve — each ``repositories.path`` row plus every
immediate subdirectory of each ``ALLOWED_REPO_ROOTS`` entry (indexed mounts).
Emits ``FAIL <path>: <reason>`` for anything validate_secure_repo_path rejects
and exits 1 when any path fails.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

from brain.config.paths import allowed_repo_roots, validate_secure_repo_path
from brain.config.settings import settings


async def _candidate_paths() -> list[str]:
    candidates: set[str] = set()
    try:
        from brain.database.session import async_engine

        async with async_engine.connect() as conn:
            rows = (await conn.execute(text("SELECT path FROM repositories"))).all()
            candidates.update(str(row[0]) for row in rows)
    except Exception as exc:
        print(f"WARN: could not read repositories table: {type(exc).__name__}: {exc}")
    for root in str(getattr(settings, "ALLOWED_REPO_ROOTS", "") or "").split(","):
        root = root.strip()
        if not root:
            continue
        base = Path(root)
        if base.is_dir():
            candidates.update(str(child) for child in base.iterdir() if child.is_dir())
    return sorted(candidates)


async def _main() -> int:
    roots = allowed_repo_roots()
    print("ALLOWED_ROOTS=" + ",".join(str(base) for base in roots))
    # A configured-but-absent root means a mount is missing or
    # ALLOWED_REPO_ROOTS names a path the container does not have.
    for base in roots:
        if not base.is_dir():
            print(f"WARN: allowed root does not exist in this container: {base}")
    bad: list[tuple[str, str]] = []
    for candidate in await _candidate_paths():
        try:
            validate_secure_repo_path(candidate)
        except Exception as exc:
            bad.append((candidate, str(exc)))
    for candidate, message in bad:
        print(f"FAIL {candidate}: {message}")
    if not bad:
        print("OK: all candidate repository paths pass validate_secure_repo_path")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
