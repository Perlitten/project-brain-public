#!/usr/bin/env python3
"""Compare PyLate BF16 and remote/GGUF late-interaction rankings.

The default/recommended CI path compares two precomputed ranking files and has
no heavy dependencies.  A GPU experiment can instead opt into the lazy PyLate
reference adapter and/or the existing remote LFM provider.  Merely importing
this module never imports torch, transformers, or PyLate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ParityQuery:
    query_id: str
    text: str
    relevant_document_ids: tuple[str, ...]


@dataclass(frozen=True)
class ParityDocument:
    document_id: str
    text: str


@dataclass(frozen=True)
class ParityCorpus:
    queries: tuple[ParityQuery, ...]
    documents: tuple[ParityDocument, ...]


def load_corpus(path: Path) -> ParityCorpus:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("parity corpus requires schema_version=1")
    raw_queries = payload.get("queries")
    raw_documents = payload.get("documents")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise ValueError("parity corpus queries[] must be non-empty")
    if not isinstance(raw_documents, list) or not raw_documents:
        raise ValueError("parity corpus documents[] must be non-empty")

    documents = tuple(
        ParityDocument(
            document_id=_text(item, "id", 255),
            text=_text(item, "text", 100_000),
        )
        for item in raw_documents
        if isinstance(item, dict)
    )
    document_ids = [document.document_id for document in documents]
    if len(document_ids) != len(set(document_ids)):
        raise ValueError("parity corpus document ids must be unique")
    known_documents = set(document_ids)

    queries: list[ParityQuery] = []
    seen_queries: set[str] = set()
    for item in raw_queries:
        if not isinstance(item, dict):
            raise ValueError("parity queries must be objects")
        query_id = _text(item, "id", 255)
        if query_id in seen_queries:
            raise ValueError(f"duplicate parity query id: {query_id}")
        seen_queries.add(query_id)
        qrels = item.get("relevant_document_ids")
        if not isinstance(qrels, list) or not qrels:
            raise ValueError(f"{query_id}: relevant_document_ids must be non-empty")
        relevant = tuple(_bounded_text(value, "relevant_document_ids", 255) for value in qrels)
        unknown = sorted(set(relevant) - known_documents)
        if unknown:
            raise ValueError(f"{query_id}: unknown relevant document ids: {unknown}")
        queries.append(
            ParityQuery(
                query_id=query_id,
                text=_text(item, "text", 10_000),
                relevant_document_ids=relevant,
            )
        )
    return ParityCorpus(queries=tuple(queries), documents=documents)


def _text(item: dict[str, Any], key: str, max_length: int) -> str:
    return _bounded_text(item.get(key), key, max_length)


def _bounded_text(value: object, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    text = value.strip()
    if len(text) > max_length:
        raise ValueError(f"{field} exceeds {max_length} characters")
    return text


def load_rankings(path: Path) -> dict[str, list[dict[str, float | str]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("rankings") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise ValueError("rankings must be an array or an object with rankings[]")
    rankings: dict[str, list[dict[str, float | str]]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("ranking records must be objects")
        query_id = _text(record, "query_id", 255)
        if query_id in rankings:
            raise ValueError(f"duplicate ranking query_id: {query_id}")
        raw_ranking = record.get("ranking")
        if not isinstance(raw_ranking, list):
            raise ValueError(f"{query_id}: ranking must be an array")
        ranking: list[dict[str, float | str]] = []
        seen: set[str] = set()
        for item in raw_ranking:
            if not isinstance(item, dict):
                raise ValueError(f"{query_id}: ranking items must be objects")
            document_id = _text(item, "id", 255)
            if document_id in seen:
                continue
            seen.add(document_id)
            score = float(item.get("score", 0.0))
            if not math.isfinite(score):
                raise ValueError(f"{query_id}/{document_id}: score must be finite")
            ranking.append({"id": document_id, "score": score})
        rankings[query_id] = ranking
    return rankings


def save_rankings(
    path: Path,
    rankings: dict[str, list[dict[str, float | str]]],
    *,
    runtime: str,
    revision: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "runtime": runtime,
        "revision": revision,
        "rankings": [
            {"query_id": query_id, "ranking": ranking}
            for query_id, ranking in sorted(rankings.items())
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def rank_with_pylate(
    corpus: ParityCorpus,
    *,
    model_name: str,
    batch_size: int = 16,
    device: str = "cuda",
) -> dict[str, list[dict[str, float | str]]]:
    """Run the BF16 reference adapter; imports heavy dependencies lazily."""
    try:
        import numpy as np
        import torch
        from pylate import models
    except ImportError as exc:
        raise RuntimeError(
            "PyLate parity generation needs optional packages; install pylate "
            "and a BF16-capable torch build in the isolated GPU environment"
        ) from exc

    from brain.late_interaction.codec import maxsim_score

    model = models.ColBERT(model_name_or_path=model_name)
    model = model.to(device=device, dtype=torch.bfloat16)
    model.eval()
    query_embeddings = model.encode(
        [query.text for query in corpus.queries],
        batch_size=batch_size,
        is_query=True,
        show_progress_bar=True,
    )
    document_embeddings = model.encode(
        [document.text for document in corpus.documents],
        batch_size=batch_size,
        is_query=False,
        show_progress_bar=True,
    )
    document_matrices = [_as_numpy(value, np) for value in document_embeddings]
    rankings: dict[str, list[dict[str, float | str]]] = {}
    for query, raw_query_matrix in zip(corpus.queries, query_embeddings):
        query_matrix = _as_numpy(raw_query_matrix, np)
        scored: list[dict[str, float | str]] = [
            {
                "id": document.document_id,
                "score": float(maxsim_score(query_matrix, document_matrix)),
            }
            for document, document_matrix in zip(corpus.documents, document_matrices)
        ]
        rankings[query.query_id] = sorted(
            scored,
            key=lambda item: (-float(item["score"]), str(item["id"])),
        )
    return rankings


def _as_numpy(value: object, np: Any) -> Any:
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "numpy"):
        value = value.numpy()
    matrix = np.asarray(value, dtype=np.float32)
    if matrix.ndim != 2:
        raise ValueError(f"runtime returned shape {matrix.shape}, expected token matrix")
    return matrix


async def rank_with_remote(
    corpus: ParityCorpus,
    *,
    base_url: str,
) -> dict[str, list[dict[str, float | str]]]:
    """Run the GGUF/remote adapter through the production provider contract."""
    from brain.late_interaction.codec import maxsim_score
    from brain.late_interaction.provider import LfmColbertProvider

    provider = LfmColbertProvider(base_url=base_url)
    try:
        document_embeddings = [
            await provider.embed(document.text, is_query=False)
            for document in corpus.documents
        ]
        rankings: dict[str, list[dict[str, float | str]]] = {}
        for query in corpus.queries:
            query_embedding = await provider.embed(query.text, is_query=True)
            scored: list[dict[str, float | str]] = [
                {
                    "id": document.document_id,
                    "score": float(
                        maxsim_score(query_embedding.vectors, document_embedding.vectors)
                    ),
                }
                for document, document_embedding in zip(corpus.documents, document_embeddings)
            ]
            rankings[query.query_id] = sorted(
                scored,
                key=lambda item: (-float(item["score"]), str(item["id"])),
            )
        return rankings
    finally:
        await provider.aclose()


def evaluate_parity(
    corpus: ParityCorpus,
    reference: dict[str, list[dict[str, float | str]]],
    candidate: dict[str, list[dict[str, float | str]]],
    *,
    top_k: int = 10,
    overlap_min: float = 0.90,
    spearman_min: float = 0.95,
    kendall_min: float = 0.90,
    ndcg_abs_delta_max: float = 0.02,
) -> dict[str, Any]:
    if top_k < 1:
        raise ValueError("top_k must be positive")
    expected_ids = {query.query_id for query in corpus.queries}
    for label, rankings in (("reference", reference), ("candidate", candidate)):
        missing = sorted(expected_ids - rankings.keys())
        unknown = sorted(rankings.keys() - expected_ids)
        if missing:
            raise ValueError(f"{label} rankings missing query ids: {missing[:5]}")
        if unknown:
            raise ValueError(f"{label} rankings contain unknown query ids: {unknown[:5]}")

    per_query: list[dict[str, Any]] = []
    for query in corpus.queries:
        ref_ids = [str(item["id"]) for item in reference[query.query_id]]
        candidate_ids = [str(item["id"]) for item in candidate[query.query_id]]
        overlap = _top_k_overlap(ref_ids, candidate_ids, top_k)
        union = list(dict.fromkeys(ref_ids[:top_k] + candidate_ids[:top_k]))
        ref_ranks = _rank_vector(ref_ids, union, top_k)
        candidate_ranks = _rank_vector(candidate_ids, union, top_k)
        ref_ndcg = _ndcg_at(ref_ids, query.relevant_document_ids, top_k)
        candidate_ndcg = _ndcg_at(candidate_ids, query.relevant_document_ids, top_k)
        per_query.append(
            {
                "query_id": query.query_id,
                f"top_{top_k}_overlap": round(overlap, 6),
                "spearman": round(_pearson(ref_ranks, candidate_ranks), 6),
                "kendall": round(_kendall_tau_b(ref_ranks, candidate_ranks), 6),
                "reference_ndcg": round(ref_ndcg, 6),
                "candidate_ndcg": round(candidate_ndcg, 6),
                "ndcg_delta": round(candidate_ndcg - ref_ndcg, 6),
            }
        )

    overlap_key = f"top_{top_k}_overlap"
    summary = {
        "count": len(per_query),
        "mean_top_k_overlap": round(
            statistics.fmean(item[overlap_key] for item in per_query),
            6,
        ),
        "min_top_k_overlap": round(min(item[overlap_key] for item in per_query), 6),
        "mean_spearman": round(statistics.fmean(item["spearman"] for item in per_query), 6),
        "mean_kendall": round(statistics.fmean(item["kendall"] for item in per_query), 6),
        "mean_ndcg_delta": round(statistics.fmean(item["ndcg_delta"] for item in per_query), 6),
        "max_abs_ndcg_delta": round(max(abs(item["ndcg_delta"]) for item in per_query), 6),
    }
    checks = {
        "mean_top_k_overlap": summary["mean_top_k_overlap"] >= overlap_min,
        "mean_spearman": summary["mean_spearman"] >= spearman_min,
        "mean_kendall": summary["mean_kendall"] >= kendall_min,
        "max_abs_ndcg_delta": summary["max_abs_ndcg_delta"] <= ndcg_abs_delta_max,
    }
    return {
        "schema_version": 1,
        "top_k": top_k,
        "summary": summary,
        "gate": {
            "passed": all(checks.values()),
            "checks": checks,
            "thresholds": {
                "overlap_min": overlap_min,
                "spearman_min": spearman_min,
                "kendall_min": kendall_min,
                "ndcg_abs_delta_max": ndcg_abs_delta_max,
            },
        },
        "per_query": per_query,
    }


def _top_k_overlap(reference: list[str], candidate: list[str], k: int) -> float:
    denominator = min(k, len(reference), len(candidate))
    if denominator == 0:
        return 0.0
    return len(set(reference[:k]) & set(candidate[:k])) / denominator


def _rank_vector(ranking: list[str], items: list[str], k: int) -> list[float]:
    positions = {item: index for index, item in enumerate(ranking[:k], start=1)}
    return [float(positions.get(item, k + 1)) for item in items]


def _pearson(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    left_ss = sum((x - left_mean) ** 2 for x in left)
    right_ss = sum((y - right_mean) ** 2 for y in right)
    denominator = math.sqrt(left_ss * right_ss)
    if denominator == 0:
        return 1.0 if left == right else 0.0
    return numerator / denominator


def _kendall_tau_b(left: list[float], right: list[float]) -> float:
    concordant = discordant = ties_left = ties_right = 0
    for i in range(len(left)):
        for j in range(i + 1, len(left)):
            left_sign = _sign(left[i] - left[j])
            right_sign = _sign(right[i] - right[j])
            if left_sign == 0 and right_sign == 0:
                continue
            if left_sign == 0:
                ties_left += 1
            elif right_sign == 0:
                ties_right += 1
            elif left_sign == right_sign:
                concordant += 1
            else:
                discordant += 1
    denominator = math.sqrt(
        (concordant + discordant + ties_left)
        * (concordant + discordant + ties_right)
    )
    if denominator == 0:
        return 1.0 if left == right else 0.0
    return (concordant - discordant) / denominator


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


def _ndcg_at(ranking: list[str], relevant: tuple[str, ...], k: int) -> float:
    relevant_set = set(relevant)
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, document_id in enumerate(ranking[:k], start=1)
        if document_id in relevant_set
    )
    ideal_hits = min(len(relevant_set), k)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    reference = parser.add_mutually_exclusive_group(required=True)
    reference.add_argument("--reference-rankings", type=Path)
    reference.add_argument("--pylate-model")
    candidate = parser.add_mutually_exclusive_group(required=True)
    candidate.add_argument("--candidate-rankings", type=Path)
    candidate.add_argument("--remote-url")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--pylate-device", default="cuda")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument(
        "--release-binding",
        type=Path,
        help=(
            "JSON object (or object containing release_binding) copied into "
            "the parity artifact for production approval"
        ),
    )
    parser.add_argument("--export-reference", type=Path)
    parser.add_argument("--export-candidate", type=Path)
    parser.add_argument("--overlap-min", type=float, default=0.90)
    parser.add_argument("--spearman-min", type=float, default=0.95)
    parser.add_argument("--kendall-min", type=float, default=0.90)
    parser.add_argument("--ndcg-abs-delta-max", type=float, default=0.02)
    return parser.parse_args()


async def main() -> int:
    args = _parse_args()
    corpus = load_corpus(args.corpus)
    if args.reference_rankings:
        reference = load_rankings(args.reference_rankings)
        reference_revision = str(args.reference_rankings)
    else:
        reference = rank_with_pylate(
            corpus,
            model_name=args.pylate_model,
            batch_size=args.batch_size,
            device=args.pylate_device,
        )
        try:
            pylate_version = version("pylate")
        except PackageNotFoundError:
            pylate_version = "unknown"
        reference_revision = (
            f"pylate={pylate_version};model={args.pylate_model};"
            f"dtype=bf16;device={args.pylate_device}"
        )
        if args.export_reference:
            save_rankings(
                args.export_reference,
                reference,
                runtime="pylate-bf16",
                revision=reference_revision,
            )

    if args.candidate_rankings:
        candidate = load_rankings(args.candidate_rankings)
        candidate_revision = str(args.candidate_rankings)
    else:
        candidate = await rank_with_remote(corpus, base_url=args.remote_url)
        candidate_revision = args.remote_url
        if args.export_candidate:
            save_rankings(
                args.export_candidate,
                candidate,
                runtime="remote-gguf",
                revision=args.remote_url,
            )

    report = evaluate_parity(
        corpus,
        reference,
        candidate,
        top_k=args.top_k,
        overlap_min=args.overlap_min,
        spearman_min=args.spearman_min,
        kendall_min=args.kendall_min,
        ndcg_abs_delta_max=args.ndcg_abs_delta_max,
    )
    report["runtime_revisions"] = {
        "reference": reference_revision,
        "candidate": candidate_revision,
    }
    if args.release_binding is not None:
        binding_payload = json.loads(
            args.release_binding.read_text(encoding="utf-8")
        )
        binding = (
            binding_payload.get("release_binding")
            if isinstance(binding_payload, dict)
            and "release_binding" in binding_payload
            else binding_payload
        )
        if not isinstance(binding, dict):
            raise ValueError("--release-binding must contain a JSON object")
        report["release_binding"] = binding
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        f"runtime-parity count={report['summary']['count']} "
        f"overlap={report['summary']['mean_top_k_overlap']:.4f} "
        f"spearman={report['summary']['mean_spearman']:.4f} "
        f"kendall={report['summary']['mean_kendall']:.4f} "
        f"max_abs_ndcg_delta={report['summary']['max_abs_ndcg_delta']:.4f} "
        f"gate={'PASS' if report['gate']['passed'] else 'FAIL'}"
    )
    return 0 if report["gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
