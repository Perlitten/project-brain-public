"""Registry trust of an indexed repository, looked up by path.

The workspace registry (``.brain/workspace/repository_registry.json``) owns
repository *trust*; team membership owns *who* may use a repository.
``brain.team.authorization.authorize_repository`` combines the two and reads
the trust side through this module. Registry records name repositories by
canonical root, indexed repositories by ``repositories.path``; both are
compared in the registry's own identity form (resolved, forward slashes,
lower case), so the spelling a path was stored under does not matter.

A repository with no registry record carries no trust restriction: the
registry is optional and most indexed repositories are never registered.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from brain.workspace.models import RepositoryRecord, TrustLevel
from brain.workspace.path_sandbox import PathSandbox
from brain.workspace.registry_store import RegistryStore

# The same location the workspace, freshness, lab, ledger, operations and experiments routers use.
DEFAULT_REGISTRY_DIR = Path(".brain/workspace")


def load_registry_records(registry_dir: Path | None = None) -> list[RepositoryRecord]:
    """Every registry record; empty when no registry file exists.

    Never creates the registry directory (``RegistryStore`` would). Raises
    ``RegistryCorruptionError`` when the file fails its integrity check, so a
    caller enforcing trust can fail closed."""
    directory = registry_dir or DEFAULT_REGISTRY_DIR
    if not (directory / "repository_registry.json").is_file():
        return []
    return RegistryStore(directory).list_all()


def _identities(path: str) -> set[str]:
    spellings = {path, path.replace("\\", "/"), os.path.realpath(path)}
    return {PathSandbox.get_canonical_string(Path(spelling)) for spelling in spellings if spelling}


def records_for_path(records: Iterable[RepositoryRecord], path: str | None) -> list[RepositoryRecord]:
    """The registry records whose canonical root is ``path`` under any spelling."""
    if not path:
        return []
    wanted = _identities(path)
    return [record for record in records
            if record.normalized_root_identity in wanted or _identities(record.canonical_root) & wanted]


def trust_withdrawn(record: RepositoryRecord) -> bool:
    """Disabled, archived or trust level ``disabled``: the registry no longer trusts the repository."""
    return bool(record.disabled or record.archived or record.trust_level == TrustLevel.DISABLED.value)
