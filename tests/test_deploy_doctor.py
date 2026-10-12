from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name == "nt", reason="deployment doctor runs on Linux hosts")
def test_public_check_needs_no_orchestrator_host(
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
    assert "orchestrator" not in result.stdout.lower()
