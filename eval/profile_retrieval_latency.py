#!/usr/bin/env python3
"""Profile HybridRetrievalPipeline latency (no LLM plan)."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from brain.config.settings import settings
from brain.database.repository_utils import get_repository_by_path
from brain.database.session import init_db
from brain.retrieval.pipeline import HybridRetrievalPipeline


QUERIES = [
    "Fix pillz cost validation in battle engine",
    "Update round resolution flow",
    "Change card ability damage calculation",
    "Refactor clan bonus application",
    "Find dead code in API layer",
]


def _stats(vals: list[float]) -> dict:
    if not vals:
        return {"mean": 0, "median": 0, "p95": 0, "worst": 0}
    s = sorted(vals)
    p95_idx = min(len(s) - 1, int(len(s) * 0.95))
    return {
        "mean": round(sum(s) / len(s), 1),
        "median": round(s[len(s) // 2], 1),
        "p95": round(s[p95_idx], 1),
        "worst": round(s[-1], 1),
    }


async def _run(repo_path: Path, cold: bool) -> dict:
    repo = await get_repository_by_path(repo_path)
    pipeline = HybridRetrievalPipeline()
    latencies = []
    timings = []

    for q in QUERIES:
        if cold:
            pipeline = HybridRetrievalPipeline()
        t0 = time.perf_counter()
        result = await pipeline.run(
            task_description=q,
            task_type="bugfix",
            keywords=q.split()[:5],
            repository_id=repo.id if repo else None,
            repository_name=repo.name if repo else repo_path.name,
            file_limit=10,
        )
        latencies.append((time.perf_counter() - t0) * 1000)
        timings.append(result.timing.to_dict())

    return {"cold": cold, "latency_ms": _stats(latencies), "timing_buckets": timings}


async def main() -> int:
    await init_db()
    repo_path = Path(settings.TARGET_REPO_PATH)
    warm = await _run(repo_path, cold=False)
    cold = await _run(repo_path, cold=True)
    payload = {
        "repo": str(repo_path),
        "warm": warm,
        "cold": cold,
        "targets": {"warm_p95_ms": 5000, "cold_p95_ms": 15000},
        "gates": {
            "warm_p95_lt_5s": warm["latency_ms"]["p95"] < 5000,
            "cold_p95_lt_15s": cold["latency_ms"]["p95"] < 15000,
        },
    }
    out = PROJECT_ROOT / "reports" / "retrieval-latency-profile.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
