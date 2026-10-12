"""Worker liveness: /health and Setup say whether a worker will pick jobs up."""
import asyncio

from brain.workers.queue import WORKER_START_HINT, worker_liveness


class _Redis:
    def __init__(self, keys):
        self.keys = set(keys)

    async def get(self, key):
        return b"{}" if key in self.keys else None


def _run(coro):
    return asyncio.run(coro)


def test_legacy_worker_heartbeat_reports_running():
    result = _run(worker_liveness(_Redis({"brain:jobs:heartbeat"}), "brain:jobs", pools_enabled=False))
    assert result == {"running": True, "pools": None, "hint": ""}


def test_missing_heartbeat_reports_not_running_with_start_hint():
    result = _run(worker_liveness(_Redis(set()), "brain:jobs", pools_enabled=False))
    assert result["running"] is False
    assert result["hint"] == WORKER_START_HINT
    assert "python -m brain.workers.worker" in result["hint"]


def test_pools_need_every_pool_alive():
    redis = _Redis({"p:fast:heartbeat", "p:maintenance:heartbeat"})
    result = _run(worker_liveness(redis, "p", pools_enabled=True))
    assert result["running"] is False
    assert result["pools"] == {"fast": True, "maintenance": True, "deep": False}
    redis.keys.add("p:deep:heartbeat")
    assert _run(worker_liveness(redis, "p", pools_enabled=True))["running"] is True


def test_bootstrap_names_the_repo_and_start_runs_api_worker_web():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    bootstrap = (root / "scripts/bootstrap.sh").read_text(encoding="utf-8")
    assert "no --repo given: Brain will read its own source as a sample repository." in bootstrap
    assert 'TARGET_REPO_PATH={sys.argv[1]}' in bootstrap
    assert "./scripts/start.sh" in bootstrap.split('echo "Done. Next:"')[1]
    start = (root / "scripts/start.sh").read_text(encoding="utf-8")
    for command in ("uvicorn apps.api.main:app", "python -m brain.workers.worker", "npm run dev"):
        assert command in start, command
