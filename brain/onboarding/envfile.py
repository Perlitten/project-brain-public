"""Persist setup configuration into the checkout's ``.env``.

Only an explicit allowlist of keys may be written through this module. The two
universal provider keys (``LLM_API_KEY`` / ``EMBEDDING_API_KEY``) are writable
only when the caller opts in with ``allow_secrets=True`` (the scoped
``/api/setup/provider`` endpoint); they are write-only and never echoed back.
Every other credential stays in the operator's own environment.
"""
from __future__ import annotations

import os
import re
import stat
import tempfile
from io import StringIO
from pathlib import Path
from typing import Dict, FrozenSet
from urllib.parse import urlparse

from dotenv.parser import parse_stream

PROVIDER_SETUP_KEYS = frozenset(
    {
        "LLM_BASE_URL",
        "LLM_MODEL",
        "SUMMARIZER_MODEL",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSION",
    }
)
ALLOWED_SETUP_KEYS = frozenset(
    {
        "TARGET_REPO_PATH",
        "DEFAULT_LLM_PROVIDER",
        "DEFAULT_EMBEDDING_PROVIDER",
    }
) | PROVIDER_SETUP_KEYS
SECRET_SETUP_KEYS = frozenset({"LLM_API_KEY", "EMBEDDING_API_KEY"})
_URL_KEYS = frozenset({"LLM_BASE_URL", "EMBEDDING_BASE_URL"})

_OPENAI_COMPATIBLE_ALIASES = frozenset({"openai_compatible", "custom", "openai-compatible"})
SUPPORTED_SETUP_PROVIDERS = {
    "DEFAULT_LLM_PROVIDER": frozenset(
        {"mock", "anthropic", "google", "openai", "nvidia", "openrouter", "groq",
         "together", "deepseek", "mistral", "ollama", "lmstudio"}
    ) | _OPENAI_COMPATIBLE_ALIASES,
    # Only providers that serve an /embeddings endpoint.
    "DEFAULT_EMBEDDING_PROVIDER": frozenset(
        {"mock", "google", "openai", "nvidia", "together", "mistral", "ollama", "lmstudio"}
    ) | _OPENAI_COMPATIBLE_ALIASES,
}


def setup_env_path() -> Path:
    """Write the instance override that setup and generated MCP launches share."""
    explicit = os.environ.get("BRAIN_ENV_FILE", "").strip()
    return Path(explicit).expanduser() if explicit else Path(".env")


def _sanitize_value(key: str, value: str) -> str:
    if any(ch in value for ch in ("\n", "\r", "\x00")):
        raise ValueError(f"{key}: value must be a single line")
    if "${" in value:
        raise ValueError(f"{key}: environment interpolation is not supported")
    if key in SUPPORTED_SETUP_PROVIDERS and value not in SUPPORTED_SETUP_PROVIDERS[key]:
        raise ValueError(f"{key}: unsupported provider")
    if key in _URL_KEYS and value:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"{key}: must be an http(s) URL such as https://api.openai.com/v1")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(f"{key}: put credentials in the API key, not in the URL")
    if key == "EMBEDDING_DIMENSION" and value and not (value.isdigit() and 0 < int(value) <= 65536):
        raise ValueError(f"{key}: must be a positive whole number")
    if (key in SECRET_SETUP_KEYS or key.endswith("_MODEL")) and any(ch.isspace() for ch in value):
        raise ValueError(f"{key}: must not contain spaces")
    return value


def _assignment(key: str, value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_./:-]+", value):
        value = "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return f"{key}={value}\n"


def update_env_file(
    env_path: Path,
    updates: Dict[str, str],
    *,
    allow_secrets: bool = False,
    extra_keys: FrozenSet[str] = frozenset(),
) -> Dict[str, str]:
    """Write ``updates`` into ``env_path`` (created if absent), KEY=VALUE lines.

    Returns the applied allowlisted updates (callers must not echo secret
    values). Raises ``ValueError`` on a key outside :data:`ALLOWED_SETUP_KEYS`
    (plus :data:`SECRET_SETUP_KEYS` when ``allow_secrets``, plus the caller's
    own validated ``extra_keys``) or an unsafe value.
    """
    allowed = ALLOWED_SETUP_KEYS | SECRET_SETUP_KEYS if allow_secrets else ALLOWED_SETUP_KEYS
    allowed = allowed | extra_keys
    applied: Dict[str, str] = {}
    for key, value in updates.items():
        if key not in allowed:
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
