"""Persist non-secret setup configuration into the checkout's ``.env``.

Only an explicit allowlist of keys may be written through this module —
provider credentials and tokens never pass through here; they stay in the
operator's own environment or the secret store, and are never echoed back.
"""
from __future__ import annotations

import os
import re
import stat
import tempfile
from io import StringIO
from pathlib import Path
from typing import Dict

from dotenv.parser import parse_stream

ALLOWED_SETUP_KEYS = frozenset(
    {
        "TARGET_REPO_PATH",
        "DEFAULT_LLM_PROVIDER",
        "DEFAULT_EMBEDDING_PROVIDER",
    }
)

SUPPORTED_SETUP_PROVIDERS = {
    "DEFAULT_LLM_PROVIDER": frozenset({"mock", "openai", "anthropic", "google", "nvidia"}),
    "DEFAULT_EMBEDDING_PROVIDER": frozenset({"mock", "openai", "google", "nvidia"}),
}


def _sanitize_value(key: str, value: str) -> str:
    if any(ch in value for ch in ("\n", "\r", "\x00")):
        raise ValueError(f"{key}: value must be a single line")
    if "${" in value:
        raise ValueError(f"{key}: environment interpolation is not supported")
    if key in SUPPORTED_SETUP_PROVIDERS and value not in SUPPORTED_SETUP_PROVIDERS[key]:
        raise ValueError(f"{key}: unsupported provider")
    return value


def _assignment(key: str, value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_./:-]+", value):
        value = "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return f"{key}={value}\n"


def update_env_file(env_path: Path, updates: Dict[str, str]) -> Dict[str, str]:
    """Write ``updates`` into ``env_path`` (created if absent), KEY=VALUE lines.

    Returns the applied allowlisted updates. Raises ``ValueError`` on a key
    outside :data:`ALLOWED_SETUP_KEYS` or an unsafe value.
    """
    applied: Dict[str, str] = {}
    for key, value in updates.items():
        if key not in ALLOWED_SETUP_KEYS:
            raise ValueError(f"{key} is not a writable setup key")
        applied[key] = _sanitize_value(key, value)
    if not applied:
        return applied

    if env_path.exists():
        with env_path.open(encoding="utf-8", newline="") as source:
            bindings = list(parse_stream(source))
        mode = stat.S_IMODE(env_path.stat().st_mode)
    else:
        bindings = list(parse_stream(StringIO("")))
        mode = 0o600
    remaining = dict(applied)
    out = []
    for binding in bindings:
        if binding.key is not None and binding.key in applied:
            if binding.key in remaining:
                out.append(_assignment(binding.key, remaining.pop(binding.key)))
        else:
            out.append(binding.original.string)
    if remaining and out and not out[-1].endswith(("\n", "\r")):
        out.append("\n")
    for key, value in remaining.items():
        out.append(_assignment(key, value))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=env_path.parent, delete=False,
        ) as target:
            temporary = Path(target.name)
            os.chmod(temporary, mode)
            target.write("".join(out))
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, env_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return applied
