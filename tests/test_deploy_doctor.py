from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name == "nt", reason="deployment doctor runs on Linux hosts")
def test_stale_scan_ignores_local_secret_files_but_not_tracked_content(
    tmp_path: Path,
) -> None:
    source = Path(__file__).resolve().parents[1] / "deploy" / "doctor.sh"
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    doctor = deploy_dir / "doctor.sh"
    shutil.copy2(source, doctor)

    stale_host = "198" + ".51.100.77"
    (tmp_path / ".env").write_text(f"BRAIN_PUBLIC_HOST=brain.{stale_host}.nip.io\n", encoding="utf-8")
    (tmp_path / ".mcp.json").write_text(f'{{"url":"https://brain.{stale_host}.nip.io"}}\n', encoding="utf-8")
    secrets_dir = tmp_path / ".secrets"
    secrets_dir.mkdir()
    (secrets_dir / "runtime.txt").write_text(stale_host, encoding="utf-8")
    (tmp_path / ".dashboard-temp-login.txt").write_text(stale_host, encoding="utf-8")
    for runtime_dir_name in ("target-repo", "indexed-repos", "indexed-projects"):
        runtime_dir = tmp_path / runtime_dir_name
        runtime_dir.mkdir()
        (runtime_dir / "mounted-source.md").write_text(stale_host, encoding="utf-8")

    clean = subprocess.run(
        ["bash", str(doctor), "stale-scan"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert clean.returncode == 0, clean.stderr

    grep_fallback = subprocess.run(
        ["bash", str(doctor), "stale-scan"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "BRAIN_DOCTOR_FORCE_GREP": "1"},
    )
    assert grep_fallback.returncode == 0, grep_fallback.stderr

    nested = tmp_path / "nested"
    nested.mkdir()
    nested_runtime_name = nested / ".env"
    nested_runtime_name.write_text(stale_host, encoding="utf-8")
    nested_blocked = subprocess.run(
        ["bash", str(doctor), "stale-scan"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "BRAIN_DOCTOR_FORCE_GREP": "1"},
    )
    assert nested_blocked.returncode == 1
    assert "nested/.env" in nested_blocked.stderr
    nested_runtime_name.unlink()

    (tmp_path / "tracked.md").write_text(stale_host, encoding="utf-8")
    blocked = subprocess.run(
        ["bash", str(doctor), "stale-scan"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert blocked.returncode == 1
    assert "tracked.md" in blocked.stderr


@pytest.mark.skipif(os.name == "nt", reason="deployment doctor runs on Linux hosts")
def test_public_check_needs_no_n8n_host(
    tmp_path: Path,
) -> None:
    source = Path(__file__).resolve().parents[1] / "deploy" / "doctor.sh"
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    doctor = deploy_dir / "doctor.sh"
    shutil.copy2(source, doctor)
    (tmp_path / ".env").write_text(
        "BRAIN_PUBLIC_HOST=brain.example.test\n",
        encoding="utf-8",
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_getent = bin_dir / "getent"
    fake_getent.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_getent.chmod(0o755)
    fake_curl = bin_dir / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\nexit 0\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    result = subprocess.run(
        ["bash", str(doctor), "public"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stderr
    assert "public API reachable" in result.stdout
    assert "n8n" not in result.stdout.lower()
