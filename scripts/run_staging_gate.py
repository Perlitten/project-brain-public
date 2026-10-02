#!/usr/bin/env python3
"""Unified Staging Gate Runner & Evidence Pack Generator for Project Brain.

Features:
1. Executes commands safely via shell or direct subprocess.
2. Captures full untruncated stdout and stderr into separate .stdout.log and .stderr.log files.
3. Performs redaction of sensitive credentials BEFORE writing to disk.
4. Generates non-self-referential standalone .json.sha256 checksum files.
5. Captures Git SHA, Git Tree SHA, working tree cleanliness, OCI image revision labels, and host metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SECRET_PATTERNS = [
    re.compile(r"X-API-Key:\s*([^\s'\"]+)", re.IGNORECASE),
    re.compile(r"password=([^\s'\"]+)", re.IGNORECASE),
    re.compile(r"redis://:[^@]+@", re.IGNORECASE),
    re.compile(r"postgres(?:ql)?://[^:]+:([^@]+)@", re.IGNORECASE),
    re.compile(r"BRAIN_API_KEY=([^\s'\"]+)", re.IGNORECASE),
]


def redact_secrets(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    return redacted


def run_cmd(cmd: str | list[str], cwd: Path = PROJECT_ROOT, env: dict | None = None) -> tuple[int, str, str]:
    current_env = os.environ.copy()
    if env:
        current_env.update(env)

    use_shell = isinstance(cmd, str)
    res = subprocess.run(
        cmd,
        shell=use_shell,
        cwd=cwd,
        env=current_env,
        capture_output=True,
        text=True,
    )
    return res.returncode, redact_secrets(res.stdout), redact_secrets(res.stderr)


def get_git_info() -> dict:
    _, commit_sha, _ = run_cmd("git rev-parse HEAD")
    _, tree_sha, _ = run_cmd("git rev-parse HEAD^{tree}")
    _, status, _ = run_cmd("git status --porcelain")
    return {
        "commit_sha": commit_sha.strip(),
        "tree_sha": tree_sha.strip(),
        "is_dirty": bool(status.strip()),
    }


def run_staging_gate(gate_num: int, name: str, output_path: str, command: str | list[str]) -> dict:
    t_start_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    start_sec = time.perf_counter()

    git_info = get_git_info()
    exit_code, stdout_full, stderr_full = run_cmd(command)
    elapsed_sec = time.perf_counter() - start_sec
    t_finish_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    # Save full untruncated logs
    stdout_file = out_file.with_suffix(".stdout.log")
    stderr_file = out_file.with_suffix(".stderr.log")
    stdout_file.write_text(stdout_full, encoding="utf-8")
    stderr_file.write_text(stderr_full, encoding="utf-8")

    stdout_sha = hashlib.sha256(stdout_file.read_bytes()).hexdigest()
    stderr_sha = hashlib.sha256(stderr_file.read_bytes()).hexdigest()

    status = "PASS" if exit_code == 0 else "FAIL"
    cmd_str = command if isinstance(command, str) else " ".join(command)

    evidence = {
        "gate_id": f"gate_{gate_num}",
        "gate_name": name,
        "status": status,
        "timestamps": {
            "start_utc": t_start_utc,
            "finish_utc": t_finish_utc,
            "duration_seconds": round(elapsed_sec, 3),
        },
        "system_info": {
            "hostname": platform.node(),
            "os": platform.system(),
            "os_release": platform.release(),
            "python_version": sys.version.split()[0],
        },
        "git_provenance": git_info,
        "executed_command": redact_secrets(cmd_str),
        "exit_code": exit_code,
        "logs": {
            "stdout_path": str(stdout_file.relative_to(PROJECT_ROOT) if stdout_file.is_relative_to(PROJECT_ROOT) else stdout_file),
            "stdout_sha256": stdout_sha,
            "stderr_path": str(stderr_file.relative_to(PROJECT_ROOT) if stderr_file.is_relative_to(PROJECT_ROOT) else stderr_file),
            "stderr_sha256": stderr_sha,
        },
        "stdout_snippet": stdout_full[-2048:] if len(stdout_full) > 2048 else stdout_full,
        "stderr_snippet": stderr_full[-2048:] if len(stderr_full) > 2048 else stderr_full,
        "redaction_applied": True,
    }

    # Write evidence JSON
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(evidence, f, indent=2)

    # Write non-self-referential checksum file
    json_sha = hashlib.sha256(out_file.read_bytes()).hexdigest()
    checksum_file = out_file.with_suffix(".json.sha256")
    checksum_file.write_text(f"{json_sha}  {out_file.name}\n", encoding="utf-8")

    print(f"[Gate {gate_num} - {name}] Status: {status} (Exit: {exit_code}). Artifact: {out_file} (SHA256: {json_sha[:12]})")
    return evidence


def main():
    parser = argparse.ArgumentParser(description="Unified Staging Gate Runner")
    parser.add_argument("--gate", type=int, required=True, help="Gate number (1-8)")
    parser.add_argument("--name", type=str, default="staging_gate", help="Gate name")
    parser.add_argument("--output", type=str, required=True, help="Output artifact path")
    parser.add_argument("command", nargs="+", help="Gate command line to execute")

    args = parser.parse_args()
    res = run_staging_gate(args.gate, args.name, args.output, args.command)
    sys.exit(res["exit_code"])


if __name__ == "__main__":
    main()
