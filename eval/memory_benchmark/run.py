"""Memory-system effectiveness benchmark.

Measures whether the layered memory (L1 episodic → L2 consolidation → L3
learnings) actually helps, with real numbers:

  retrieval   — plant N learnings, ask questions, measure recall@k / MRR
                of query-ranked learning retrieval
  dedup       — plant near-duplicate pairs, run the G1 gate, measure
                duplicate-detection rate
  concurrency — M concurrent agents writing + reading; measure lost
                writes, errors, and double-promotion under concurrent
                consolidation runs
  overhead    — latency of learning retrieval at 0 / 50 / 200 learnings,
                token estimate of the injected learnings block

Usage:
  python eval/memory_benchmark/run.py [--scenario retrieval] [--agents 10]
                                      [--shared-db]

Writes results to eval/memory_benchmark/results/memory_bench_<stamp>.json.

Isolation: by default every scenario runs against a scratch Postgres
database (created from the real migrations, dropped afterwards), so planted
learnings can never leak into the production L3 that /ask reads.
``--shared-db`` restores the old behavior (write to the configured DB and
reject planted learnings afterwards) for environments where CREATE
DATABASE is unavailable — a crash can then leave benchmark rows behind.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "eval"))

from bench_db import isolated_bench_db  # noqa: E402
from brain.context.context_pack_builder import _load_active_learnings  # noqa: E402
from brain.memory.consolidation import (  # noqa: E402
    ConsolidationCandidate,
    GateOutcome,
    evaluate_gates,
)
from brain.memory.learning_store import LearningStore  # noqa: E402

BENCH_CATEGORY = "membench"


# ---------------------------------------------------------------- metrics

def recall_at_k(relevant_ids: set[int], ranked_ids: list[int], k: int) -> float:
    if not relevant_ids:
        return 1.0
    return len(set(ranked_ids[:k]) & relevant_ids) / len(relevant_ids)


def reciprocal_rank(relevant_ids: set[int], ranked_ids: list[int]) -> float:
    for i, lid in enumerate(ranked_ids, 1):
        if lid in relevant_ids:
            return 1.0 / i
    return 0.0


# ---------------------------------------------------------------- helpers

async def cleanup(ids: list[int]) -> None:
    for lid in ids:
        try:
            await LearningStore.reject(lid)
        except Exception:
            pass


async def plant(statement: str, **kw) -> int:
    kw.setdefault("category", BENCH_CATEGORY)
    return await LearningStore.add_learning(statement, **kw)


# ---------------------------------------------------------------- scenario: retrieval

RETRIEVAL_CORPUS = [
    # (statement, category)
    ("The embeddings dimension for the NVIDIA model is 2048.", "config"),
    ("PostgreSQL runs on port 5433 in docker-compose, 5432 natively.", "config"),
    ("The /health endpoint is a shallow liveness ping; /ready checks all services.", "api"),
    ("Diff-review must never approve unparseable LLM JSON; mark needs_review.", "review"),
    ("Impact analysis filters SQL fragments and prose noise from file lists.", "review"),
    ("Neo4j graph is derived from the indexed repo; rebuild via reindex.", "ops"),
    ("Nightly maintenance runs reindex, embedding repair, and quality probes.", "ops"),
    ("Memory consolidation is best-effort inside nightly maintenance.", "memory"),
    ("Learnings are ranked by embedding cosine similarity to the query.", "memory"),
    ("G1 dedup gate links candidates similar to existing learnings.", "memory"),
    ("The benchmark CI job uses mock providers and skips review tasks.", "ci"),
    ("Regression gate fails on task errors and overall score below floor.", "ci"),
    ("bootstrap.sh creates venv, .env, and starts docker services.", "onboarding"),
    ("brain doctor is the preflight check after bootstrap.", "onboarding"),
    ("Retry budget for LLM calls is LLM_MAX_RETRIES with exponential backoff.", "reliability"),
    ("Daily backup cron dumps postgres to ~/workspace/backups.", "reliability"),
    ("Restore drops and recreates brain_db, then reindexes for neo4j.", "reliability"),
    ("The demo page documents three real bugs Brain found in itself.", "demo"),
    ("Context packs cap learnings at 10, /ask at 12.", "memory"),
    ("Supersede learnings instead of deleting; the chain is the audit trail.", "memory"),
]

RETRIEVAL_QUESTIONS = [
    # (question, keywords that identify the relevant learnings)
    ("What embedding dimension does the NVIDIA model use?", ["2048"]),
    ("Which port is postgres on?", ["5433"]),
    ("How do I check if the API is fully ready?", ["/ready"]),
    ("What should diff-review do with unparseable JSON?", ["needs_review"]),
    ("How is the neo4j graph recovered after data loss?", ["reindex"]),
    ("How are learnings ranked for a query?", ["cosine similarity"]),
    ("What does the G1 gate do?", ["dedup"]),
    ("How do I set up a new machine quickly?", ["bootstrap.sh"]),
    ("What controls LLM retry behavior?", ["LLM_MAX_RETRIES"]),
    ("Where do backups go?", ["~/workspace/backups"]),
]


async def scenario_retrieval() -> dict:
    ids: list[int] = []
    try:
        for statement, cat in RETRIEVAL_CORPUS:
            ids.append(await plant(statement, category=f"{BENCH_CATEGORY}-{cat}"))
        # map keyword -> learning ids whose statement contains it
        await asyncio.sleep(0.2)
        recalls, rrs = [], []
        details = []
        for question, keywords in RETRIEVAL_QUESTIONS:
            relevant = {
                lid for lid, (stmt, _) in zip(ids, RETRIEVAL_CORPUS)
                if any(kw.lower() in stmt.lower() for kw in keywords)
            }
            ranked = await _load_active_learnings(None, query=question, limit=10)
            # restrict to our planted ids for a fair measurement
            ranked_ids = [row.id for row in ranked if row.id in set(ids)]
            r5 = recall_at_k(relevant, ranked_ids, 5)
            rr = reciprocal_rank(relevant, ranked_ids)
            recalls.append(r5)
            rrs.append(rr)
            details.append({"q": question[:50], "recall@5": round(r5, 2), "rr": round(rr, 2)})
        return {
            "n_learnings": len(ids),
            "n_questions": len(RETRIEVAL_QUESTIONS),
            "mean_recall@5": round(sum(recalls) / len(recalls), 3),
            "mean_reciprocal_rank": round(sum(rrs) / len(rrs), 3),
            "details": details,
        }
    finally:
        await cleanup(ids)


# ---------------------------------------------------------------- scenario: dedup

DEDUP_PAIRS = [
    ("The embeddings dimension is 2048.",
     "Embeddings use dimension 2048."),
    ("Never approve unparseable LLM JSON in diff-review.",
     "Diff-review must not approve when LLM JSON cannot be parsed."),
    ("Postgres runs on port 5433 in docker compose.",
     "In docker-compose, postgres listens on 5433."),
    ("Backups go to ~/workspace/backups daily at 03:38.",
     "The daily backup cron writes to ~/workspace/backups."),
    ("Reindex rebuilds the neo4j graph from scratch.",
     "The neo4j graph is rebuilt by running reindex."),
]


async def scenario_dedup() -> dict:
    ids: list[int] = []
    try:
        caught = 0
        details = []
        for original, duplicate in DEDUP_PAIRS:
            oid = await plant(original)
            ids.append(oid)
            cand = ConsolidationCandidate(
                statement=duplicate,
                evidence=[{"e": 1}, {"e": 2}, {"e": 3}],
            )
            result = await evaluate_gates(cand)
            is_dup = result.outcome == GateOutcome.LINK_DUPLICATE
            caught += is_dup
            details.append({"dup": duplicate[:45], "caught": is_dup,
                            "reason": result.reasons[0] if result.reasons else ""})
        return {
            "n_pairs": len(DEDUP_PAIRS),
            "duplicates_caught": caught,
            "dedup_rate": round(caught / len(DEDUP_PAIRS), 3),
            "details": details,
        }
    finally:
        await cleanup(ids)


# ---------------------------------------------------------------- scenario: concurrency

async def _agent_work(agent_idx: int, n_writes: int, n_reads: int) -> dict:
    ids: list[int] = []
    errors: list[str] = []
    try:
        for w in range(n_writes):
            try:
                lid = await plant(
                    f"Agent {agent_idx} learning {w}: concurrent write probe.",
                    confidence=0.5,
                )
                ids.append(lid)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"write: {type(exc).__name__}")
        for r in range(n_reads):
            try:
                await _load_active_learnings(None, query=f"agent {agent_idx} probe {r}", limit=5)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"read: {type(exc).__name__}")
    finally:
        await cleanup(ids)
    return {"writes": len(ids), "errors": errors}


async def scenario_concurrency(n_agents: int = 10) -> dict:
    t0 = time.time()
    results = await asyncio.gather(*[_agent_work(i, 5, 3) for i in range(n_agents)])
    dt = time.time() - t0
    total_writes = sum(r["writes"] for r in results)
    total_errors = sum(len(r["errors"]) for r in results)
    # verify no residue: all membench learnings should be rejected
    remaining = await LearningStore.list_active_learnings()
    residue = [row for row in remaining if (row.category or "").startswith(BENCH_CATEGORY)]
    return {
        "n_agents": n_agents,
        "writes_per_agent": 5,
        "reads_per_agent": 3,
        "expected_writes": n_agents * 5,
        "actual_writes": total_writes,
        "lost_writes": n_agents * 5 - total_writes,
        "errors": total_errors,
        "residue_learnings": len(residue),
        "duration_s": round(dt, 1),
    }


async def scenario_concurrent_consolidation() -> dict:
    """Two consolidation runs racing over the same episodes.

    Tests the advisory-lock serialization in run_consolidation: the second
    concurrent run must skip instead of double-promoting.
    """
    from brain.memory.consolidation import run_consolidation

    r1, r2 = await asyncio.gather(
        run_consolidation(dry_run=True),
        run_consolidation(dry_run=True),
    )
    skipped = sum(1 for r in (r1, r2) if r.get("status") == "skipped")
    return {
        "run1_status": r1.get("status", "completed"),
        "run2_status": r2.get("status", "completed"),
        "runs_skipped": skipped,
        "serialized": skipped >= 1,
        "note": "Advisory lock serializes concurrent consolidation runs; "
                "the loser skips instead of racing through G1.",
    }


# ---------------------------------------------------------------- scenario: overhead

async def scenario_overhead() -> dict:
    ids: list[int] = []
    try:
        # baseline: measure with only pre-existing learnings
        t0 = time.time()
        base = await _load_active_learnings(None, query="probe", limit=10)
        base_ms = (time.time() - t0) * 1000

        for i in range(100):
            ids.append(await plant(f"Overhead probe learning number {i}."))

        t0 = time.time()
        many = await _load_active_learnings(None, query="probe", limit=10)
        many_ms = (time.time() - t0) * 1000

        # Real token count (cl100k_base) for the learnings block — not a
        # chars/4 estimate, which systematically overstates text blocks.
        block = "\n".join(f"- {row.statement}" for row in many[:10])
        import tiktoken

        tokens = len(tiktoken.get_encoding("cl100k_base").encode(block))
        return {
            "baseline_learnings": len(base),
            "learnings_ms_baseline": round(base_ms, 1),
            "learnings_ms_100": round(many_ms, 1),
            "tokens_per_ask": tokens,
        }
    finally:
        await cleanup(ids)


# ---------------------------------------------------------------- main

SCENARIOS = {
    "retrieval": scenario_retrieval,
    "dedup": scenario_dedup,
    "concurrency": scenario_concurrency,
    "double_promote": scenario_concurrent_consolidation,
    "overhead": scenario_overhead,
}


async def _run_scenarios(args) -> dict:
    chosen = [args.scenario] if args.scenario else list(SCENARIOS)
    results: dict = {}
    for name in chosen:
        print(f"[{name}] ...", flush=True)
        t0 = time.time()
        try:
            if name == "concurrency":
                out = await SCENARIOS[name](n_agents=args.agents)
            else:
                out = await SCENARIOS[name]()
            out["status"] = "ok"
        except Exception as exc:  # noqa: BLE001
            out = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        out["duration_s"] = round(time.time() - t0, 1)
        results[name] = out
        print(f"  -> {json.dumps(out)[:220]}", flush=True)
    return results


async def amain() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default=None, choices=list(SCENARIOS),
                    help="run one scenario (default: all)")
    ap.add_argument("--agents", type=int, default=10)
    ap.add_argument("--output", default=None)
    ap.add_argument("--shared-db", action="store_true",
                    help="write to the configured database instead of a scratch "
                         "one (old behavior; a crash can leak benchmark learnings)")
    args = ap.parse_args()

    if args.shared_db:
        print("[isolation] --shared-db: writing to the configured database")
        results = await _run_scenarios(args)
        isolation = "shared"
    else:
        async with isolated_bench_db() as bench:
            print(f"[isolation] scratch database: {bench.db_name}")
            results = await _run_scenarios(args)
        isolation = "scratch-db (dropped)"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = args.output or os.path.join(HERE, "results", f"memory_bench_{stamp}.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"isolation": isolation, "results": results}, f, indent=2)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    asyncio.run(amain())
