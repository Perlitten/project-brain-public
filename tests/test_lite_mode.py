"""Isolated profile tests cannot accidentally inherit the full-stack engine."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from brain.database.lite_cache import SQLiteCache
from brain.workers.lite_queue import SQLiteJobQueue


@pytest.mark.asyncio
async def test_lite_queue_retries_and_lists_persisted_jobs(tmp_path):
    queue = SQLiteJobQueue(SQLiteCache(tmp_path / "cache"), prefix="test")
    first = await queue.enqueue("health_check", max_attempts=2)
    await asyncio.sleep(0.01)
    second = await queue.enqueue("health_check")

    assert await queue.recent_job_ids(limit=1) == [second]
    assert await queue.recent_job_ids(limit=1, offset=1) == [first]

    job, token = await queue.claim()
    assert job["job_id"] == first and job["attempt"] == 1
    assert await queue.finish(first, token, "queued", {"error": "transient"})
    retried, retry_token = await queue.claim()
    assert retried["job_id"] == first and retried["attempt"] == 2
    assert await queue.finish(first, retry_token, "failed", {"error": "terminal"})


def test_zero_service_end_to_end_and_restart(tmp_path):
    runner = Path(__file__).with_name("lite_smoke_runner.py")
    repo = tmp_path / "repo"
    env = {**os.environ, "ALLOW_SCRATCH_REPO_ROOTS":"true"}
    first = subprocess.run([sys.executable, str(runner), str(repo)], env=env, capture_output=True, text=True, timeout=60)
    assert first.returncode == 0, first.stdout + first.stderr
    assert json.loads(first.stdout.strip().splitlines()[-1])["external_clients"] == 0
    # A new process reuses the persisted store and returns identical capabilities.
    second = subprocess.run([sys.executable, str(runner), str(repo)], env=env, capture_output=True, text=True, timeout=60)
    assert second.returncode == 0, second.stdout + second.stderr


def test_lite_cli_bootstraps_before_engine_import(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH":str(root), "DEFAULT_EMBEDDING_PROVIDER":"local"}
    result = subprocess.run([sys.executable, "-m", "apps.cli.entrypoint", "init", "--lite"], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / ".brain/brain.sqlite3").is_file()
    assert (tmp_path / ".brain/config.json").is_file()
    result = subprocess.run([sys.executable, "-m", "apps.cli.entrypoint", "index"], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
