"""Memory-continuity eval: replays session-1 writes → session-N reads.

For each scenario in ``scenarios.yaml`` the runner:
  1. writes the declared memory artifacts (handoffs, open tasks, learnings,
     decisions) through the real store/write paths into a fresh sqlite DB,
  2. optionally injects ``filler_sessions`` worth of newer handoffs+tasks to
     simulate intervening work (rot axis),
  3. builds the same runtime context a new agent session receives
     (``_load_continuation`` + ``select_learnings_for_context`` +
     ``select_relevant_normative_memory``),
  4. measures whether the required facts surfaced.

Metrics written to ``metrics.json``:
  continuity_hit_rate      required reads whose facts all surfaced
  fact_survival            hit rate grouped by filler-session gap (rot curve)
  stale_surface_rate       demoted-marker decisions surfacing on off-topic reads
  context_bytes            bytes of the combined memory payload vs stripped
  per_scenario             full read-by-read breakdown

Exit code is non-zero when the expected-hit continuity rate drops below
``--min-hit-rate`` (default 0.8), so the harness works as a CI gate or a
manual regression check.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

import yaml
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# The script lives in eval/memory_continuity — make the repo root importable so
# `brain` resolves to this checkout, not an installed/foreign package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from brain.context import runtime_context_builder
from brain.database.harness_models import AgentTask, MemoryHandoff
from brain.database.models import Decision, Learning, Rule
from brain.memory import decision_store, learning_retrieval, learning_store, relevance, rule_store


async def _make_db(monkeypatch_factory, tmp: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp}", execution_options={"schema_translate_map": {"harness": None}}
    )
    async with engine.begin() as conn:
        for table in (Learning.__table__, AgentTask.__table__, MemoryHandoff.__table__,
                      Rule.__table__, Decision.__table__):
            await conn.run_sync(table.create)
    return async_sessionmaker(engine, expire_on_commit=False), engine


def _patch_factory(factory):
    for module in (learning_store, learning_retrieval, relevance, runtime_context_builder,
                   decision_store, rule_store):
        module.async_session_factory = factory


async def _write(scenario: dict, session) -> None:
    for task in scenario["writes"].get("tasks", []):
        session.add(AgentTask(title=task["title"], goal=task["title"],
                              status=task["status"], repo_path=scenario.get("repo", "global"),
                              priority=task.get("priority", "P2")))
    for handoff in scenario["writes"].get("handoffs", []):
        # Handoffs written through a task get repo scope; free-standing stay global.
        session.add(MemoryHandoff(session_id="eval-session", summary=handoff))
    await session.commit()


async def _write_stores(scenario: dict) -> None:
    for item in scenario["writes"].get("learnings", []):
        await learning_store.LearningStore.add_learning(
            statement=item["statement"], category=item.get("category", "eval"),
            confidence=item.get("confidence", 0.8),
            evidence=[{"kind": "agent_explicit", "source": "memory_continuity_eval"}],
            repo_scope=scenario.get("repo"), status="active")
    for item in scenario["writes"].get("decisions", []):
        await decision_store.DecisionStore.record_decision(
            title=item["title"], repo_path=scenario.get("repo"),
            description=item.get("description"))


async def _inject_filler(scenario: dict, factory, count: int) -> None:
    """Later sessions' handoffs. Explicit created_at keeps the recency window
    deterministic — server_default ties all writes to the same second."""
    from datetime import datetime, timedelta, timezone

    base = datetime.now(timezone.utc)
    async with factory() as session:
        for i in range(count):
            session.add(MemoryHandoff(session_id=f"filler-{i}",
                                      summary=f"filler work item {i} for {scenario['name']}",
                                      created_at=base + timedelta(seconds=i + 1)))
        await session.commit()


async def _read_context(task: str, repo: str | None, metadata: dict) -> tuple[str, int]:
    """Build the memory half of a runtime context; return (text, bytes)."""
    parts: list[object] = []
    continuation = await runtime_context_builder.RuntimeContextBuilder._load_continuation(repo, metadata)
    if continuation:
        parts.append(continuation)
    try:
        learnings = await asyncio.wait_for(
            learning_retrieval.select_learnings_for_context(task, repo, max_bytes=1200), timeout=5.0)
    except Exception:
        learnings = []
    normative = await relevance.select_relevant_normative_memory(task, [], repo)
    if normative:
        parts.append(normative)
    if learnings:
        parts.append({"learnings": learnings})
    text = json.dumps(parts, ensure_ascii=False, default=str)
    return text, len(text.encode("utf-8"))


async def run(scenarios_path: Path, metrics_path: Path, min_hit_rate: float) -> int:
    scenarios = yaml.safe_load(scenarios_path.read_text(encoding="utf-8"))["scenarios"]
    results: list[dict] = []
    expected_hits = expected_total = 0
    stale_surfaces = stale_opportunities = 0

    for scenario in scenarios:
        # Fresh DB per scenario: cross-scenario accumulation would pollute the
        # recency-window and repo-isolation measurements.
        with tempfile.TemporaryDirectory() as td:
            factory, engine = await _make_db(None, Path(td) / "eval.sqlite3")
            _patch_factory(factory)
            try:
                metadata: dict = {"missing": []}
                async with factory() as session:
                    await _write(scenario, session)
                await _write_stores(scenario)
                filler = int(scenario.get("filler_sessions", 0))
                await _inject_filler(scenario, factory, filler)

                for read in scenario.get("reads", []):
                    repo = read.get("repo_override", scenario.get("repo"))
                    text, nbytes = await _read_context(read["task"], repo, metadata)
                    folded = text.casefold()
                    hit = all(f.casefold() in folded for f in read.get("required_facts", []))
                    missing = [f for f in read.get("required_facts", []) if f.casefold() not in folded]
                    forbidden_hit = [f for f in read.get("forbidden_facts", []) if f.casefold() in folded]
                    expect = read.get("expect_hit", True)
                    ok = hit and not forbidden_hit if expect else not hit
                    if expect:
                        expected_total += 1
                        expected_hits += int(ok)
                    stale_surfaces += 1 if expect and any(
                        m in folded for m in ("release", "deploy", "checkpoint")) and \
                        not any(m in read["task"].casefold() for m in ("release", "deploy", "checkpoint")) else 0
                    stale_opportunities += 1 if expect else 0
                    results.append({
                        "scenario": scenario["name"], "task": read["task"], "repo": repo,
                        "expect_hit": expect, "hit": hit, "ok": ok,
                        "missing_facts": missing, "forbidden_surfaced": forbidden_hit,
                        "context_bytes": nbytes, "filler_sessions": filler,
                    })
            finally:
                await engine.dispose()

    hit_rate = expected_hits / expected_total if expected_total else 0.0
    survival: dict[int, list[bool]] = {}
    for r in results:
        if r["expect_hit"]:
            survival.setdefault(r["filler_sessions"], []).append(r["hit"])
    metrics = {
        "continuity_hit_rate": round(hit_rate, 4),
        "expected_reads": expected_total,
        "expected_hits": expected_hits,
        "fact_survival_by_filler": {str(k): round(sum(v) / len(v), 4) for k, v in sorted(survival.items())},
        "stale_surface_rate": round(stale_surfaces / stale_opportunities, 4) if stale_opportunities else 0.0,
        "context_bytes": {r["scenario"]: r["context_bytes"] for r in results},
        "per_scenario": results,
    }
    metrics_path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"continuity_hit_rate: {metrics['continuity_hit_rate']} ({expected_hits}/{expected_total})")
    print(f"fact_survival: {metrics['fact_survival_by_filler']}")
    print(f"stale_surface_rate: {metrics['stale_surface_rate']}")
    for r in results:
        mark = "OK" if r["ok"] else "MISS"
        print(f"  [{mark}] {r['scenario']} :: {r['task']!r} repo={r['repo']} bytes={r['context_bytes']}")
        if r["missing_facts"]:
            print(f"       missing: {r['missing_facts']}")
        if r["forbidden_surfaced"]:
            print(f"       forbidden leaked: {r['forbidden_surfaced']}")
    return 0 if hit_rate >= min_hit_rate else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Memory continuity replay eval")
    parser.add_argument("--scenarios", type=Path, default=Path(__file__).with_name("scenarios.yaml"))
    parser.add_argument("--metrics", type=Path, default=Path(__file__).with_name("metrics.json"))
    parser.add_argument("--min-hit-rate", type=float, default=0.8)
    args = parser.parse_args()
    return asyncio.run(run(args.scenarios, args.metrics, args.min_hit_rate))


if __name__ == "__main__":
    sys.exit(main())
