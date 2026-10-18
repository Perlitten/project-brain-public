import os
import subprocess
import sys
from pathlib import Path

import pytest

from brain.config.settings import Settings, _env_files


def test_disable_dotenv_isolation_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_DISABLE_DOTENV", "true")
    monkeypatch.setenv("BRAIN_STORAGE_MODE", "full")
    monkeypatch.setenv("BRAIN_ENV_FILE", "explicit.env")
    assert _env_files() == ()
    (tmp_path / ".env").write_text("POSTGRES_POOL_SIZE=50\n", encoding="utf-8")
    env = dict(os.environ)
    env.pop("POSTGRES_POOL_SIZE", None)
    env.update({"BRAIN_DISABLE_DOTENV": "true", "BRAIN_STORAGE_MODE": "full",
                "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    proc = subprocess.run(
        [sys.executable, "-c", "from brain.config.settings import settings; print(settings.POSTGRES_POOL_SIZE)"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=True,
    )
    assert proc.stdout.strip() == "10"


def test_postgres_pool_settings_have_bounded_operational_defaults():
    configured = Settings(_env_file=None)

    assert configured.POSTGRES_POOL_SIZE == 10
    assert configured.POSTGRES_MAX_OVERFLOW == 10
    assert configured.POSTGRES_POOL_TIMEOUT_SECONDS == 15.0


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("POSTGRES_POOL_SIZE", 0, "POSTGRES_POOL_SIZE"),
        ("POSTGRES_POOL_SIZE", 51, "POSTGRES_POOL_SIZE"),
        ("POSTGRES_MAX_OVERFLOW", -1, "POSTGRES_MAX_OVERFLOW"),
        ("POSTGRES_MAX_OVERFLOW", 51, "POSTGRES_MAX_OVERFLOW"),
        ("POSTGRES_POOL_TIMEOUT_SECONDS", 0, "POSTGRES_POOL_TIMEOUT_SECONDS"),
        ("POSTGRES_POOL_TIMEOUT_SECONDS", 301, "POSTGRES_POOL_TIMEOUT_SECONDS"),
    ],
)
def test_postgres_pool_settings_reject_unsafe_ranges(name, value, message):
    with pytest.raises(ValueError, match=message):
        Settings(_env_file=None, **{name: value})
