"""Subprocess acceptance: no external clients, real SQLite and MCP functions."""
import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch


def forbidden(*args, **kwargs):
    raise AssertionError("Lite attempted to construct an external database client")


async def exercise(repo: Path):
    from brain.database.session import init_db, async_session_factory, close_database_connections, redis_client
    from brain.database.models import Embedding, File, FileChunk
    from brain.indexers.file_indexer import FileIndexer
    from brain.memory.decision_store import DecisionStore
    from brain.memory.rule_store import RuleStore
    from brain.memory.learning_store import LearningStore
    from brain.graph.graph_client import GraphClient
    from brain.workers.queue import JobQueue
    from brain.workers.lite_queue import serve_worker
    from apps.mcp_server.server import search_code, prepare_task_context, impact_analysis
    from sqlalchemy import select, func

    await init_db()
    indexer = FileIndexer()
    record = await indexer.index_repository(repo)
    assert record.indexing_status == "completed", indexer.verification
    await DecisionStore.add_decision(title="Payment charge decision", description="Payments remain deterministic",
                                    repo_path=str(repo), affected_files=["payments.py"])
    await RuleStore.add_rule(rule_id="payment-rule", name="Payment charge rule", description="Validate payment input",
                             repo_path=str(repo), applies_to={"files":["payments.py"]})
    await LearningStore.add_learning("Payment charge validates an amount", category="payments", confidence=0.95,
                                     repo_scope=str(repo), evidence=[{"file":"payments.py","line":2}])
    search = await search_code("charge", str(repo))
    assert any(candidate["path"] == "payments.py" for candidate in search.get("results", [])), search
    context = await prepare_task_context("charge payments amount", str(repo))
    assert context["slices"] and any("return amount" in item["content"] for item in context["slices"]), context
    assert context.get("memory", {}).get("decisions"), context
    assert context.get("memory", {}).get("rules"), context
    assert context.get("memory", {}).get("learnings"), context
    impact = await impact_analysis("Rename charge payments", str(repo))
    assert impact["status"] == "ok" and impact["affected"], impact
    neighbors = await GraphClient(record.id).get_neighbors("payments.py")
    assert neighbors
    async with async_session_factory() as session:
        chunks = await session.scalar(select(func.count()).select_from(FileChunk))
        assert chunks and await session.scalar(select(func.count()).select_from(Embedding)) >= chunks
        before = await session.scalar(select(func.count()).select_from(File))
    await indexer.index_repository(repo)
    assert indexer.progress["files"]["skipped"] == before
    await redis_client.delete("lite-nx")
    assert await redis_client.set("lite-nx", "first", nx=True, ex=10)
    assert not await redis_client.set("lite-nx", "second", nx=True, ex=10)
    assert await redis_client.get("lite-nx") == "first"
    queue = JobQueue(redis_client, prefix="lite-smoke")
    job_id = await queue.enqueue("health_check", idempotency_key="one")
    assert await queue.enqueue("health_check", idempotency_key="one") == job_id
    if (await queue.get_job(job_id))["status"] != "completed":
        job, token = await queue.claim()
        assert job["job_id"] == job_id
        assert not await queue.finish(job_id, "wrong-token", "completed", {})
        assert await queue.finish(job_id, token, "completed", {"ok":True})
    assert (await queue.get_job(job_id))["result"]["ok"]
    # Actual background worker and SQL fence, with no Redis lease emulation.
    from brain.config.settings import settings
    worker_queue = JobQueue(redis_client, settings.WORKER_REDIS_PREFIX)
    worker_job = await worker_queue.enqueue("reindex", {"repo_path":str(repo)})
    worker = asyncio.create_task(serve_worker())
    try:
        deadline = asyncio.get_running_loop().time() + 30
        while True:
            state = await worker_queue.get_job(worker_job)
            if state["status"] in {"completed","failed"}:
                break
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.05)
        assert state["status"] == "completed", state
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    await close_database_connections()
    print(json.dumps({"index":"completed", "mcp_search":True, "context_memory_layers":3,
                      "impact":True,"graph_neighbors":True,"sqlite_worker":True,"external_clients":0}))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    repo = Path(sys.argv[1]).resolve()
    repo.mkdir(exist_ok=True)
    (repo / "payments.py").write_text('"""Payments."""\ndef charge(amount):\n    """Validate payment amount."""\n    return amount\n')
    (repo / "consumer.py").write_text('from payments import charge\ndef checkout(amount):\n    return charge(amount)\n')
    os.environ.update(BRAIN_STORAGE_MODE="lite", BRAIN_LITE_DIR=str(repo / ".brain"), DEFAULT_EMBEDDING_PROVIDER="local",
                      DEFAULT_LLM_PROVIDER="disabled", EMBEDDING_DIMENSION="384", REPO_ROOT=str(repo), TARGET_REPO_PATH=str(repo),
                      BRAIN_AGENT_CONTEXT_V2_MODE="on", BRAIN_WORKER_POOLS_V2_ENABLED="false", LATE_INTERACTION_ENABLED="false")
    with patch("redis.asyncio.from_url", forbidden), patch("neo4j.AsyncGraphDatabase.driver", forbidden), patch("asyncpg.connect", forbidden):
        asyncio.run(exercise(repo))
