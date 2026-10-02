"""Small dependency-free latency probe for a llama.cpp ColBERT endpoint."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
import urllib.request
from typing import Any


def request_json(base_url: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def token_ids(base_url: str, text: str, *, add_special: bool) -> list[int]:
    payload = request_json(
        base_url,
        "/tokenize",
        {"content": text, "add_special": add_special, "with_pieces": False},
    )
    values = payload.get("tokens", [])
    result: list[int] = []
    for value in values:
        token = value.get("id") if isinstance(value, dict) else value
        if not isinstance(token, int):
            raise RuntimeError("llama.cpp returned invalid token IDs")
        result.append(token)
    if not result:
        raise RuntimeError("llama.cpp returned no token IDs")
    return result


def embedding(base_url: str, tokens: list[int], *, expected_dimension: int = 128) -> None:
    payload = request_json(base_url, "/embedding", {"content": tokens})
    matrix = payload[0]["embedding"]
    if len(matrix) != len(tokens):
        raise RuntimeError(f"unexpected token rows: {len(matrix)} != {len(tokens)}")
    for row in matrix:
        if len(row) != expected_dimension or any(not math.isfinite(float(value)) for value in row):
            raise RuntimeError("llama.cpp returned an invalid token embedding")


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def measure(base_url: str, text: str, *, is_query: bool, pad_token_id: int) -> float:
    prefix = "[Q] " if is_query else "[D] "
    max_tokens = 32 if is_query else 512
    started = time.perf_counter()
    tokens = token_ids(base_url, prefix + text, add_special=True)[:max_tokens]
    if is_query:
        tokens.extend([pad_token_id] * (max_tokens - len(tokens)))
    embedding(base_url, tokens)
    return (time.perf_counter() - started) * 1000


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--query-runs", type=int, default=5)
    parser.add_argument("--document-runs", type=int, default=2)
    args = parser.parse_args()

    pad_tokens = token_ids(args.base_url, "<|im_end|>", add_special=False)
    if len(pad_tokens) != 1:
        raise RuntimeError("pad token does not map to exactly one token")
    pad_token_id = pad_tokens[0]

    measure(args.base_url, "warm up query", is_query=True, pad_token_id=pad_token_id)
    query_latencies = [
        measure(
            args.base_url,
            "Where are repository-scoped database migrations applied?",
            is_query=True,
            pad_token_id=pad_token_id,
        )
        for _ in range(args.query_runs)
    ]
    document = " ".join(
        [
            "Project Brain applies repository-scoped migrations and validates durable worker state."
            for _ in range(24)
        ]
    )
    document_latencies = [
        measure(args.base_url, document, is_query=False, pad_token_id=pad_token_id)
        for _ in range(args.document_runs)
    ]
    print(
        json.dumps(
            {
                "query_runs": len(query_latencies),
                "query_p50_ms": round(statistics.median(query_latencies), 2),
                "query_p95_ms": round(percentile(query_latencies, 0.95), 2),
                "document_runs": len(document_latencies),
                "document_p50_ms": round(statistics.median(document_latencies), 2),
                "document_p95_ms": round(percentile(document_latencies, 0.95), 2),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
