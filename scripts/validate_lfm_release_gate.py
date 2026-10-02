#!/usr/bin/env python3
"""Fail-closed production preflight for LFM release authorization."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path
from typing import Any, Mapping

# Run as ``python3 scripts/validate_lfm_release_gate.py`` on the host (no
# installed package, no PYTHONPATH), so make the repo's ``brain`` importable.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from brain.release_gate.approval import (  # noqa: E402
    HEX64_RE,
    MAX_EVIDENCE_BYTES,
    GateError,
    _read_bounded_json,
    release_binding_digest,
    validate_release_approval_payload,
)

# Re-exports: tests and brain.config.settings historically imported these
# from this script; the implementations now live in brain.release_gate.
__all__ = [
    "HEX64_RE",
    "MAX_EVIDENCE_BYTES",
    "GateError",
    "load_env_file",
    "parse_bool",
    "release_binding_digest",
    "validate_effective_compose_config",
    "validate_release_approval_payload",
    "validate_release_gate",
]

CRITICAL_KEYS = {
    "LATE_INTERACTION_ENABLED",
    "LATE_INTERACTION_DUAL_WRITE_ENABLED",
    "LATE_INTERACTION_SHADOW_ENABLED",
    "LATE_INTERACTION_RERANK_ENABLED",
    "LATE_INTERACTION_EXPERIMENT_AUTHORIZED",
    "LATE_INTERACTION_PRODUCTION_GATES_PASSED",
    "LATE_INTERACTION_REMOTE_ENABLED",
    "LATE_INTERACTION_CANARY_PERCENT",
    "LATE_INTERACTION_APPROVED_REPOSITORY_ID",
    "LATE_INTERACTION_APPROVED_BUILD_SHA",
    "LATE_INTERACTION_APPROVED_SOURCE_DIGEST",
    "LATE_INTERACTION_APPROVED_MODEL_REVISION",
    "LATE_INTERACTION_APPROVED_INDEX_REVISION",
    "LATE_INTERACTION_APPROVED_LINEAGE_ID",
    "LATE_INTERACTION_APPROVED_IDENTITY_DIGEST",
    "LATE_INTERACTION_APPROVED_DOCUMENT_COUNT",
    "LATE_INTERACTION_PRODUCTION_EVIDENCE_PATH",
    "LATE_INTERACTION_PRODUCTION_EVIDENCE_SHA256",
    "LATE_INTERACTION_REMOTE_MODEL_REVISION",
    "LATE_INTERACTION_MODEL_REVISION",
    "LATE_INTERACTION_REMOTE_EXPECTED_INDEX_REVISION",
}
BOOL_KEYS = {
    "LATE_INTERACTION_ENABLED",
    "LATE_INTERACTION_DUAL_WRITE_ENABLED",
    "LATE_INTERACTION_SHADOW_ENABLED",
    "LATE_INTERACTION_RERANK_ENABLED",
    "LATE_INTERACTION_EXPERIMENT_AUTHORIZED",
    "LATE_INTERACTION_PRODUCTION_GATES_PASSED",
    "LATE_INTERACTION_REMOTE_ENABLED",
}
TRUE_VALUES = {"true", "1", "yes", "on"}
FALSE_VALUES = {"false", "0", "no", "off", ""}
MAX_COMPOSE_CONFIG_BYTES = 4 * 1024 * 1024
RELEASE_APPROVAL_SCHEMA_VERSION = 1




def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    counts: dict[str, int] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        counts[key] = counts.get(key, 0) + 1
        values[key] = _unquote(value)
    duplicates = sorted(
        key for key, count in counts.items() if count > 1 and key in CRITICAL_KEYS
    )
    if duplicates:
        raise GateError(
            "duplicate LFM release-gate keys are not allowed: "
            + ", ".join(duplicates)
        )
    return values


def parse_bool(values: Mapping[str, str], key: str) -> bool:
    value = values.get(key, "").strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise GateError(f"{key} has invalid boolean value {value!r}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _compose_environment(service: Mapping[str, Any], service_name: str) -> dict[str, str]:
    raw = service.get("environment")
    if not isinstance(raw, dict):
        raise GateError(
            f"effective Compose service {service_name!r} has no resolved environment"
        )
    return {
        str(key): "" if value is None else str(value)
        for key, value in raw.items()
    }


def validate_effective_compose_config(
    path: Path,
    *,
    values: Mapping[str, str],
) -> None:
    """Reject Compose interpolation/override divergence for every LFM control."""
    if path.is_symlink() or not path.is_file():
        raise GateError("effective Compose config is missing or symlinked")
    payload = _read_bounded_json(
        path,
        maximum_bytes=MAX_COMPOSE_CONFIG_BYTES,
        label="effective Compose config",
    )
    services = payload.get("services")
    if not isinstance(services, dict):
        raise GateError("effective Compose config requires services")
    effective_by_service: dict[str, dict[str, str]] = {}
    for service_name in ("api", "worker"):
        service = services.get(service_name)
        if not isinstance(service, dict):
            raise GateError(
                f"effective Compose config is missing service {service_name!r}"
            )
        effective_by_service[service_name] = _compose_environment(
            service,
            service_name,
        )
    for key in sorted(CRITICAL_KEYS):
        expected = values.get(key, "")
        api_value = effective_by_service["api"].get(key, "")
        worker_value = effective_by_service["worker"].get(key, "")
        if api_value != expected or worker_value != expected:
            raise GateError(
                f"effective Compose override diverges for {key}: "
                f".env={expected!r}, api={api_value!r}, worker={worker_value!r}"
            )


def validate_release_gate(
    values: Mapping[str, str],
    *,
    build_sha: str,
    source_digest: str,
    reports_root: Path,
    effective_compose_path: Path | None = None,
) -> None:
    for key in BOOL_KEYS:
        parse_bool(values, key)
    if effective_compose_path is not None:
        validate_effective_compose_config(
            effective_compose_path,
            values=values,
        )

    try:
        canary_percent = float(values.get("LATE_INTERACTION_CANARY_PERCENT", "0"))
    except ValueError as exc:
        raise GateError("LATE_INTERACTION_CANARY_PERCENT must be numeric") from exc
    if not 0.0 <= canary_percent <= 100.0:
        raise GateError("LATE_INTERACTION_CANARY_PERCENT must be between 0 and 100")

    experiment_active = any(
        parse_bool(values, key)
        for key in (
            "LATE_INTERACTION_ENABLED",
            "LATE_INTERACTION_DUAL_WRITE_ENABLED",
            "LATE_INTERACTION_SHADOW_ENABLED",
        )
    )
    traffic_active = (
        parse_bool(values, "LATE_INTERACTION_RERANK_ENABLED")
        or canary_percent > 0
    )
    if experiment_active and not parse_bool(
        values, "LATE_INTERACTION_EXPERIMENT_AUTHORIZED"
    ):
        raise GateError(
            "LFM experiment flags require LATE_INTERACTION_EXPERIMENT_AUTHORIZED=true"
        )
    if traffic_active and not parse_bool(
        values, "LATE_INTERACTION_PRODUCTION_GATES_PASSED"
    ):
        raise GateError(
            "LFM traffic flags require LATE_INTERACTION_PRODUCTION_GATES_PASSED=true"
        )
    if not experiment_active and not traffic_active:
        return
    if not parse_bool(values, "LATE_INTERACTION_REMOTE_ENABLED"):
        raise GateError("active production LFM requires the authenticated remote service")

    build_sha = build_sha.strip()
    if build_sha == "unknown" or values.get("LATE_INTERACTION_APPROVED_BUILD_SHA") != build_sha:
        raise GateError(f"LFM authorization is not bound to build {build_sha}")
    source_digest = source_digest.strip()
    if (
        HEX64_RE.fullmatch(source_digest) is None
        or values.get("LATE_INTERACTION_APPROVED_SOURCE_DIGEST") != source_digest
    ):
        raise GateError("LFM authorization is not bound to the source digest")
    try:
        approved_repository_id = int(
            values.get("LATE_INTERACTION_APPROVED_REPOSITORY_ID", "")
        )
    except ValueError as exc:
        raise GateError("LFM approval requires a numeric repository id") from exc
    if approved_repository_id < 1:
        raise GateError("LFM approval requires a positive repository id")
    model_revision = values.get("LATE_INTERACTION_REMOTE_MODEL_REVISION", "")
    if (
        not model_revision
        or values.get("LATE_INTERACTION_APPROVED_MODEL_REVISION") != model_revision
    ):
        raise GateError("LFM authorization is not bound to the remote model revision")
    index_revision = values.get(
        "LATE_INTERACTION_REMOTE_EXPECTED_INDEX_REVISION", ""
    )
    if (
        re.fullmatch(r"r[0-9]+", index_revision) is None
        or values.get("LATE_INTERACTION_APPROVED_INDEX_REVISION") != index_revision
    ):
        raise GateError("LFM authorization is not bound to the expected index revision")
    lineage_id = values.get("LATE_INTERACTION_APPROVED_LINEAGE_ID", "").strip()
    if not lineage_id or len(lineage_id) > 128:
        raise GateError("LFM authorization requires an approved corpus lineage id")
    approved_identity_digest = values.get(
        "LATE_INTERACTION_APPROVED_IDENTITY_DIGEST", ""
    )
    if HEX64_RE.fullmatch(approved_identity_digest) is None:
        raise GateError("LFM authorization requires an approved corpus identity digest")
    try:
        approved_document_count = int(
            values.get("LATE_INTERACTION_APPROVED_DOCUMENT_COUNT", "")
        )
    except ValueError as exc:
        raise GateError("LFM approval requires a numeric document count") from exc
    if approved_document_count < 1:
        raise GateError("LFM approval requires a positive document count")
    if not traffic_active:
        return

    evidence_hash = values.get("LATE_INTERACTION_PRODUCTION_EVIDENCE_SHA256", "")
    if HEX64_RE.fullmatch(evidence_hash) is None:
        raise GateError("LFM traffic approval requires an evidence SHA-256")
    evidence_value = values.get("LATE_INTERACTION_PRODUCTION_EVIDENCE_PATH", "")
    if not evidence_value:
        raise GateError("LFM traffic approval requires an evidence bundle path")
    evidence_relative = Path(evidence_value)
    if evidence_relative.is_absolute():
        raise GateError("LFM evidence path must be relative to the reports directory")
    if ".." in evidence_relative.parts:
        raise GateError("LFM evidence path escapes the reports directory")
    reports_root = reports_root.resolve()
    unresolved_evidence_path = (reports_root / evidence_relative).absolute()
    try:
        relative_parts = unresolved_evidence_path.relative_to(reports_root).parts
    except ValueError as exc:
        raise GateError("LFM evidence path escapes the reports directory") from exc
    current_path = reports_root
    for part in relative_parts:
        current_path = current_path / part
        if current_path.is_symlink():
            raise GateError("LFM evidence path must not use symlinks")
    evidence_path = unresolved_evidence_path.resolve()
    try:
        evidence_path.relative_to(reports_root)
    except ValueError as exc:
        raise GateError("LFM evidence path escapes the reports directory") from exc
    if not evidence_path.is_file() or evidence_path.is_symlink():
        raise GateError("LFM production evidence bundle is missing or invalid")
    payload = _read_bounded_json(
        evidence_path,
        maximum_bytes=MAX_EVIDENCE_BYTES,
        label="LFM production evidence bundle",
        expected_sha256=evidence_hash,
    )
    validate_release_approval_payload(
        payload,
        expected_binding={
            "repository_id": approved_repository_id,
            "build_sha": build_sha,
            "source_digest": source_digest,
            "model_revision": model_revision,
            "index_revision": index_revision,
            "lineage_id": lineage_id,
            "identity_digest": approved_identity_digest,
            "document_count": approved_document_count,
        },
        reports_root=reports_root,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--build-sha", required=True)
    parser.add_argument("--source-digest", required=True)
    parser.add_argument("--reports-root", required=True, type=Path)
    parser.add_argument("--effective-compose-config", type=Path)
    args = parser.parse_args()
    try:
        values = load_env_file(args.env_file)
        validate_release_gate(
            values,
            build_sha=args.build_sha,
            source_digest=args.source_digest,
            reports_root=args.reports_root,
            effective_compose_path=args.effective_compose_config,
        )
    except (OSError, GateError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc


if __name__ == "__main__":
    main()
