#!/usr/bin/env python3
"""Offline paired evaluation for baseline, LFM rerank, and FastPLAID.

The harness consumes saved rankings; it never calls production or a paid model.
Every query is scored against the same checked-in relevance set, producing
per-query deltas plus language/query-class slices.  This makes the gate paired
and reproducible instead of comparing unrelated aggregate smoke runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_DATASET = PROJECT_ROOT / "eval" / "golden_set.json"
DEFAULT_EXTENSION_DATASET = PROJECT_ROOT / "eval" / "lfm_eval_extension.json"
DEFAULT_HOLDOUT_DATASET = PROJECT_ROOT / "eval" / "holdout_set.json"
DEFAULT_FREEZE_MANIFEST = PROJECT_ROOT / "eval" / "frozen_datasets.json"
DEFAULT_CAPTURE_RUNNER = PROJECT_ROOT / "eval" / "run_paired_lfm_retrieval.py"
PRODUCTION_GATE_POLICY = "lfm-production-v1"
PRODUCTION_MEDIAN_NDCG_DELTA_MIN = 0.03
PRODUCTION_IMPROVED_RATIO_MIN = 0.60
PRODUCTION_HIT3_REGRESSION_MAX = 0.10
PRODUCTION_SLICE_REGRESSION_MAX = 0.05
VARIANTS = ("baseline", "lfm_rerank", "fastplaid")
METRIC_NAMES = ("hit@3", "recall@10", "recall@50", "mrr@10", "ndcg@10")
MIN_RECALL_50_DEPTH = 50
SCORED_LATE_INTERACTION_STATUSES = frozenset({"scored", "ok", "success"})
DEPTH_EVIDENCE_STATUSES = frozenset(
    {
        "sufficient",
        "sufficient_actual_depth",
        "insufficient_requested_depth",
        "insufficient_returned_depth",
        "insufficient_corpus_depth",
        "insufficient_missing_depth_metadata",
    }
)


@dataclass(frozen=True)
class DatasetEntry:
    query_id: str
    question: str
    expected_files: tuple[str, ...]
    language: str
    query_class: str
    provenance: dict[str, Any]
    split: str = "tune"
    trap_files: tuple[str, ...] = ()


def normalize_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    if normalized.startswith("/app/"):
        normalized = normalized[5:]
    return normalized.lstrip("/")


def _frozen_digests(manifest_path: Path = DEFAULT_FREEZE_MANIFEST) -> dict[str, str]:
    """Recorded sha256 digests of frozen dataset files, keyed by file name."""
    if not manifest_path.exists():
        return {}
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, dict):
        raise TypeError(f"{manifest_path} must contain a 'files' object")
    return {str(name): str(digest) for name, digest in files.items()}


def _verify_freeze(path: Path, manifest_path: Path = DEFAULT_FREEZE_MANIFEST) -> None:
    """Refuse to score a frozen holdout whose bytes drifted from the recording.

    A deliberate dataset change must also update ``eval/frozen_datasets.json``
    via ``--write-freeze``; silent edits to a holdout otherwise go unnoticed
    and every historical comparison becomes unauditable.
    """
    expected = _frozen_digests(manifest_path).get(path.name)
    if expected is None:
        return
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError(
            f"frozen dataset drifted: {path.name} sha256 {actual[:12]} != "
            f"recorded {expected[:12]} (regenerate with --write-freeze if intentional)"
        )


def load_dataset(
    base_path: Path = DEFAULT_BASE_DATASET,
    extension_path: Path = DEFAULT_EXTENSION_DATASET,
    holdout_path: Path | None = None,
) -> list[DatasetEntry]:
    """Load and validate the original golden set plus its separate extension."""
    paths = [base_path, extension_path]
    if holdout_path is not None:
        paths.append(holdout_path)
    records: list[dict[str, Any]] = []
    for path in paths:
        _verify_freeze(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise TypeError(f"{path} must contain a JSON array")
        records.extend(payload)

    entries: list[DatasetEntry] = []
    seen: set[str] = set()
    for position, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise TypeError(f"dataset item {position} must be an object")
        query_id = _required_text(record, "id", max_length=64)
        if query_id in seen:
            raise ValueError(f"duplicate dataset id: {query_id}")
        seen.add(query_id)
        question = _required_text(record, "question", max_length=2000)
        expected = record.get("expect_files")
        if not isinstance(expected, list) or not expected:
            raise ValueError(f"{query_id}: expect_files must be a non-empty array")
        expected_files = tuple(
            normalize_path(_bounded_text(path, f"{query_id}.expect_files", 1024))
            for path in expected
        )
        language = _required_text(record, "language", max_length=16).lower()
        query_class = _required_text(record, "query_class", max_length=64).lower()
        split = str(record.get("split") or "tune").lower()
        if split not in {"tune", "holdout"}:
            raise ValueError(f"{query_id}: split must be 'tune' or 'holdout'")
        traps = record.get("trap_files") or []
        if not isinstance(traps, list):
            raise TypeError(f"{query_id}: trap_files must be an array")
        trap_files = tuple(
            normalize_path(_bounded_text(path, f"{query_id}.trap_files", 1024))
            for path in traps
        )
        if query_class == "adversarial" and not trap_files:
            raise ValueError(
                f"{query_id}: adversarial items must declare trap_files"
            )
        provenance = record.get("provenance")
        if not isinstance(provenance, dict):
            raise ValueError(f"{query_id}: provenance is required")
        _validate_provenance(query_id, provenance)
        entries.append(
            DatasetEntry(
                query_id=query_id,
                question=question,
                expected_files=expected_files,
                language=language,
                query_class=query_class,
                provenance=provenance,
                split=split,
                trap_files=trap_files,
            )
        )
    return entries


def _required_text(record: dict[str, Any], key: str, *, max_length: int) -> str:
    return _bounded_text(record.get(key), key, max_length)


def _bounded_text(value: object, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    text = value.strip()
    if len(text) > max_length:
        raise ValueError(f"{field} exceeds {max_length} characters")
    return text


def _validate_provenance(query_id: str, provenance: dict[str, Any]) -> None:
    _bounded_text(provenance.get("kind"), f"{query_id}.provenance.kind", 64)
    _bounded_text(provenance.get("source"), f"{query_id}.provenance.source", 255)
    production_real = provenance.get("production_real")
    if not isinstance(production_real, bool):
        raise ValueError(f"{query_id}: provenance.production_real must be boolean")
    if provenance.get("kind") in {"curated", "curated_translation", "curated_synthetic"} and production_real:
        raise ValueError(f"{query_id}: curated/translated queries cannot claim production_real")


def load_runs(
    path: Path,
    *,
    require_production_evidence: bool = False,
) -> dict[str, dict[str, dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    enveloped = isinstance(payload, dict)
    expected_index_revision: str | None = None
    if require_production_evidence and (
        not enveloped or payload.get("schema_version") != 2
    ):
        raise ValueError(
            "production gate requires a complete schema v2 paired capture"
        )
    if enveloped:
        if payload.get("complete") is not True:
            raise ValueError("runs envelope is incomplete: complete must be true")
        failures = payload.get("failures")
        if not isinstance(failures, list):
            raise ValueError("runs envelope failures must be an array")
        if failures:
            raise ValueError(f"runs envelope contains {len(failures)} capture failures")
        if payload.get("schema_version") == 2:
            evidence = payload.get("evidence")
            if not isinstance(evidence, dict):
                raise ValueError("schema v2 runs require evidence")
            expected_index_revision = _bounded_text(
                evidence.get("expected_index_revision"),
                "evidence.expected_index_revision",
                64,
            )
            if not re.fullmatch(r"r[0-9]+", expected_index_revision):
                raise ValueError("schema v2 expected index revision must use rN")
            expected_lineage_id = _bounded_text(
                evidence.get("expected_lineage_id"),
                "evidence.expected_lineage_id",
                128,
            )
            observed_lineage_id = _bounded_text(
                evidence.get("observed_lineage_id"),
                "evidence.observed_lineage_id",
                128,
            )
            if expected_lineage_id != observed_lineage_id:
                raise ValueError("schema v2 corpus lineage evidence does not match")
            expected_identity_digest = _bounded_text(
                evidence.get("expected_identity_digest"),
                "evidence.expected_identity_digest",
                64,
            ).lower()
            observed_identity_digest = _bounded_text(
                evidence.get("observed_identity_digest"),
                "evidence.observed_identity_digest",
                64,
            ).lower()
            if (
                not re.fullmatch(r"[0-9a-f]{64}", expected_identity_digest)
                or expected_identity_digest != observed_identity_digest
            ):
                raise ValueError("schema v2 corpus identity digest evidence does not match")
            raw_expected_document_count = evidence.get("expected_document_count")
            raw_observed_document_count = evidence.get("observed_document_count")
            if (
                raw_expected_document_count is None
                or raw_observed_document_count is None
            ):
                raise ValueError(
                    "schema v2 corpus document count evidence must be numeric"
                )
            try:
                expected_document_count = int(raw_expected_document_count)
                observed_document_count = int(raw_observed_document_count)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "schema v2 corpus document count evidence must be numeric"
                ) from exc
            if (
                expected_document_count < 1
                or expected_document_count != observed_document_count
            ):
                raise ValueError("schema v2 corpus document count evidence does not match")
        records = payload.get("results")
    else:
        records = payload
    if not isinstance(records, list):
        raise ValueError("runs file must be an array or an object with results[]")

    runs: dict[str, dict[str, dict[str, Any]]] = {}
    for raw in records:
        if not isinstance(raw, dict):
            raise ValueError("each runs item must be an object")
        query_id = _bounded_text(raw.get("query_id"), "query_id", 64)
        if query_id in runs:
            raise ValueError(f"duplicate runs query_id: {query_id}")
        variants = raw.get("variants")
        if not isinstance(variants, dict):
            raise ValueError(f"{query_id}: variants must be an object")
        if "baseline" not in variants or "lfm_rerank" not in variants:
            raise ValueError(f"{query_id}: baseline and lfm_rerank rankings are required")
        late = raw.get("late_interaction")
        if enveloped and not isinstance(late, dict):
            raise ValueError(
                f"{query_id}: enveloped capture requires late_interaction evidence"
            )
        if late is not None:
            if not isinstance(late, dict):
                raise ValueError(f"{query_id}: late_interaction must be an object")
            late_status = str(late.get("status") or "missing")
            if late_status not in SCORED_LATE_INTERACTION_STATUSES:
                raise ValueError(
                    f"{query_id}: late_interaction status {late_status!r} is not scored"
                )
            if expected_index_revision is not None:
                observed_index_revision = _bounded_text(
                    late.get("index_revision"),
                    f"{query_id}.late_interaction.index_revision",
                    64,
                )
                if observed_index_revision != expected_index_revision:
                    raise ValueError(
                        f"{query_id}: late-interaction index revision "
                        f"{observed_index_revision!r} does not match exact "
                        f"{expected_index_revision!r}"
                    )
        runs[query_id] = {
            name: _normalize_variant(query_id, name, variants[name])
            for name in VARIANTS
            if name in variants
        }
    return runs


def load_release_binding(
    path: Path,
    *,
    required: bool = False,
) -> dict[str, Any] | None:
    """Load the immutable release/corpus identity from a paired capture."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        if required:
            raise ValueError("production gate requires a release-bound runs envelope")
        return None
    raw_binding = payload.get("release_binding")
    if raw_binding is None and not required:
        return None
    if not isinstance(raw_binding, dict):
        raise ValueError("production gate requires release_binding")

    repository_id = raw_binding.get("repository_id")
    document_count = raw_binding.get("document_count")
    if (
        isinstance(repository_id, bool)
        or not isinstance(repository_id, int)
        or repository_id < 1
    ):
        raise ValueError("release_binding.repository_id must be a positive integer")
    if (
        isinstance(document_count, bool)
        or not isinstance(document_count, int)
        or document_count < 1
    ):
        raise ValueError("release_binding.document_count must be a positive integer")
    binding: dict[str, Any] = {
        "repository_id": repository_id,
        "build_sha": _bounded_text(
            raw_binding.get("build_sha"),
            "release_binding.build_sha",
            128,
        ),
        "source_digest": _bounded_text(
            raw_binding.get("source_digest"),
            "release_binding.source_digest",
            64,
        ).lower(),
        "model_revision": _bounded_text(
            raw_binding.get("model_revision"),
            "release_binding.model_revision",
            128,
        ),
        "index_revision": _bounded_text(
            raw_binding.get("index_revision"),
            "release_binding.index_revision",
            64,
        ),
        "lineage_id": _bounded_text(
            raw_binding.get("lineage_id"),
            "release_binding.lineage_id",
            128,
        ),
        "identity_digest": _bounded_text(
            raw_binding.get("identity_digest"),
            "release_binding.identity_digest",
            64,
        ).lower(),
        "document_count": document_count,
    }
    if binding["build_sha"] == "unknown":
        raise ValueError("release_binding.build_sha must be immutable")
    if not re.fullmatch(r"[0-9a-f]{64}", binding["source_digest"]):
        raise ValueError("release_binding.source_digest must be SHA-256")
    if not re.fullmatch(r"r[0-9]+", binding["index_revision"]):
        raise ValueError("release_binding.index_revision must use rN")
    if not re.fullmatch(r"[0-9a-f]{64}", binding["identity_digest"]):
        raise ValueError("release_binding.identity_digest must be SHA-256")
    if set(raw_binding) != set(binding):
        raise ValueError("release_binding contains an unexpected field set")

    repository = payload.get("repository")
    if not isinstance(repository, dict) or repository.get("id") != repository_id:
        raise ValueError("release_binding repository does not match capture repository")
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("release_binding requires capture evidence")
    cross_checks = {
        "expected_index_revision": binding["index_revision"],
        "observed_lineage_id": binding["lineage_id"],
        "observed_identity_digest": binding["identity_digest"],
        "observed_document_count": binding["document_count"],
    }
    for field, expected in cross_checks.items():
        if evidence.get(field) != expected:
            raise ValueError(
                f"release_binding does not match capture evidence {field}"
            )

    records = payload.get("results")
    if not isinstance(records, list):
        raise ValueError("release_binding requires capture results")
    for raw in records:
        if not isinstance(raw, dict):
            raise ValueError("release-bound capture results must be objects")
        query_id = _bounded_text(raw.get("query_id"), "query_id", 64)
        late = raw.get("late_interaction")
        if not isinstance(late, dict):
            raise ValueError(
                f"{query_id}: release-bound capture requires late_interaction"
            )
        if late.get("model_revision") != binding["model_revision"]:
            raise ValueError(
                f"{query_id}: model revision does not match release_binding"
            )
        if late.get("index_revision") != binding["index_revision"]:
            raise ValueError(
                f"{query_id}: index revision does not match release_binding"
            )
    return binding


def _normalize_variant(
    query_id: str,
    variant: str,
    raw_variant: object,
) -> dict[str, Any]:
    raw_ranking: object
    raw_depth: object
    if isinstance(raw_variant, list):
        raw_ranking = raw_variant
        raw_depth = None
    elif isinstance(raw_variant, dict):
        raw_ranking = raw_variant.get("ranking")
        raw_depth = raw_variant.get("depth")
    else:
        raise ValueError(f"{query_id}.{variant}: variant must be an array or object")

    ranking = _normalize_ranking(query_id, variant, raw_ranking)
    actual_unique = len(ranking)
    if raw_depth is None:
        sufficient = actual_unique >= MIN_RECALL_50_DEPTH
        return {
            "ranking": ranking,
            "depth": {
                "requested": None,
                "returned_unique": actual_unique,
                "corpus_size": None,
                "status": (
                    "sufficient_actual_depth"
                    if sufficient
                    else "insufficient_missing_depth_metadata"
                ),
                "recall_at_50_eligible": sufficient,
            },
        }
    if not isinstance(raw_depth, dict):
        raise ValueError(f"{query_id}.{variant}.depth must be an object")

    requested = _optional_nonnegative_int(
        raw_depth.get("requested"),
        f"{query_id}.{variant}.depth.requested",
    )
    returned_unique = _optional_nonnegative_int(
        raw_depth.get("returned_unique"),
        f"{query_id}.{variant}.depth.returned_unique",
    )
    if returned_unique is None:
        raise ValueError(f"{query_id}.{variant}.depth.returned_unique is required")
    if returned_unique != actual_unique:
        raise ValueError(
            f"{query_id}.{variant}: depth returned_unique={returned_unique} "
            f"does not match {actual_unique} unique ranking items"
        )
    corpus_size = _optional_nonnegative_int(
        raw_depth.get("corpus_size"),
        f"{query_id}.{variant}.depth.corpus_size",
    )
    if corpus_size is not None and actual_unique > corpus_size:
        raise ValueError(
            f"{query_id}.{variant}: ranking has {actual_unique} unique items "
            f"but corpus_size is {corpus_size}"
        )
    status = _bounded_text(
        raw_depth.get("status"),
        f"{query_id}.{variant}.depth.status",
        64,
    )
    if status not in DEPTH_EVIDENCE_STATUSES:
        raise ValueError(f"{query_id}.{variant}: unknown depth evidence status {status!r}")
    if status == "insufficient_corpus_depth" and (
        corpus_size is None or corpus_size >= MIN_RECALL_50_DEPTH
    ):
        raise ValueError(
            f"{query_id}.{variant}: insufficient_corpus_depth requires "
            f"corpus_size below {MIN_RECALL_50_DEPTH}"
        )
    sufficient = (
        status in {"sufficient", "sufficient_actual_depth"}
        and actual_unique >= MIN_RECALL_50_DEPTH
        and (requested is None or requested >= MIN_RECALL_50_DEPTH)
    )
    if status in {"sufficient", "sufficient_actual_depth"} and not sufficient:
        raise ValueError(
            f"{query_id}.{variant}: sufficient depth status requires "
            f"{MIN_RECALL_50_DEPTH} actual unique items and requested depth >= "
            f"{MIN_RECALL_50_DEPTH}"
        )
    return {
        "ranking": ranking,
        "depth": {
            "requested": requested,
            "returned_unique": returned_unique,
            "corpus_size": corpus_size,
            "status": status,
            "recall_at_50_eligible": sufficient,
        },
    }


def _optional_nonnegative_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer or null")
    return value


def _normalize_ranking(
    query_id: str,
    variant: str,
    raw_ranking: object,
) -> list[dict[str, Any]]:
    if not isinstance(raw_ranking, list):
        raise ValueError(f"{query_id}.{variant}: ranking must be an array")
    if len(raw_ranking) > 1000:
        raise ValueError(f"{query_id}.{variant}: ranking exceeds 1000 items")
    ranking: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_ranking:
        if isinstance(raw, str):
            path = normalize_path(raw)
            score = None
        elif isinstance(raw, dict):
            path_value = raw.get("path") or raw.get("file_path") or raw.get("id")
            path = normalize_path(_bounded_text(path_value, f"{query_id}.{variant}.path", 1024))
            score = raw.get("score")
            if score is not None:
                score = float(score)
                if not math.isfinite(score):
                    raise ValueError(f"{query_id}.{variant}: score must be finite")
        else:
            raise ValueError(f"{query_id}.{variant}: ranking items must be strings or objects")
        if not path or path in seen:
            continue
        seen.add(path)
        ranking.append({"path": path, "score": score})
    return ranking


def _is_relevant(path: str, expected_files: Iterable[str]) -> bool:
    return any(
        path == expected
        or path.endswith(f"/{expected}")
        or expected.endswith(f"/{path}")
        for expected in expected_files
    )


def score_ranking(
    ranking: list[dict[str, Any]],
    expected_files: tuple[str, ...],
    *,
    recall_at_50_eligible: bool | None = None,
) -> dict[str, float | None]:
    """Return binary-relevance retrieval metrics for one ordered ranking."""
    relevant_flags = [
        1.0 if _is_relevant(item["path"], expected_files) else 0.0
        for item in ranking
    ]
    relevant_total = len(set(expected_files))
    first_rank = next(
        (index for index, flag in enumerate(relevant_flags[:10], start=1) if flag),
        0,
    )

    def recall_at(k: int) -> float:
        found_paths = {
            item["path"]
            for item in ranking[:k]
            if _is_relevant(item["path"], expected_files)
        }
        # Each expected file is a separate relevant target, so match expected
        # values directly rather than counting duplicate path aliases.
        found_expected = sum(
            1
            for expected in set(expected_files)
            if any(_is_relevant(item["path"], (expected,)) for item in ranking[:k])
        )
        return found_expected / relevant_total if relevant_total else 0.0

    dcg = sum(
        flag / math.log2(rank + 1)
        for rank, flag in enumerate(relevant_flags[:10], start=1)
    )
    ideal_hits = min(relevant_total, 10)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    if recall_at_50_eligible is None:
        recall_at_50_eligible = len(ranking) >= MIN_RECALL_50_DEPTH
    return {
        "hit@3": 1.0 if any(relevant_flags[:3]) else 0.0,
        "recall@10": recall_at(10),
        "recall@50": recall_at(50) if recall_at_50_eligible else None,
        "mrr@10": 1.0 / first_rank if first_rank else 0.0,
        "ndcg@10": dcg / idcg if idcg else 0.0,
    }


def evaluate_paired(
    dataset: list[DatasetEntry],
    runs: dict[str, dict[str, Any]],
    *,
    median_ndcg_delta_min: float = 0.03,
    improved_ratio_min: float = 0.60,
    hit3_regression_max: float = 0.10,
    slice_regression_max: float = 0.05,
) -> dict[str, Any]:
    expected_ids = {entry.query_id for entry in dataset}
    missing = sorted(expected_ids - runs.keys())
    unknown = sorted(runs.keys() - expected_ids)
    if missing:
        raise ValueError(f"runs missing {len(missing)} dataset queries: {missing[:5]}")
    if unknown:
        raise ValueError(f"runs contain unknown query ids: {unknown[:5]}")

    normalized_runs: dict[str, dict[str, dict[str, Any]]] = {}
    for query_id, query_runs in runs.items():
        if not isinstance(query_runs, dict):
            raise ValueError(f"{query_id}: query variants must be an object")
        normalized_runs[query_id] = {
            variant: _normalize_variant(query_id, variant, query_runs[variant])
            for variant in VARIANTS
            if variant in query_runs
        }
        if "baseline" not in normalized_runs[query_id]:
            raise ValueError(f"{query_id}: baseline ranking is required")

    per_query: list[dict[str, Any]] = []
    for entry in dataset:
        query_runs = normalized_runs[entry.query_id]
        scored = {
            variant: score_ranking(
                payload["ranking"],
                entry.expected_files,
                recall_at_50_eligible=payload["depth"]["recall_at_50_eligible"],
            )
            for variant, payload in query_runs.items()
        }
        deltas = {
            variant: {
                metric: _metric_delta(values[metric], scored["baseline"][metric])
                for metric in METRIC_NAMES
            }
            for variant, values in scored.items()
            if variant != "baseline"
        }
        traps = None
        if entry.trap_files:
            traps = {}
            for variant, payload in query_runs.items():
                ranking = payload["ranking"]
                trap_ranks = [
                    rank
                    for rank, item in enumerate(ranking, start=1)
                    if _is_relevant(item["path"], entry.trap_files)
                ]
                expected_ranks = [
                    rank
                    for rank, item in enumerate(ranking, start=1)
                    if _is_relevant(item["path"], entry.expected_files)
                ]
                traps[variant] = {
                    "in_top_10": any(rank <= 10 for rank in trap_ranks),
                    "above_expected": bool(
                        trap_ranks
                        and (not expected_ranks or min(trap_ranks) < min(expected_ranks))
                    ),
                }
        per_query.append(
            {
                "query_id": entry.query_id,
                "language": entry.language,
                "query_class": entry.query_class,
                "split": entry.split,
                "metrics": scored,
                "deltas_vs_baseline": deltas,
                "traps": traps,
                "variant_evidence": {
                    variant: payload["depth"]
                    for variant, payload in query_runs.items()
                },
            }
        )

    variant_counts = {
        variant: sum(1 for item in per_query if variant in item["metrics"])
        for variant in VARIANTS
    }
    depth_50_eligible_counts = {
        variant: sum(
            1
            for item in per_query
            if item["variant_evidence"].get(variant, {}).get(
                "recall_at_50_eligible",
                False,
            )
        )
        for variant in VARIANTS
    }
    depth_status_counts: dict[str, dict[str, int]] = {}
    for variant in VARIANTS:
        counts: dict[str, int] = defaultdict(int)
        for item in per_query:
            evidence = item["variant_evidence"].get(variant)
            if evidence is not None:
                counts[str(evidence["status"])] += 1
        depth_status_counts[variant] = dict(sorted(counts.items()))

    variants = sorted(
        {variant for item in per_query for variant in item["metrics"]},
        key=VARIANTS.index,
    )
    aggregate = {
        variant: _mean_metrics(
            item["metrics"][variant]
            for item in per_query
            if variant in item["metrics"]
        )
        for variant in variants
    }
    slices = {
        dimension: _slice_metrics(per_query, dimension, variants)
        for dimension in ("language", "query_class", "split")
    }
    trap_queries = [item for item in per_query if item["traps"]]
    trap_contamination = {
        variant: {
            "queries": len(trap_queries),
            "in_top_10_rate": (
                sum(1 for item in trap_queries if item["traps"][variant]["in_top_10"])
                / len(trap_queries)
            ),
            "above_expected_rate": (
                sum(1 for item in trap_queries if item["traps"][variant]["above_expected"])
                / len(trap_queries)
            ),
        }
        for variant in variants
    } if trap_queries else None
    paired = {
        variant: _paired_summary(per_query, variant)
        for variant in variants
        if variant != "baseline"
    }

    required_count = len(dataset)
    ru_count = sum(1 for entry in dataset if entry.language.startswith("ru"))
    lfm = paired.get("lfm_rerank", _empty_paired_summary())
    baseline_metrics = aggregate["baseline"]
    lfm_metrics = aggregate.get("lfm_rerank", _empty_metrics())
    slice_failures = _slice_regressions(
        slices,
        candidate="lfm_rerank",
        maximum=slice_regression_max,
    )
    lfm_depth_evidence_complete = (
        depth_50_eligible_counts["baseline"] == required_count
        and depth_50_eligible_counts["lfm_rerank"] == required_count
    )
    lfm_variant_coverage_complete = (
        variant_counts["baseline"] == required_count
        and variant_counts["lfm_rerank"] == required_count
        and lfm["count"] == required_count
    )
    recall_50_non_regression = (
        lfm_depth_evidence_complete
        and _not_worse(
            lfm_metrics["recall@50"],
            baseline_metrics["recall@50"],
        )
    )
    checks = {
        "dataset_count_at_least_50": len(dataset) >= 50,
        "ru_or_ru_en_count_at_least_15": ru_count >= 15,
        "baseline_and_lfm_variant_coverage_complete": lfm_variant_coverage_complete,
        "recall_at_50_evidence_complete": lfm_depth_evidence_complete,
        "median_ndcg_delta": _at_least(
            lfm["median_deltas"]["ndcg@10"],
            median_ndcg_delta_min,
        ),
        "improved_query_ratio": lfm["improved_ndcg_ratio"] >= improved_ratio_min,
        "recall_at_50_non_regression": recall_50_non_regression,
        "hit_at_3_regression_bound": (
            _not_worse(
                lfm_metrics["hit@3"],
                baseline_metrics["hit@3"],
                allowed_regression=hit3_regression_max,
            )
        ),
        "language_and_class_regression_bound": not slice_failures,
    }
    gate = {
        "passed": all(checks.values()),
        "checks": checks,
        "thresholds": {
            "median_ndcg_delta_min": median_ndcg_delta_min,
            "improved_ratio_min": improved_ratio_min,
            "hit3_regression_max": hit3_regression_max,
            "slice_regression_max": slice_regression_max,
        },
        "slice_failures": slice_failures,
    }

    fastplaid_gate = None
    if variant_counts["fastplaid"]:
        fast = aggregate["fastplaid"]
        fast_paired = paired.get("fastplaid", _empty_paired_summary())
        fast_variant_coverage_complete = (
            variant_counts["baseline"] == required_count
            and variant_counts["fastplaid"] == required_count
            and fast_paired["count"] == required_count
        )
        fast_depth_evidence_complete = (
            depth_50_eligible_counts["baseline"] == required_count
            and depth_50_eligible_counts["fastplaid"] == required_count
        )
        fast_checks = {
            "dataset_coverage_complete": fast_variant_coverage_complete,
            "paired_query_count_complete": fast_paired["count"] == required_count,
            "recall_at_50_evidence_complete": fast_depth_evidence_complete,
            "recall_at_50_non_regression": (
                fast_depth_evidence_complete
                and _not_worse(
                    fast["recall@50"],
                    baseline_metrics["recall@50"],
                )
            ),
            "hit_at_3_regression_bound": _not_worse(
                fast["hit@3"],
                baseline_metrics["hit@3"],
                allowed_regression=hit3_regression_max,
            ),
        }
        fastplaid_gate = {
            "passed": all(fast_checks.values()),
            "checks": fast_checks,
        }
    overall_gate = {
        "passed": gate["passed"]
        and (fastplaid_gate is None or fastplaid_gate["passed"]),
        "required_gates": [
            "lfm_rerank",
            *(["fastplaid"] if fastplaid_gate is not None else []),
        ],
    }

    return {
        "schema_version": 2,
        "dataset": {
            "count": len(dataset),
            "splits": {
                split: sum(1 for entry in dataset if entry.split == split)
                for split in sorted({entry.split for entry in dataset})
            },
            "ru_or_ru_en_count": ru_count,
            "production_real_count": sum(
                1 for entry in dataset if entry.provenance["production_real"]
            ),
            "provenance_notice": (
                "Curated and translated queries are evaluation fixtures, not production-real traffic."
            ),
        },
        "evidence": {
            "required_query_count": required_count,
            "variant_query_counts": variant_counts,
            "recall_at_50_eligible_counts": depth_50_eligible_counts,
            "depth_status_counts": depth_status_counts,
            "recall_at_50_minimum_ranking_depth": MIN_RECALL_50_DEPTH,
        },
        "aggregate": aggregate,
        "paired": paired,
        "trap_contamination": trap_contamination,
        "slices": slices,
        "per_query": per_query,
        "lfm_rerank_gate": gate,
        "fastplaid_gate": fastplaid_gate,
        "overall_gate": overall_gate,
    }


def _metric_delta(
    candidate: float | None,
    baseline: float | None,
) -> float | None:
    if candidate is None or baseline is None:
        return None
    return candidate - baseline


def _empty_metrics() -> dict[str, float | None]:
    return {metric: None for metric in METRIC_NAMES}


def _mean_metrics(
    values: Iterable[dict[str, float | None]],
) -> dict[str, float | None]:
    rows = list(values)
    if not rows:
        return _empty_metrics()
    result: dict[str, float | None] = {}
    for metric in METRIC_NAMES:
        metric_values = [row[metric] for row in rows]
        result[metric] = (
            round(statistics.fmean(value for value in metric_values if value is not None), 6)
            if all(value is not None for value in metric_values)
            else None
        )
    return result


def _paired_summary(
    per_query: list[dict[str, Any]],
    candidate: str,
) -> dict[str, Any]:
    rows = [
        item["deltas_vs_baseline"][candidate]
        for item in per_query
        if candidate in item["deltas_vs_baseline"]
    ]
    mean_deltas: dict[str, float | None] = {}
    median_deltas: dict[str, float | None] = {}
    for metric in METRIC_NAMES:
        values = [row[metric] for row in rows]
        if rows and all(value is not None for value in values):
            numeric = [value for value in values if value is not None]
            mean_deltas[metric] = round(statistics.fmean(numeric), 6)
            median_deltas[metric] = round(statistics.median(numeric), 6)
        else:
            mean_deltas[metric] = None
            median_deltas[metric] = None
    return {
        "count": len(rows),
        "mean_deltas": mean_deltas,
        "median_deltas": median_deltas,
        "improved_ndcg_ratio": round(
            sum(1 for row in rows if row["ndcg@10"] > 0) / len(rows),
            6,
        )
        if rows
        else 0.0,
        "regressed_ndcg_ratio": round(
            sum(1 for row in rows if row["ndcg@10"] < 0) / len(rows),
            6,
        )
        if rows
        else 0.0,
    }


def _empty_paired_summary() -> dict[str, Any]:
    return {
        "count": 0,
        "mean_deltas": _empty_metrics(),
        "median_deltas": _empty_metrics(),
        "improved_ndcg_ratio": 0.0,
        "regressed_ndcg_ratio": 0.0,
    }


def _at_least(value: float | None, minimum: float) -> bool:
    return value is not None and value >= minimum


def _not_worse(
    candidate: float | None,
    baseline: float | None,
    *,
    allowed_regression: float = 0.0,
) -> bool:
    return (
        candidate is not None
        and baseline is not None
        and candidate >= baseline - allowed_regression
    )


def _slice_metrics(
    per_query: list[dict[str, Any]],
    dimension: str,
    variants: list[str],
) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in per_query:
        groups[item[dimension]].append(item)
    result: dict[str, Any] = {}
    for slice_name, items in sorted(groups.items()):
        result[slice_name] = {
            "count": len(items),
            "variants": {
                variant: _mean_metrics(
                    item["metrics"][variant]
                    for item in items
                    if variant in item["metrics"]
                )
                for variant in variants
            },
        }
    return result


def _slice_regressions(
    slices: dict[str, dict[str, Any]],
    *,
    candidate: str,
    maximum: float,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for dimension, dimension_slices in slices.items():
        for slice_name, payload in dimension_slices.items():
            variants = payload["variants"]
            if candidate not in variants:
                continue
            for metric in ("hit@3", "recall@10"):
                candidate_value = variants[candidate][metric]
                baseline_value = variants["baseline"][metric]
                if candidate_value is None or baseline_value is None:
                    continue
                delta = candidate_value - baseline_value
                if delta < -maximum:
                    failures.append(
                        {
                            "dimension": dimension,
                            "slice": slice_name,
                            "metric": metric,
                            "delta": round(delta, 6),
                        }
                    )
    return failures


def production_gate_thresholds() -> dict[str, float]:
    """Return a fresh copy of the immutable production quality policy."""
    return {
        "median_ndcg_delta_min": PRODUCTION_MEDIAN_NDCG_DELTA_MIN,
        "improved_ratio_min": PRODUCTION_IMPROVED_RATIO_MIN,
        "hit3_regression_max": PRODUCTION_HIT3_REGRESSION_MAX,
        "slice_regression_max": PRODUCTION_SLICE_REGRESSION_MAX,
    }


def resolve_evaluation_policy(args: argparse.Namespace) -> dict[str, float]:
    """Resolve CLI thresholds and reject production policy overrides."""
    option_names = tuple(production_gate_thresholds())
    supplied_overrides = {
        name: getattr(args, name)
        for name in option_names
        if getattr(args, name) is not None
    }
    if args.production_gate:
        if getattr(args, "holdout_dataset", None) is not None:
            raise ValueError("production gate does not accept an additional holdout dataset")
        if getattr(args, "split", "all") != "all":
            raise ValueError("production gate requires the complete dataset; --split must be all")
        if supplied_overrides:
            raise ValueError(
                "production gate thresholds are fixed and cannot be overridden: "
                + ", ".join(sorted(supplied_overrides))
            )
        if Path(args.base_dataset).resolve() != DEFAULT_BASE_DATASET.resolve():
            raise ValueError("production gate requires the baked default base dataset")
        if (
            Path(args.extension_dataset).resolve()
            != DEFAULT_EXTENSION_DATASET.resolve()
        ):
            raise ValueError(
                "production gate requires the baked default extension dataset"
            )
        return production_gate_thresholds()

    thresholds = production_gate_thresholds()
    thresholds.update(supplied_overrides)
    return thresholds


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _provenance_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _file_provenance(path: Path, *, required: bool) -> dict[str, Any] | None:
    resolved = path.resolve()
    if not resolved.is_file():
        if required:
            raise FileNotFoundError(f"evaluation input is missing: {resolved}")
        return None
    return {
        "path": _provenance_path(resolved),
        "sha256": _sha256_file(resolved),
        "bytes": resolved.stat().st_size,
    }


def build_evaluation_provenance(
    *,
    runs_path: Path,
    base_dataset_path: Path,
    extension_dataset_path: Path,
    production_gate: bool,
    thresholds: dict[str, float],
    evaluator_path: Path | None = None,
    runner_path: Path | None = DEFAULT_CAPTURE_RUNNER,
    holdout_dataset_path: Path | None = None,
    split: str = "all",
) -> dict[str, Any]:
    """Hash every artifact that determines the paired production verdict."""
    evaluator = evaluator_path or Path(__file__)
    return {
        "policy": (
            PRODUCTION_GATE_POLICY if production_gate else "custom-or-default"
        ),
        "production_gate": production_gate,
        "fixed_thresholds": production_gate,
        "split": split,
        "thresholds": dict(sorted(thresholds.items())),
        "artifacts": {
            "runs": _file_provenance(runs_path, required=True),
            "base_dataset": _file_provenance(
                base_dataset_path,
                required=True,
            ),
            "extension_dataset": _file_provenance(
                extension_dataset_path,
                required=True,
            ),
            "holdout_dataset": (
                _file_provenance(holdout_dataset_path, required=True)
                if holdout_dataset_path is not None
                else None
            ),
            "evaluator": _file_provenance(evaluator, required=False),
            "runner": (
                _file_provenance(runner_path, required=False)
                if runner_path is not None
                else None
            ),
        },
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, required=True, help="Saved paired rankings JSON")
    parser.add_argument("--base-dataset", type=Path, default=DEFAULT_BASE_DATASET)
    parser.add_argument("--extension-dataset", type=Path, default=DEFAULT_EXTENSION_DATASET)
    parser.add_argument(
        "--holdout-dataset",
        type=Path,
        default=None,
        help="Frozen holdout dataset; digest is verified against eval/frozen_datasets.json",
    )
    parser.add_argument(
        "--split",
        choices=("all", "tune", "holdout"),
        default="all",
        help="Restrict scoring to one dataset split",
    )
    parser.add_argument(
        "--write-freeze",
        metavar="DATASET",
        type=Path,
        default=None,
        help="Record the sha256 of a dataset into eval/frozen_datasets.json and exit",
    )
    parser.add_argument("--json", type=Path, help="Output report path (required unless --write-freeze)")
    parser.add_argument(
        "--production-gate",
        action="store_true",
        help=(
            "Use baked datasets, schema v2 exact-index capture, and immutable "
            "production thresholds"
        ),
    )
    parser.add_argument("--median-ndcg-delta-min", type=float)
    parser.add_argument("--improved-ratio-min", type=float)
    parser.add_argument("--hit3-regression-max", type=float)
    parser.add_argument("--slice-regression-max", type=float)
    return parser.parse_args()


def _write_freeze(path: Path, manifest_path: Path = DEFAULT_FREEZE_MANIFEST) -> None:
    """Record a dataset's sha256 in the freeze manifest (deliberate action)."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest: dict[str, Any] = {"files": {}}
    if manifest_path.exists():
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(loaded.get("files"), dict):
            manifest["files"] = loaded["files"]
    manifest["files"][path.name] = digest
    manifest["sha256"] = "sha256 of file bytes"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    args = _parse_args()
    if args.write_freeze is not None:
        _write_freeze(args.write_freeze)
        return 0
    if args.json is None:
        raise ValueError("--json is required")
    thresholds = resolve_evaluation_policy(args)
    provenance = build_evaluation_provenance(
        runs_path=args.runs,
        base_dataset_path=args.base_dataset,
        extension_dataset_path=args.extension_dataset,
        production_gate=args.production_gate,
        thresholds=thresholds,
        holdout_dataset_path=args.holdout_dataset,
        split=args.split,
    )
    release_binding = load_release_binding(
        args.runs,
        required=args.production_gate,
    )
    dataset = load_dataset(
        args.base_dataset,
        args.extension_dataset,
        holdout_path=args.holdout_dataset,
    )
    if args.split != "all":
        dataset = [entry for entry in dataset if entry.split == args.split]
        if not dataset:
            raise ValueError(f"no dataset entries with split={args.split}")
    report = evaluate_paired(
        dataset,
        load_runs(
            args.runs,
            require_production_evidence=args.production_gate,
        ),
        **thresholds,
    )
    verified_provenance = build_evaluation_provenance(
        runs_path=args.runs,
        base_dataset_path=args.base_dataset,
        extension_dataset_path=args.extension_dataset,
        production_gate=args.production_gate,
        thresholds=thresholds,
    )
    if verified_provenance != provenance:
        raise RuntimeError("evaluation inputs changed while the report was running")
    report["evaluation_provenance"] = provenance
    if release_binding is not None:
        report["release_binding"] = release_binding
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    gate = report["overall_gate"]
    median_delta = report["paired"]["lfm_rerank"]["median_deltas"]["ndcg@10"]
    median_text = f"{median_delta:.4f}" if median_delta is not None else "unavailable"
    print(
        f"paired-eval count={report['dataset']['count']} "
        f"ru={report['dataset']['ru_or_ru_en_count']} "
        f"median_delta_ndcg@10={median_text} "
        f"improved={report['paired']['lfm_rerank']['improved_ndcg_ratio']:.1%} "
        f"gate={'PASS' if gate['passed'] else 'FAIL'}"
    )
    return 0 if gate["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
