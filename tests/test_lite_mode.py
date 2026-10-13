"""Isolated profile tests cannot accidentally inherit the full-stack engine."""
import json
import os
import subprocess
import sys
from pathlib import Path


def test_zero_service_end_to_end_and_restart(tmp_path):
    runner = Path(__file__).with_name("lite_smoke_runner.py")
    repo = tmp_path / "repo"
    env = {**os.environ, "ALLOW_SCRATCH_REPO_ROOTS":"true"}
    first = subprocess.run([sys.executable, str(runner), str(repo)], env=env, capture_output=True, text=True, timeout=45)
    assert first.returncode == 0, first.stdout + first.stderr
    assert json.loads(first.stdout.strip().splitlines()[-1])["external_clients"] == 0
    # A new process reuses the persisted store and returns identical capabilities.
    second = subprocess.run([sys.executable, str(runner), str(repo)], env=env, capture_output=True, text=True, timeout=45)
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
