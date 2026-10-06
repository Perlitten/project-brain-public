"""Path helpers for portable repo-root resolution."""

from pathlib import Path
import tempfile
from typing import Optional

from brain.config.settings import settings


def get_repo_root() -> Path:
    """Return the configured repository root, resolved to an absolute path."""
    return Path(settings.REPO_ROOT).resolve()


def resolve_repo_path(repo: Optional[str | Path] = None) -> Path:
    """Resolve a repository path from CLI/API input or fall back to defaults."""
    if repo:
        return validate_secure_repo_path(repo)
    configured = get_repo_root()
    if configured.exists():
        return configured
    return Path.cwd().resolve()


def scratch_repo_roots_allowed() -> bool:
    """Whether cwd and the system tempdir count as allowed repo roots.

    Explicit ``ALLOW_SCRATCH_REPO_ROOTS`` wins; unset means allowed everywhere
    except ``ENVIRONMENT=production``, so a server never accepts
    ``repo_path=/tmp/...`` or paths under its working directory by accident.
    """
    explicit = getattr(settings, "ALLOW_SCRATCH_REPO_ROOTS", None)
    if explicit is not None:
        return bool(explicit)
    return str(getattr(settings, "ENVIRONMENT", "local")).lower() != "production"


def allowed_repo_roots() -> list[Path]:
    """Base directories validate_secure_repo_path accepts as repository roots."""
    allowed_bases = [
        get_repo_root(),
        Path(settings.TARGET_REPO_PATH).resolve(),
    ]
    if scratch_repo_roots_allowed():
        allowed_bases.append(Path.cwd().resolve())
        allowed_bases.append(Path(tempfile.gettempdir()).resolve())

    # Explicit user configured extra roots if present
    extra_roots = getattr(settings, "ALLOWED_REPO_ROOTS", None)
    if extra_roots:
        for r in str(extra_roots).split(","):
            if r.strip():
                allowed_bases.append(Path(r.strip()).resolve())

    return allowed_bases


def validate_secure_repo_path(repo_input: str | Path) -> Path:
    """Strictly validate repository path against traversal, symlink escape, and non-existent targets."""
    raw_str = str(repo_input).strip()
    if not raw_str:
        raise ValueError("Repository path cannot be empty")

    path_obj = Path(raw_str)

    # Symlink escape check
    try:
        resolved = path_obj.resolve(strict=True)
    except FileNotFoundError:
        raise ValueError(f"Repository path does not exist: {raw_str}")

    if not resolved.is_dir():
        raise ValueError(f"Repository path is not a directory: {raw_str}")

    is_inside = any(
        resolved == base or base in resolved.parents
        for base in allowed_repo_roots()
    )

    if not is_inside:
        raise ValueError(f"Access denied: Repository path '{resolved}' is outside allowed repository roots")

    return resolved


def reports_dir(repo_path: Optional[Path] = None) -> Path:
    """Return the reports directory for a repository."""
    if settings.REPORT_OUTPUT_DIR:
        return Path(settings.REPORT_OUTPUT_DIR).resolve()
    base = repo_path or get_repo_root()
    return base / "reports"


def context_packs_dir() -> Path:
    """Return the writable artifact store for generated context packs."""
    configured = settings.CONTEXT_PACK_OUTPUT_DIR
    if configured:
        return Path(configured).resolve()
    return get_repo_root() / "context_packs"
