"""dotenv file discovery for Settings (BRAIN_DISABLE_DOTENV / BRAIN_ENV_FILE / lite mode)."""
from __future__ import annotations

import os
from pathlib import Path


def _env_files() -> tuple[str, ...]:
    """dotenv files the settings load, lowest to highest precedence.

    The Brain install's own ``.env`` (next to pyproject.toml in a checkout) is
    the base layer so MCP clients launching the server from an unrelated
    working directory still find the install's configuration. The process
    CWD's ``.env`` then overrides it (backwards compatible), and an explicit
    ``BRAIN_ENV_FILE`` path wins over everything — the supported way to point
    an installed wheel at its configuration. ``BRAIN_DISABLE_DOTENV`` is the
    supported isolation switch for test harnesses that provide a complete
    process environment and must not read either checkout file."""
    if os.getenv("BRAIN_DISABLE_DOTENV", "").strip().lower() in {"1", "true", "yes", "on"}:
        return ()
    files: list[str] = []
    if os.getenv("BRAIN_STORAGE_MODE") == "lite":
        explicit = os.getenv("BRAIN_ENV_FILE", "").strip()
        return (explicit,) if explicit else ()
    package_env = Path(__file__).resolve().parents[3] / ".env"
    if package_env.is_file():
        files.append(str(package_env))
    files.append(".env")
    explicit = os.getenv("BRAIN_ENV_FILE", "").strip()
    if explicit:
        files.append(explicit)
    return tuple(files)
