"""Release-approval bundle validation for LFM production traffic.

Moved from scripts/validate_lfm_release_gate.py so the wheel installs the
validator — brain.config.settings imports it at module level and must not
depend on the scripts/ toolbox, which is not packaged.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping

HEX64_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
MAX_EVIDENCE_BYTES = 4 * 1024 * 1024
RELEASE_APPROVAL_SCHEMA = "project-brain.lfm-release-approval"
RELEASE_APPROVAL_SCHEMA_VERSION = 1
QUALITY_POLICY = "lfm-production-v1"
QUALITY_THRESHOLDS = {
    "median_ndcg_delta_min": 0.03,
    "improved_ratio_min": 0.60,
    "hit3_regression_max": 0.10,
    "slice_regression_max": 0.05,
}


class GateError(ValueError):
    """An unsafe or ambiguous production gate configuration."""


def _read_bounded_json(
    path: Path,
    *,
    maximum_bytes: int,
    label: str,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    size = path.stat().st_size
    if size < 1 or size > maximum_bytes:
        raise GateError(
            f"{label} must be between 1 and {maximum_bytes} bytes"
        )
    raw = path.read_bytes()
    if len(raw) != size:
        raise GateError(f"{label} changed while it was being read")
    if (
        expected_sha256 is not None
        and hashlib.sha256(raw).hexdigest().lower() != expected_sha256.lower()
    ):
        raise GateError(f"{label} SHA-256 does not match")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise GateError(f"{label} root must be a JSON object")
    return payload


def _required_object(payload: Mapping[str, Any], key: str, *, label: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise GateError(f"{label}.{key} must be an object")
    return value


def _required_true(payload: Mapping[str, Any], key: str, *, label: str) -> None:
    if payload.get(key) is not True:
        raise GateError(f"{label}.{key} must be exactly true")


def _required_int(
    payload: Mapping[str, Any],
    key: str,
    *,
    label: str,
    minimum: int = 1,
) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise GateError(f"{label}.{key} must be an integer >= {minimum}")
    return value


def _required_number(
    payload: Mapping[str, Any],
    key: str,
    *,
    label: str,
) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GateError(f"{label}.{key} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise GateError(f"{label}.{key} must be finite")
    return number


def _required_text(
    payload: Mapping[str, Any],
    key: str,
    *,
    label: str,
    maximum_length: int = 128,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise GateError(f"{label}.{key} must be a non-empty string")
    value = value.strip()
    if len(value) > maximum_length:
        raise GateError(
            f"{label}.{key} must not exceed {maximum_length} characters"
        )
    return value


def release_binding_digest(binding: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(binding),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _resolve_report_artifact(
    reports_root: Path,
    raw_path: object,
    *,
    label: str,
) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise GateError(f"{label}.evidence_path must be a relative path")
    relative = Path(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise GateError(f"{label}.evidence_path escapes the reports directory")
    reports_root = reports_root.resolve()
    unresolved = (reports_root / relative).absolute()
    try:
        parts = unresolved.relative_to(reports_root).parts
    except ValueError as exc:
        raise GateError(f"{label}.evidence_path escapes the reports directory") from exc
    current = reports_root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise GateError(f"{label}.evidence_path must not use symlinks")
    resolved = unresolved.resolve()
    try:
        resolved.relative_to(reports_root)
    except ValueError as exc:
        raise GateError(f"{label}.evidence_path escapes the reports directory") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise GateError(f"{label} evidence artifact is missing or invalid")
    return resolved


def _validate_gate_artifact(
    gate_name: str,
    gate: Mapping[str, Any],
    *,
    reports_root: Path,
    binding: Mapping[str, Any],
) -> None:
    label = f"release_approval.gates.{gate_name}"
    evidence_hash = str(gate.get("evidence_sha256", ""))
    artifact_path = _resolve_report_artifact(
        reports_root,
        gate.get("evidence_path"),
        label=label,
    )
    artifact = _read_bounded_json(
        artifact_path,
        maximum_bytes=MAX_EVIDENCE_BYTES,
        label=f"{gate_name} evidence artifact",
        expected_sha256=evidence_hash,
    )
    if artifact.get("release_binding") != dict(binding):
        raise GateError(f"{gate_name} evidence is not release/corpus bound")

    if gate_name == "quality":
        if artifact.get("schema_version") != 2:
            raise GateError("quality evidence requires report schema_version=2")
        overall = _required_object(artifact, "overall_gate", label="quality evidence")
        _required_true(overall, "passed", label="quality evidence.overall_gate")
        provenance = _required_object(
            artifact,
            "evaluation_provenance",
            label="quality evidence",
        )
        _required_true(provenance, "production_gate", label="quality evidence")
        _required_true(provenance, "fixed_thresholds", label="quality evidence")
        if provenance.get("policy") != QUALITY_POLICY:
            raise GateError("quality evidence has the wrong production policy")
        if provenance.get("thresholds") != QUALITY_THRESHOLDS:
            raise GateError("quality evidence has non-production thresholds")
        return

    if gate_name == "runtime_parity":
        if artifact.get("schema_version") != 1:
            raise GateError("runtime parity evidence requires schema_version=1")
        artifact_gate = _required_object(
            artifact,
            "gate",
            label="runtime parity evidence",
        )
        _required_true(artifact_gate, "passed", label="runtime parity evidence.gate")
        expected_thresholds = {
            "overlap_min": 0.90,
            "spearman_min": 0.95,
            "kendall_min": 0.90,
            "ndcg_abs_delta_max": 0.02,
        }
        if artifact_gate.get("thresholds") != expected_thresholds:
            raise GateError("runtime parity evidence has non-production thresholds")
        summary = _required_object(
            artifact,
            "summary",
            label="runtime parity evidence",
        )
        for key in (
            "count",
            "mean_top_k_overlap",
            "mean_spearman",
            "mean_kendall",
            "max_abs_ndcg_delta",
        ):
            section_key = "sample_count" if key == "count" else key
            if gate.get(section_key) != summary.get(key):
                raise GateError(
                    f"runtime parity approval does not match evidence summary {key}"
                )
        return

    expected_schema = {
        "runtime_latency_slo": "project-brain.lfm-runtime-latency",
        "production_shadow": "project-brain.lfm-production-shadow",
    }[gate_name]
    if (
        artifact.get("schema") != expected_schema
        or artifact.get("schema_version") != 1
    ):
        raise GateError(f"{gate_name} evidence has the wrong schema/version")
    _required_true(artifact, "passed", label=f"{gate_name} evidence")
    summary = _required_object(artifact, "summary", label=f"{gate_name} evidence")
    fields = (
        (
            "p95_end_to_end_increase_ms",
            "provider_failure_rate",
            "fail_open_verified",
            "sample_count",
        )
        if gate_name == "runtime_latency_slo"
        else (
            "production_real",
            "window_days",
            "successfully_recorded_eligible_queries",
            "scored_count",
            "skipped_count",
            "error_count",
        )
    )
    for key in fields:
        if gate.get(key) != summary.get(key):
            raise GateError(
                f"{gate_name} approval does not match evidence summary {key}"
            )


def validate_release_approval_payload(
    payload: Mapping[str, Any],
    *,
    expected_binding: Mapping[str, Any],
    reports_root: Path,
) -> None:
    """Validate the complete traffic approval bundle, not just offline quality.

    The paired quality report is necessary but cannot prove runtime latency,
    runtime parity, or real production shadow duration/volume. All four gates
    are therefore mandatory and bound to one immutable release/corpus identity.
    """
    if payload.get("schema") != RELEASE_APPROVAL_SCHEMA:
        raise GateError(
            f"release approval schema must be {RELEASE_APPROVAL_SCHEMA!r}"
        )
    if payload.get("schema_version") != RELEASE_APPROVAL_SCHEMA_VERSION:
        raise GateError(
            f"release approval schema_version must be {RELEASE_APPROVAL_SCHEMA_VERSION}"
        )
    _required_true(payload, "passed", label="release_approval")

    binding = _required_object(payload, "release_binding", label="release_approval")
    exact_integer_fields = {"repository_id", "document_count"}
    for field, expected in expected_binding.items():
        observed = binding.get(field)
        if field in exact_integer_fields:
            if isinstance(observed, bool) or not isinstance(observed, int):
                raise GateError(
                    f"release_approval.release_binding.{field} must be an integer"
                )
        elif not isinstance(observed, str):
            raise GateError(
                f"release_approval.release_binding.{field} must be a string"
            )
        if observed != expected:
            raise GateError(
                f"release approval is not bound to current {field}"
            )
    if set(binding) != set(expected_binding):
        raise GateError("release approval contains an unexpected binding shape")
    binding_sha256 = release_binding_digest(binding)

    gates = _required_object(payload, "gates", label="release_approval")
    required_gates = {
        "quality",
        "runtime_latency_slo",
        "runtime_parity",
        "production_shadow",
    }
    if set(gates) != required_gates:
        raise GateError(
            "release approval requires exactly quality, runtime_latency_slo, "
            "runtime_parity, and production_shadow gates"
        )
    for gate_name in sorted(required_gates):
        gate = _required_object(gates, gate_name, label="release_approval.gates")
        if gate.get("schema_version") != 1:
            raise GateError(
                f"release_approval.gates.{gate_name}.schema_version must be 1"
            )
        _required_true(
            gate,
            "passed",
            label=f"release_approval.gates.{gate_name}",
        )
        if gate.get("binding_sha256") != binding_sha256:
            raise GateError(
                f"release_approval.gates.{gate_name} is not bound to "
                "release_binding"
            )
        if HEX64_RE.fullmatch(str(gate.get("evidence_sha256", ""))) is None:
            raise GateError(
                f"release_approval.gates.{gate_name}.evidence_sha256 "
                "must be SHA-256"
            )
        _validate_gate_artifact(
            gate_name,
            gate,
            reports_root=reports_root,
            binding=binding,
        )

    quality = gates["quality"]
    if quality.get("report_schema_version") != 2:
        raise GateError("quality gate requires paired report schema_version=2")
    _required_true(quality, "overall_gate_passed", label="quality")
    _required_true(quality, "production_gate", label="quality")
    _required_true(quality, "fixed_thresholds", label="quality")
    if quality.get("policy") != QUALITY_POLICY:
        raise GateError(f"quality gate policy must be {QUALITY_POLICY!r}")
    if quality.get("thresholds") != QUALITY_THRESHOLDS:
        raise GateError("quality gate thresholds do not match the fixed policy")

    latency = gates["runtime_latency_slo"]
    p95_ms = _required_number(
        latency,
        "p95_end_to_end_increase_ms",
        label="runtime_latency_slo",
    )
    if p95_ms < 0 or p95_ms > 500.0:
        raise GateError(
            "runtime latency gate requires p95 end-to-end increase <= 500 ms"
        )
    failure_rate = _required_number(
        latency,
        "provider_failure_rate",
        label="runtime_latency_slo",
    )
    if failure_rate < 0 or failure_rate >= 0.001:
        raise GateError(
            "runtime latency gate requires provider failure rate < 0.1%"
        )
    _required_true(latency, "fail_open_verified", label="runtime_latency_slo")
    _required_int(
        latency,
        "sample_count",
        label="runtime_latency_slo",
        minimum=500,
    )

    parity = gates["runtime_parity"]
    _required_int(parity, "sample_count", label="runtime_parity")
    if _required_number(
        parity,
        "mean_top_k_overlap", label="runtime_parity"
    ) < 0.90:
        raise GateError("runtime parity mean top-10 overlap must be >= 0.90")
    if _required_number(parity, "mean_spearman", label="runtime_parity") < 0.95:
        raise GateError("runtime parity mean Spearman must be >= 0.95")
    if _required_number(parity, "mean_kendall", label="runtime_parity") < 0.90:
        raise GateError("runtime parity mean Kendall must be >= 0.90")
    if _required_number(
        parity,
        "max_abs_ndcg_delta", label="runtime_parity"
    ) > 0.02:
        raise GateError("runtime parity max absolute NDCG delta must be <= 0.02")

    shadow = gates["production_shadow"]
    _required_true(shadow, "production_real", label="production_shadow")
    window_days = _required_number(
        shadow,
        "window_days",
        label="production_shadow",
    )
    if window_days < 7.0:
        raise GateError("production shadow must cover at least 7 full days")
    _required_int(
        shadow,
        "successfully_recorded_eligible_queries",
        label="production_shadow",
        minimum=500,
    )
    scored = _required_int(
        shadow,
        "scored_count",
        label="production_shadow",
        minimum=0,
    )
    skipped = _required_int(
        shadow,
        "skipped_count",
        label="production_shadow",
        minimum=0,
    )
    errors = _required_int(
        shadow,
        "error_count",
        label="production_shadow",
        minimum=0,
    )
    if scored + skipped + errors < 500:
        raise GateError(
            "production shadow must account for at least 500 eligible queries"
        )
