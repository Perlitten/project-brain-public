"""Real dotenv round trips and failure preservation for setup writes."""
import os
import stat

import pytest
from dotenv import dotenv_values

from brain.onboarding import envfile


def test_setup_replaces_duplicate_exported_and_spaced_keys(tmp_path):
    path = tmp_path / ".env"
    untouched = b"# existing config\r\nOPENAI_API_KEY='first\r\nsecond'\r\n"
    path.write_bytes(untouched + b"TARGET_REPO_PATH=/old\n export TARGET_REPO_PATH = /last\n")
    envfile.update_env_file(path, {"TARGET_REPO_PATH": "/new"})
    assert dotenv_values(path)["TARGET_REPO_PATH"] == "/new"
    assert path.read_bytes().startswith(untouched)
    assert path.read_text().count("TARGET_REPO_PATH=") == 1


@pytest.mark.parametrize("value", ["/repos/space # project", "/repos/it's \\\"quoted\\\"", "/repos/a+b&c"])
def test_setup_repository_path_round_trips(tmp_path, value):
    path = tmp_path / ".env"
    envfile.update_env_file(path, {"TARGET_REPO_PATH": value})
    assert dotenv_values(path)["TARGET_REPO_PATH"] == value


def test_setup_rejects_interpolation_before_touching_env(tmp_path):
    path = tmp_path / ".env"
    path.write_text("TARGET_REPO_PATH=/keep\n")
    with pytest.raises(ValueError, match="interpolation"):
        envfile.update_env_file(path, {"TARGET_REPO_PATH": "/repos/${HOME}"})
    assert path.read_text() == "TARGET_REPO_PATH=/keep\n"


@pytest.mark.parametrize("key,value", [
    ("OPENAI_API_KEY", "not-allowed"),
    ("DEFAULT_LLM_PROVIDER", "typo"),
    ("DEFAULT_EMBEDDING_PROVIDER", "anthropic"),
])
def test_setup_rejects_invalid_keys_and_unavailable_providers(tmp_path, key, value):
    path = tmp_path / ".env"
    with pytest.raises(ValueError):
        envfile.update_env_file(path, {key: value})
    assert not path.exists()


def test_setup_atomic_replace_failure_preserves_existing_config(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    original = b"OPENAI_API_KEY=keep\nTARGET_REPO_PATH=/old\n"
    path.write_bytes(original)

    def fail_replace(*args):
        raise OSError("simulated write failure")

    monkeypatch.setattr(envfile.os, "replace", fail_replace)
    with pytest.raises(OSError):
        envfile.update_env_file(path, {"TARGET_REPO_PATH": "/new"})
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.skipif(os.name == "nt", reason="Windows chmod does not expose POSIX permission bits")
def test_setup_config_posix_permissions(tmp_path):
    path = tmp_path / ".env"
    envfile.update_env_file(path, {"TARGET_REPO_PATH": "/new"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_text("OPENAI_API_KEY=keep")
    path.chmod(0o640)
    envfile.update_env_file(path, {"TARGET_REPO_PATH": "/new"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert dotenv_values(path)["OPENAI_API_KEY"] == "keep"
    assert dotenv_values(path)["TARGET_REPO_PATH"] == "/new"


def test_setup_preserves_final_unterminated_line(tmp_path):
    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY=keep")
    envfile.update_env_file(path, {"TARGET_REPO_PATH": "/new"})
    assert dotenv_values(path)["OPENAI_API_KEY"] == "keep"
    assert dotenv_values(path)["TARGET_REPO_PATH"] == "/new"
