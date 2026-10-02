#!/usr/bin/env python3
"""Run the checked-in paired LFM retrieval corpus against one indexed repo.

This executes the same HybridRetrievalPipeline used by context packs. It forces
deterministic classification/reranking so the baseline and LFM arms differ only
by late-interaction scores and never spend a second LLM call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from brain.config.settings import settings
from brain.context.context_pack_builder import ContextPackBuilder
from brain.database.models import File
from brain.database.repository_utils import get_repository_by_path
from brain.database.session import (
    async_session_factory,
    close_database_connections,
    init_db,
)
from brain.late_interaction.client import (
    close_late_interaction_client,
    validate_remote_late_interaction_release,
)
from brain.retrieval.pipeline import HybridRetrievalPipeline
from brain.version import build_info
from eval.paired_retrieval_eval import load_dataset


SCORED_STATUSES = frozenset({"scored", "ok", "success"})
MIN_RECALL_50_DEPTH = 50


@contextmanager
def fixture_shadow_persistence_disabled():
    """Keep curated evaluation traffic out of durable production evidence."""
    previous = settings.LATE_INTERACTION_SHADOW_PERSIST_ENABLED
    settings.LATE_INTERACTION_SHADOW_PERSIST_ENABLED = False
    try:
        yield
    finally:
        settings.LATE_INTERACTION_SHADOW_PERSIST_ENABLED = previous


def require_exact_evaluation_revision() -> str:
    """Refuse live/floor mode so one capture cannot span index revisions."""
    if settings.LATE_INTERACTION_DUAL_WRITE_ENABLED:
        raise RuntimeError(
            "disable LATE_INTERACTION_DUAL_WRITE_ENABLED for exact paired capture"
        )
    expected = settings.LATE_INTERACTION_REMOTE_EXPECTED_INDEX_REVISION
    if not expected:
        raise RuntimeError(
            "LATE_INTERACTION_REMOTE_EXPECTED_INDEX_REVISION is required"
        )
    return expected


def _variant_record(
    paths: list[str],
    *,
    requested_depth: int,
    corpus_size: int | None,
) -> dict[str, Any]:
    unique_paths = list(dict.fromkeys(str(path) for path in paths if str(path)))
    returned_unique = len(unique_paths)
    if requested_depth < MIN_RECALL_50_DEPTH:
        status = "insufficient_requested_depth"
    elif returned_unique >= MIN_RECALL_50_DEPTH:
        status = "sufficient"
    elif corpus_size is not None and corpus_size < MIN_RECALL_50_DEPTH:
        status = "insufficient_corpus_depth"
    else:
        status = "insufficient_returned_depth"
    return {
        "ranking": [{"path": path} for path in unique_paths],
        "depth": {
            "requested": requested_depth,
            "returned_unique": returned_unique,
            "corpus_size": corpus_size,
            "status": status,
        },
    }


def result_record(
    query_id: str,
    result,
    *,
    requested_depth: int = MIN_RECALL_50_DEPTH,
    corpus_size: int | None = None,
) -> dict[str, Any]:
    late = (result.debug or {}).get("late_interaction") or {}
    baseline = list(late.get("baseline_top_k") or result.selected_paths)
    status = str(late.get("status") or "missing")
    counterfactual_raw = late.get("counterfactual_top_k")
    counterfactual = (
        list(counterfactual_raw)
        if isinstance(counterfactual_raw, list)
        else []
    )
    variants = {
        "baseline": _variant_record(
            baseline,
            requested_depth=requested_depth,
            corpus_size=corpus_size,
        )
    }
    if status in SCORED_STATUSES and counterfactual:
        variants["lfm_rerank"] = _variant_record(
            counterfactual,
            requested_depth=requested_depth,
            corpus_size=corpus_size,
        )
    return {
        "query_id": query_id,
        "variants": variants,
        "late_interaction": {
            "status": status,
            "coverage": float(late.get("coverage") or 0.0),
            "latency_ms": float(late.get("latency_ms") or 0.0),
            "model_revision": str(late.get("model_revision") or ""),
            "index_revision": str(late.get("index_revision") or ""),
            "reason": str(late.get("reason") or ""),
        },
    }


async def run(args: argparse.Namespace) -> int:
    if not settings.LATE_INTERACTION_REMOTE_ENABLED:
        raise RuntimeError("LATE_INTERACTION_REMOTE_ENABLED=true is required")
    if not settings.LATE_INTERACTION_ENABLED:
        raise RuntimeError("LATE_INTERACTION_ENABLED=true is required")
    if not settings.LATE_INTERACTION_SHADOW_ENABLED:
        raise RuntimeError("LATE_INTERACTION_SHADOW_ENABLED=true is required")
    if settings.LATE_INTERACTION_RERANK_ENABLED:
        raise RuntimeError("disable active LATE_INTERACTION_RERANK_ENABLED for paired shadow capture")
    expected_index_revision = require_exact_evaluation_revision()

    # Evaluation parity: no classification/rerank LLM and therefore no hidden
    # second model call or token spend in either arm.
    settings.RETRIEVAL_USE_LLM_CLASSIFICATION = False
    settings.RETRIEVAL_USE_LLM_RERANK = False
    await init_db()
    repo_path = Path(args.repo).resolve()
    repository = await get_repository_by_path(repo_path)
    if repository is None:
        raise LookupError(f"repository is not indexed: {repo_path}")
    async with async_session_factory() as session:
        corpus_size = int(
            (
                await session.execute(
                    select(func.count(File.id)).where(
                        File.repository_id == repository.id
                    )
                )
            ).scalar_one()
        )

    dataset = load_dataset(args.base_dataset, args.extension_dataset)
    classifier = ContextPackBuilder()
    pipeline = HybridRetrievalPipeline()
    records: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    validated_index = None
    try:
        validated_index = await validate_remote_late_interaction_release()
        if validated_index is None:
            raise RuntimeError("exact paired capture requires active release validation")
        if validated_index.repository_id != repository.id:
            raise RuntimeError(
                "approved late-interaction repository does not match the evaluated repo"
            )
        # Checked-in fixtures are never production traffic. Keep their
        # rankings in the output artifact without contaminating durable shadow
        # evidence, and restore the process-global setting even on failure.
        with fixture_shadow_persistence_disabled():
            for entry in dataset:
                task_type, keywords, _risks, _features = await classifier._classify_task(
                    entry.question
                )
                result = await pipeline.run(
                    task_description=entry.question,
                    task_type=task_type,
                    keywords=keywords,
                    repository_id=repository.id,
                    repository_name=repository.name,
                    file_limit=args.file_limit,
                )
                record = result_record(
                    entry.query_id,
                    result,
                    requested_depth=args.file_limit,
                    corpus_size=corpus_size,
                )
                records.append(record)
                status = record["late_interaction"]["status"]
                reasons: list[str] = []
                if status not in SCORED_STATUSES:
                    reasons.append(
                        record["late_interaction"]["reason"] or f"status={status}"
                    )
                observed_index_revision = record["late_interaction"][
                    "index_revision"
                ]
                if observed_index_revision != expected_index_revision:
                    reasons.append(
                        "index revision "
                        f"{observed_index_revision or 'missing'} != "
                        f"{expected_index_revision}"
                    )
                if "lfm_rerank" not in record["variants"]:
                    reasons.append("missing scored lfm_rerank variant")
                for variant, payload in record["variants"].items():
                    depth_status = payload["depth"]["status"]
                    if depth_status != "sufficient":
                        reasons.append(f"{variant} depth evidence={depth_status}")
                if reasons:
                    failed.append(
                        {
                            "query_id": entry.query_id,
                            "status": status,
                            "reason": "; ".join(reasons),
                        }
                    )
    finally:
        await close_late_interaction_client()
        await close_database_connections()

    assert validated_index is not None
    release_info = build_info()
    payload = {
        "schema_version": 2,
        "repository": {
            "id": repository.id,
            "path": repository.path,
            "last_indexed_commit": repository.last_indexed_commit,
        },
        "complete": not failed and len(records) == len(dataset),
        "failures": failed,
        "release_binding": {
            "repository_id": repository.id,
            "build_sha": release_info["build_sha"],
            "source_digest": release_info["source_digest"],
            "model_revision": validated_index.model_revision,
            "index_revision": validated_index.index_revision,
            "lineage_id": validated_index.lineage_id,
            "identity_digest": validated_index.identity_digest,
            "document_count": validated_index.document_count,
        },
        "evidence": {
            "requested_depth": args.file_limit,
            "minimum_recall_at_50_depth": MIN_RECALL_50_DEPTH,
            "corpus_size": corpus_size,
            "expected_index_revision": expected_index_revision,
            "expected_lineage_id": settings.LATE_INTERACTION_APPROVED_LINEAGE_ID,
            "observed_lineage_id": validated_index.lineage_id,
            "expected_identity_digest": (
                settings.LATE_INTERACTION_APPROVED_IDENTITY_DIGEST
            ),
            "observed_identity_digest": validated_index.identity_digest,
            "expected_document_count": (
                settings.LATE_INTERACTION_APPROVED_DOCUMENT_COUNT
            ),
            "observed_document_count": validated_index.document_count,
            "observed_index_revisions": sorted(
                {
                    record["late_interaction"]["index_revision"]
                    for record in records
                    if record["late_interaction"]["index_revision"]
                }
            ),
            "variant_query_counts": {
                variant: sum(
                    1 for record in records if variant in record["variants"]
                )
                for variant in ("baseline", "lfm_rerank")
            },
            "depth_50_eligible_counts": {
                variant: sum(
                    1
                    for record in records
                    if record["variants"].get(variant, {})
                    .get("depth", {})
                    .get("status")
                    == "sufficient"
                )
                for variant in ("baseline", "lfm_rerank")
            },
        },
        "results": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"paired-shadow-capture queries={len(records)} failures={len(failed)} "
        f"output={args.output}"
    )
    return 0 if payload["complete"] else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument(
        "--base-dataset",
        type=Path,
        default=Path(__file__).with_name("golden_set.json"),
    )
    parser.add_argument(
        "--extension-dataset",
        type=Path,
        default=Path(__file__).with_name("lfm_eval_extension.json"),
    )
    parser.add_argument("--file-limit", type=int, default=50, choices=range(10, 51))
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
