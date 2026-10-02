"""Release identity read from the source manifest baked into the image."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


def read_baked_source_manifest(
    app_root: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    root = app_root or Path(__file__).resolve().parents[1]
    path = root / ".brain-source-manifest.json"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None
    source_revision = payload.get("source_revision")
    content_digest = payload.get("content_digest")
    if not isinstance(source_revision, str) or not source_revision.strip():
        return None
    if (
        not isinstance(content_digest, str)
        or _DIGEST_RE.fullmatch(content_digest) is None
    ):
        return None
    return payload
