"""Verifiable identity for repositories delivered as file snapshots."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

SOURCE_MANIFEST_FILENAME = ".brain-source-manifest.json"
SOURCE_MANIFEST_SCHEMA_VERSION = 1
_HEX_RE = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)


def read_source_manifest(repository_path: Path) -> Optional[dict[str, Any]]:
    """Read a bounded source manifest without trusting arbitrary JSON shape."""
    path = repository_path / SOURCE_MANIFEST_FILENAME
    try:
        if not path.is_file() or path.stat().st_size > 64 * 1024:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != SOURCE_MANIFEST_SCHEMA_VERSION:
        return None
    revision = payload.get("revision")
    digest = payload.get("content_digest")
    if not isinstance(revision, str) or not revision.strip():
        return None
    if not isinstance(digest, str) or not _HEX_RE.fullmatch(digest):
        return None
    return payload


def source_manifest_revision(repository_path: Path) -> Optional[str]:
    manifest = read_source_manifest(repository_path)
    if manifest is None:
        return None
    return str(manifest["revision"])


def embedded_source_revision(revision: str | None) -> Optional[str]:
    """Return the source revision carried by a validated snapshot identity.

    Handles bare SHAs, snapshot:<rev>, and snapshot:<rev>:<digest>.
    """
    value = str(revision or "").strip()
    if not value:
        return None

    if value.startswith("snapshot:"):
        parts = value.split(":")
        if len(parts) >= 2 and parts[1]:
            return parts[1]
        return None

    return value


def revision_matches(expected: str | None, actual: str | None) -> bool:
    """Compare Git SHAs with bare or content-bound snapshot revisions safely.

    Prevents false mismatches between short/full Git SHAs and snapshot:<rev> prefixes.
    """
    expected_value = str(expected or "").strip()
    actual_value = str(actual or "").strip()
    if not expected_value or not actual_value:
        return False

    if expected_value == actual_value:
        return True

    if expected_value.startswith("snapshot:") and actual_value.startswith("snapshot:"):
        return expected_value == actual_value

    exp_rev = embedded_source_revision(expected_value) or expected_value
    act_rev = embedded_source_revision(actual_value) or actual_value

    if exp_rev == act_rev:
        return True

    # Check short-prefix match for Git commit SHAs (e.g. 7-char vs 40-char)
    if len(exp_rev) >= 7 and len(act_rev) >= 7:
        if exp_rev.startswith(act_rev) or act_rev.startswith(exp_rev):
            return True

    return False
