"""Benchmark the isolated LFM2.5-ColBERT llama.cpp canary.

Run through an SSH loopback tunnel; this script never changes Project Brain
routing or database state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

import numpy as np

from brain.late_interaction.codec import encode_matrix, maxsim_score
from brain.late_interaction.provider import LfmColbertProvider


CORRECTNESS_CASES = [
    (
        "Where is the PostgreSQL schema migration applied?",
        [
            "The API router renders the dashboard overview and accepts search requests.",
            "brain/database/migrations.py applies idempotent PostgreSQL schema migrations during init_db.",
            "The graph client creates repository-scoped Neo4j nodes and relationships.",
        ],
        1,
    ),
    (
        "How are stale worker jobs recovered?",
        [
            "The durable worker queue reaps expired leases and retries jobs within a bounded retry budget.",
            "CSS design tokens define spacing and color variables for the cockpit.",
            "The tokenizer creates one vector per document token.",
        ],
        0,
    ),
    (
        "Как отправляются предупреждения владельцу?",
        [
            "Self-diagnosis deduplicates findings and sends owner alerts through the Telegram bot.",
            "Neo4j stores dependency edges between symbols.",
            "File chunks are split with overlap before dense embedding.",
        ],
        0,
    ),
]

ACCEPTANCE_QUERY = (
    "Explain the production safeguards that prevent per-job PostgreSQL schema DDL "
    "and recover safely from malformed LLM self-diagnosis output."
)
ACCEPTANCE_MUST_HIT = {
    "brain/database/session.py": ("async def init_db", "pg_advisory_lock"),
    "brain/insights/proactive.py": ("def _parse_json_payload", "self-diagnosis"),
    "tests/test_database_resilience.py": ("init_db", "schema DDL"),
    "tests/test_proactive_insights.py": ("malformed", "self_diagnosis"),
}
ACCEPTANCE_DISTRACTORS = {
    "apps/api/routers/dashboard.py": ("dashboard",),
    "brain/graph/graph_client.py": ("class GraphClient",),
    "brain/search/code_search.py": ("search",),
    "tests/test_dashboard.py": ("dashboard",),
}


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percent)))
    return ordered[index]


def representative_chunks(root: Path, limit: int) -> list[str]:
    if limit <= 0:
        return []
    chunks: list[str] = []
    allowed = {".py", ".md", ".json", ".yml", ".yaml", ".toml"}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in allowed:
            continue
        if any(part in {".git", ".venv", "node_modules", "reports", "context_packs"} for part in path.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for start in range(0, len(lines), 40):
            text = "\n".join(lines[start : start + 50]).strip()
            if text:
                chunks.append(text)
            if len(chunks) >= limit:
                return chunks
    return chunks


def anchored_file_excerpt(root: Path, relative_path: str, anchors: tuple[str, ...]) -> str | None:
    path = root / relative_path
    if not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    lowered = [line.casefold() for line in lines]
    anchor_index = 0
    for anchor in anchors:
        match = next(
            (index for index, line in enumerate(lowered) if anchor.casefold() in line),
            None,
        )
        if match is not None:
            anchor_index = match
            break
    start = max(0, anchor_index - 20)
    end = min(len(lines), anchor_index + 35)
    return f"{relative_path}\n" + "\n".join(lines[start:end])


async def run(args: argparse.Namespace) -> dict:
    provider = LfmColbertProvider(
        base_url=args.base_url,
        model="LiquidAI/LFM2.5-ColBERT-350M-GGUF",
        model_revision="bc240003aba07253e261a8aaf0d2c9683318a967",
        dimension=128,
        timeout_s=args.timeout,
        query_max_tokens=32,
        document_max_tokens=512,
    )
    health = await provider.health()
    await provider.embed("warm up query", is_query=True)
    await provider.embed("warm up document", is_query=False)

    correctness: list[dict] = []
    query_latencies: list[float] = []
    document_latencies: list[float] = []
    benchmark_query_matrix: np.ndarray | None = None
    correctness_cases = [] if args.acceptance_only else CORRECTNESS_CASES
    for query, documents, expected in correctness_cases:
        started = time.perf_counter()
        query_matrix = await provider.embed(query, is_query=True)
        if benchmark_query_matrix is None:
            benchmark_query_matrix = query_matrix.vectors
        query_latencies.append((time.perf_counter() - started) * 1000)
        scores: list[float] = []
        for document in documents:
            started = time.perf_counter()
            document_matrix = await provider.embed(document, is_query=False)
            document_latencies.append((time.perf_counter() - started) * 1000)
            scores.append(maxsim_score(query_matrix.vectors, document_matrix.vectors))
        predicted = int(np.argmax(scores))
        correctness.append(
            {
                "query": query,
                "expected": expected,
                "predicted": predicted,
                "pass": predicted == expected,
                "scores": [round(score, 4) for score in scores],
            }
        )

    corpus_root = Path(args.corpus_dir).resolve()
    acceptance_candidates = {**ACCEPTANCE_MUST_HIT, **ACCEPTANCE_DISTRACTORS}
    acceptance_missing: list[str] = []
    acceptance_scores: dict[str, float] = {}
    acceptance_document_latencies: list[float] = []
    acceptance_started = time.perf_counter()
    acceptance_query = await provider.embed(ACCEPTANCE_QUERY, is_query=True)
    for path, anchors in acceptance_candidates.items():
        excerpt = anchored_file_excerpt(corpus_root, path, anchors)
        if excerpt is None:
            acceptance_missing.append(path)
            continue
        started = time.perf_counter()
        document_encoded = await provider.embed(excerpt, is_query=False)
        acceptance_document_latencies.append((time.perf_counter() - started) * 1000)
        acceptance_scores[path] = maxsim_score(
            acceptance_query.vectors,
            document_encoded.vectors,
        )
    acceptance_ranking = [
        path
        for path, _score in sorted(
            acceptance_scores.items(),
            key=lambda item: item[1],
            reverse=True,
        )
    ]
    acceptance_top4 = acceptance_ranking[:4]
    acceptance_hits = len(set(acceptance_top4) & set(ACCEPTANCE_MUST_HIT))
    acceptance_latency_ms = (time.perf_counter() - acceptance_started) * 1000

    samples = representative_chunks(corpus_root, args.sample_chunks)
    sample_bytes: list[int] = []
    sample_tokens: list[int] = []
    sample_chars: list[int] = []
    sample_truncated = 0
    sample_latencies: list[float] = []
    sample_matrices: list[np.ndarray] = []
    for text in samples:
        started = time.perf_counter()
        encoded = await provider.embed(text, is_query=False)
        sample_latencies.append((time.perf_counter() - started) * 1000)
        sample_matrices.append(encoded.vectors)
        sample_bytes.append(len(encode_matrix(encoded.vectors)))
        sample_tokens.append(encoded.token_count)
        sample_chars.append(len(text))
        sample_truncated += int(encoded.truncated)

    average_bytes = statistics.mean(sample_bytes) if sample_bytes else 0.0
    projected_bytes = int(average_bytes * args.production_chunks)
    maxsim_latencies: list[float] = []
    if benchmark_query_matrix is not None and sample_matrices:
        top_k_matrices = [
            sample_matrices[index % len(sample_matrices)]
            for index in range(args.maxsim_candidates)
        ]
        for _ in range(args.maxsim_runs):
            started = time.perf_counter()
            for matrix in top_k_matrices:
                maxsim_score(benchmark_query_matrix, matrix)
            maxsim_latencies.append((time.perf_counter() - started) * 1000)
    payload = {
        "health": health,
        "correctness": {
            "passed": sum(int(case["pass"]) for case in correctness),
            "total": len(correctness),
            "cases": correctness,
        },
        "acceptance_quality_comparison": {
            "query": ACCEPTANCE_QUERY,
            "must_hit": list(ACCEPTANCE_MUST_HIT),
            "missing_files": acceptance_missing,
            "ranking": [
                {"path": path, "score": round(acceptance_scores[path], 4)}
                for path in acceptance_ranking
            ],
            "precision_at_4": round(acceptance_hits / 4, 4),
            "recall_at_4": round(acceptance_hits / len(ACCEPTANCE_MUST_HIT), 4),
            "latency_ms": round(acceptance_latency_ms, 2),
            "document_p95_ms": round(
                percentile(acceptance_document_latencies, 0.95),
                2,
            ),
            "scope": (
                "anchored real-file ColBERT comparison over a fixed candidate set; "
                "not an end-to-end recall measurement"
            ),
            "dense_production_baseline": "PARTIAL_CONTEXT; all four must-hit files missed",
        },
        "latency_ms": {
            "query_p50": round(percentile(query_latencies, 0.50), 2),
            "query_p95": round(percentile(query_latencies, 0.95), 2),
            "document_p50": round(percentile(document_latencies, 0.50), 2),
            "document_p95": round(percentile(document_latencies, 0.95), 2),
            "representative_chunk_p50": round(percentile(sample_latencies, 0.50), 2),
            "representative_chunk_p95": round(percentile(sample_latencies, 0.95), 2),
            "maxsim_candidate_count": args.maxsim_candidates,
            "maxsim_batch_p50": round(percentile(maxsim_latencies, 0.50), 2),
            "maxsim_batch_p95": round(percentile(maxsim_latencies, 0.95), 2),
        },
        "representative_corpus": {
            "sample_chunks": len(samples),
            "sample_chars": sum(sample_chars),
            "average_token_vectors": round(statistics.mean(sample_tokens), 2) if sample_tokens else 0.0,
            "p95_token_vectors": percentile([float(v) for v in sample_tokens], 0.95),
            "truncated_chunks": sample_truncated,
            "average_float16_bytes_per_chunk": round(average_bytes, 2),
            "production_chunks": args.production_chunks,
            "projected_float16_bytes": projected_bytes,
            "projected_float16_gib": round(projected_bytes / (1024**3), 3),
        },
    }
    await provider.aclose()
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18089")
    parser.add_argument("--corpus-dir", default=".")
    parser.add_argument("--sample-chunks", type=int, default=40)
    parser.add_argument("--production-chunks", type=int, default=41304)
    parser.add_argument("--maxsim-candidates", type=int, default=50)
    parser.add_argument("--maxsim-runs", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--acceptance-only", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    payload = asyncio.run(run(args))
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
