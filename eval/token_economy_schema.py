"""Strict corpus schema for Project Brain's full task-loop acceptance benchmark.

Schema v1 remains readable only for historical, non-acceptance baselines. A
release candidate must use schema v2 with explicit production provenance and
observable file, symbol, range and completion assertions for every task.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_PATH = PROJECT_ROOT / "eval" / "token_economy_tasks_web.json"



@dataclass(frozen=True)
class TokenEconomyTask:
    id: str
    question: str
    expected_files: tuple[str, ...]
    expected_symbols: tuple[str, ...]
    expected_ranges: tuple[tuple[str, int, int], ...]
    required_assertions: tuple[str, ...]
    category: str
    language: str
    provenance: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["expected_files"] = list(self.expected_files)
        data["expected_symbols"] = list(self.expected_symbols)
        data["expected_ranges"] = [list(item) for item in self.expected_ranges]
        data["required_assertions"] = list(self.required_assertions)
        return data


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> dict[str, Any]:
    if path.name == DEFAULT_MANIFEST_PATH.name:
        freeze = _read_json(PROJECT_ROOT / "eval" / "frozen_datasets.json")
        expected = freeze.get("files", {}).get(path.name)
        if not expected or sha256_file(path) != expected:
            raise ValueError("Current web corpus digest does not match its frozen recording")
    manifest = _read_json(path)
    if manifest.get("schema_version") not in {1, 2}:
        raise ValueError("Unsupported token economy manifest schema")
    return manifest


def _ranges(raw: Any) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        (str(item[0]), int(item[1]), int(item[2]))
        for item in raw or []
        if isinstance(item, list) and len(item) == 3
    )


def _task_from_raw(raw: dict[str, Any], category: str) -> TokenEconomyTask:
    return TokenEconomyTask(
        id=str(raw.get("id") or ""),
        question=str(raw.get("question") or ""),
        expected_files=tuple(str(item) for item in raw.get("expect_files", raw.get("expected_files", [])) if item),
        expected_symbols=tuple(str(item) for item in raw.get("expect_symbols", raw.get("expected_symbols", [])) if item),
        expected_ranges=_ranges(raw.get("expect_ranges", raw.get("expected_ranges", []))),
        required_assertions=tuple(str(item) for item in raw.get("required_assertions", []) if item),
        category=category,
        language=str(raw.get("language") or "unknown"),
        provenance=dict(raw.get("provenance") or {}),
    )


def is_literal_assertion(
    assertion: str, file_texts: dict[str, str] | None = None
) -> bool:
    """Whether an assertion is checkable as a verbatim substring of the
    expected files at the evaluated revision.

    With ``file_texts`` (required for measurement) this is an evidence
    check: the string must occur verbatim in an expected file. Without it,
    only a bounded non-sentence string is treated as plausibly literal —
    used for manifest lint where tree contents are unavailable. Assertions
    that are natural-language prose or stale literals are never scored as
    retrieval coverage."""
    if not assertion.strip():
        return False
    if file_texts is not None:
        return any(assertion in text for text in file_texts.values())
    stripped = assertion.strip()
    return (
        0 < len(stripped) <= 100
        and "\n" not in stripped
        and not stripped.endswith((".", "?", "!"))
    )


def _validate_split(tasks: list[TokenEconomyTask], declared: dict[str, Any] | None) -> dict[str, int]:
    if not declared:
        return {}
    required = {str(k): int(v) for k, v in declared.items()}
    observed = {name: sum(task.category == name for task in tasks) for name in required}
    declared_total = sum(required.values())
    if len(tasks) != declared_total or observed != required:
        raise ValueError(f"Expected the declared {declared_total}-task category split, got {len(tasks)} / {observed}")
    return observed


def _legacy_tasks(manifest: dict[str, Any]) -> tuple[list[TokenEconomyTask], dict[str, str]]:
    if not isinstance(manifest.get("sources"), list) or not manifest["sources"]:
        raise ValueError("Token economy manifest must list source fixtures")
    category_by_id = {
        task_id: category
        for category, task_ids in manifest.get("categories", {}).items()
        for task_id in task_ids
    }
    source_hashes: dict[str, str] = {}
    tasks: list[TokenEconomyTask] = []
    seen: set[str] = set()
    for source in manifest["sources"]:
        source_path = PROJECT_ROOT / str(source)
        if not source_path.is_file():
            raise ValueError(f"Missing token economy fixture: {source_path}")
        source_hashes[str(source)] = sha256_file(source_path)
        raw_tasks = _read_json(source_path)
        if not isinstance(raw_tasks, list):
            raise ValueError(f"Fixture must be a list: {source_path}")
        for raw in raw_tasks:
            if not isinstance(raw, dict):
                continue
            task = _task_from_raw(raw, str(category_by_id.get(str(raw.get("id") or "")) or ""))
            if not task.id or task.id in seen:
                raise ValueError(f"Duplicate or missing task id: {task.id!r}")
            if not task.category:
                raise ValueError(f"Task {task.id} has no token-economy category")
            seen.add(task.id)
            tasks.append(task)
    return tasks, source_hashes


def _production_tasks(manifest: dict[str, Any]) -> list[TokenEconomyTask]:
    raw_tasks = manifest.get("tasks")
    if not isinstance(raw_tasks, list):
        raise ValueError("Schema v2 manifest must contain an explicit tasks list")
    tasks = [_task_from_raw(raw, str(raw.get("category") or "")) for raw in raw_tasks if isinstance(raw, dict)]
    ids = [task.id for task in tasks]
    if not all(ids) or len(set(ids)) != len(ids):
        raise ValueError("Schema v2 manifest contains duplicate or missing task ids")
    return tasks


def _production_claim_invalid(task: TokenEconomyTask) -> bool:
    """``production_real`` may only be claimed by tasks whose origin is a
    real, observed user task — never by curated questions, no matter how
    much enrichment was layered on afterwards."""
    if task.provenance.get("production_real") is not True:
        return False
    if task.provenance.get("kind") != "production_observed":
        return True
    original = task.provenance.get("original_provenance") or {}
    if original and original.get("production_real") is not True:
        return True
    return not bool(task.provenance.get("observed_task_evidence"))


def _acceptance_reasons(tasks: list[TokenEconomyTask], schema_version: int) -> list[str]:
    reasons: list[str] = []
    if schema_version != 2:
        reasons.append("schema_v2_required")
    for task in tasks:
        if task.provenance.get("production_real") is not True:
            reasons.append(f"{task.id}:production_real_required")
        elif _production_claim_invalid(task):
            reasons.append(f"{task.id}:curated_origin_cannot_claim_production")
        if not task.expected_files:
            reasons.append(f"{task.id}:expected_file_required")
        if not task.expected_symbols:
            reasons.append(f"{task.id}:expected_symbol_required")
        if not task.expected_ranges:
            reasons.append(f"{task.id}:expected_range_required")
        if not task.required_assertions:
            reasons.append(f"{task.id}:completion_assertion_required")
    return reasons


def load_tasks(
    path: Path = DEFAULT_MANIFEST_PATH, *, include_holdout: bool = False
) -> tuple[list[TokenEconomyTask], dict[str, Any]]:
    """Load a fixed corpus and state whether it is admissible for acceptance.

    ``holdout_ids`` tasks are excluded by default so they stay untouched by
    tuning; they are only scored when explicitly requested."""
    manifest = load_manifest(path)
    schema_version = int(manifest["schema_version"])
    if schema_version == 1:
        tasks, source_hashes = _legacy_tasks(manifest)
    else:
        tasks = _production_tasks(manifest)
        source_hashes = {}
    all_tasks = sorted(tasks, key=lambda task: task.id)
    tasks = all_tasks
    holdout_ids = [str(item) for item in manifest.get("holdout_ids") or []]
    if include_holdout:
        held_out = []
    else:
        held_out = [task for task in tasks if task.id in holdout_ids]
        tasks = [task for task in tasks if task.id not in holdout_ids]
    category_counts = _validate_split(all_tasks, manifest.get("category_split"))
    reasons = _acceptance_reasons(tasks, schema_version)
    repository = manifest.get("repository") or {}
    resolved = Path(path).resolve()
    manifest_path = (
        str(resolved.relative_to(PROJECT_ROOT)).replace("\\", "/")
        if resolved.is_relative_to(PROJECT_ROOT)
        else str(resolved)
    )
    return tasks, {
        "manifest_path": manifest_path,
        "manifest_sha256": sha256_file(path),
        "fixture_sha256": source_hashes,
        "seed": manifest.get("seed"),
        "category_counts": category_counts,
        "repository": {
            "name": repository.get("name"),
            "pinned_commit": repository.get("pinned_commit"),
            "pinned_ref": repository.get("pinned_ref"),
        },
        "holdout_ids": holdout_ids,
        "holdout_excluded": [task.id for task in held_out] if not include_holdout else [],
        "acceptance_eligible": not reasons,
        "acceptance_ineligible_reasons": reasons,
    }
