"""Portable repository-scope helpers for normative memory."""

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
    return normalized.rstrip("/") or "/"


def repository_scope_clause(
    model: Union[Type[Rule], Type[Decision]],
    repo_path: Optional[str],
):
    """Include global normative memory plus memory owned by one repository."""
    normalized_scope = normalize_repo_scope(repo_path)
    return or_(
        model.repo_path.is_(None),
        func.lower(func.trim(model.repo_path)) == (normalized_scope or "").casefold(),
    )
