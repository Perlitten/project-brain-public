"""Multi-agent benchmark: concurrent writes to L2/L3, isolation, performance.

Runs against a scratch Postgres database by default (see eval/bench_db.py):
planted learnings and episodes never reach the L3 that /ask reads.
``--shared-db`` restores the old behavior of writing to the configured DB.
"""

import argparse
import asyncio
import os
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

from bench_db import isolated_bench_db  # noqa: E402

BENCH_CATEGORY = "mabench"


async def _reject_quietly(learning_id: int) -> None:
    from brain.memory.learning_store import LearningStore

    try:
        await LearningStore.reject(learning_id)
    except Exception:
        pass


async def test_concurrent_learnings() -> bool:
    """Multiple agents writing learnings simultaneously."""
    from brain.memory.learning_store import LearningStore

    planted: list[int] = []

    async def write_learning(agent_id: int) -> bool:
        try:
            lid = await LearningStore.add_learning(
                statement=f"Agent {agent_id} learning {uuid.uuid4().hex[:8]}",
                category=BENCH_CATEGORY,
                confidence=0.9,
                repo_scope=f"/tmp/agent-{agent_id}",
            )
            if lid is not None:
                planted.append(lid)
                return True
        except Exception:
            pass
        return False

    start = time.time()
    try:
        results = await asyncio.gather(*[write_learning(i) for i in range(10)])
        elapsed = time.time() - start
        success = sum(results)
        print(f"Concurrent L3 writes: {success}/10 in {elapsed:.1f}s")
        return success == 10
    finally:
        await asyncio.gather(*[_reject_quietly(lid) for lid in planted])


async def test_repo_isolation() -> bool:
    """A learning scoped to repo A must not surface under repo B's scope."""
    from brain.memory.learning_store import LearningStore

    scope_a = "/tmp/isolated-repo"
    scope_b = "/tmp/other-repo"
    lid = await LearningStore.add_learning(
        statement=f"Isolation test {uuid.uuid4().hex[:8]}",
        category=BENCH_CATEGORY,
        confidence=0.9,
        repo_scope=scope_a,
    )
    try:
        in_own = [row.id for row in await LearningStore.list_active_learnings(scope_a)]
        in_other = [row.id for row in await LearningStore.list_active_learnings(scope_b)]
        global_ids = [row.id for row in await LearningStore.list_active_learnings(None)]
        own_hit = lid in in_own
        leaked_other = lid in in_other
        leaked_global = lid in global_ids
        print(
            f"Isolation test: own-scope hit={own_hit}, "
            f"leaked to other scope={leaked_other}, leaked to global={leaked_global}"
        )
        # Able to fail: a scoped learning that leaks to another repo's view
        # (or to the global view) is a real isolation bug, not informational.
        return own_hit and not leaked_other and not leaked_global
    finally:
        await _reject_quietly(lid)


async def test_concurrent_episodes() -> bool:
    """Multiple agents creating L2 episodes."""
    from brain.database.models import MemoryEpisode
    from brain.database.session import async_session_factory

    created: list[int] = []

    async def create_episode(agent_id: int) -> bool:
        try:
            async with async_session_factory() as session:
                ep = MemoryEpisode(
                    source_event_ids=[],
                    distilled_summary=f"Agent {agent_id} episode",
                    topic=BENCH_CATEGORY,
                    status="pending",
                    confidence=0.8,
                )
                session.add(ep)
                await session.commit()
                created.append(ep.id)
                return True
        except Exception:
            return False

    try:
        results = await asyncio.gather(*[create_episode(i) for i in range(10)])
        success = sum(results)
        print(f"Concurrent L2 writes: {success}/10")
        return success == 10
    finally:
        from sqlalchemy import delete

        async with async_session_factory() as session:
            await session.execute(delete(MemoryEpisode).where(MemoryEpisode.id.in_(created)))
            await session.commit()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shared-db", action="store_true",
                    help="write to the configured database instead of a scratch one")
    args = ap.parse_args()

    print("=== Multi-Agent Benchmark ===")

    async def run() -> bool:
        r1 = await test_concurrent_learnings()
        r2 = await test_repo_isolation()
        r3 = await test_concurrent_episodes()
        print(f"\nResults: L3 concurrent={r1}, isolation={r2}, L2 concurrent={r3}")
        return all([r1, r2, r3])

    if args.shared_db:
        print("[isolation] --shared-db: writing to the configured database")
        result = await run()
    else:
        async with isolated_bench_db() as bench:
            print(f"[isolation] scratch database: {bench.db_name}")
            result = await run()
    return 0 if result else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
