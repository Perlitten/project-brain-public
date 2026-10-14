import json
from pathlib import Path
import subprocess
import sys

from scripts.mutation_audit import manifest, public_command, run


def test_public_command_preserves_arguments_without_host_path():
    command = ["/synthetic/private-worktree/.venv/bin/python", "-m", "pytest", "tests/test_nonce.py"]
    assert public_command(command) == ["python", *command[1:]]
    assert command[0].startswith("/synthetic/")


def test_record_anonymizes_interpreter_without_changing_execution(tmp_path, monkeypatch):
    root = tmp_path / "source"
    for name in ("brain/harness", "brain/context", "apps", "tests", "rules", "eval"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "brain/harness/nonce.py").write_text("def generate_nonce():\n    return True\n")
    for relative in ("brain/harness/attestation.py", "brain/context/budget.py", "pyproject.toml"):
        (root / relative).write_text("")
    actual = []

    def completed(command, **kwargs):
        actual.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="fixture-only", stderr="")

    monkeypatch.setattr(subprocess, "run", completed)
    output = tmp_path / "output"
    run(root, manifest(root), output, [], 1, 5)
    record = json.loads((output / "results.json").read_text())
    assert actual and all(command[0] == sys.executable for command in actual)
    assert record["command"] == public_command(actual[0])
    assert record["interpreter_path_redacted"] is True
    assert record["python_version"] == sys.version.split()[0]
    assert record["results"][0]["exit_code"] == 0
    assert Path(root / "brain/harness/nonce.py").read_text().endswith("return True\n")
