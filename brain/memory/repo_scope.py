"""Portable repository-scope helpers for normative memory."""

import ntpath
import os
from typing import Optional, Type, Union

from sqlalchemy import func, or_

from brain.database.models import Decision, Rule


def normalize_repo_scope(repo_path: Optional[str]) -> Optional[str]:
    """Normalize a repository identity without resolving host-specific mounts."""
    if repo_path is None:
        return None
    normalized = repo_path.strip().replace("\\", "/")
    if not normalized:
        return None
    if normalized.lower() == "system_global":
        return None
    normalized = normalized.rstrip("/") or "/"
    # POSIX paths are case-sensitive. Windows drive/UNC identities retain
    # their case-insensitive semantics even when queried from a Linux server.
    if os.name == "nt" or ntpath.splitdrive(normalized)[0]:
        normalized = ntpath.normcase(normalized).replace("\\", "/")
    return normalized


def scope_equals(column, repo_path: Optional[str]):
    normalized = normalize_repo_scope(repo_path)
    if normalized is None:
        return or_(column.is_(None), column == "system_global")
    if os.name == "nt" or ntpath.splitdrive(normalized)[0]:
        return func.lower(func.trim(column)) == normalized
    return func.trim(column) == normalized


def repository_scope_clause(
    model: Union[Type[Rule], Type[Decision]],
    repo_path: Optional[str],
):
    """Include global normative memory plus memory owned by one repository."""
    normalized_scope = normalize_repo_scope(repo_path)
    return or_(
        model.repo_path.is_(None),
        model.repo_path == "system_global",
        scope_equals(model.repo_path, normalized_scope),
    )
