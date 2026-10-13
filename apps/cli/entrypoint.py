"""Select a saved local profile before importing engines or CLI commands."""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path


def initialize_lite(directory: Path, repo: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    profile = directory / "config.json"
    if not profile.exists():
        descriptor = os.open(profile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"storage_mode": "lite", "repo": str(repo), "api_key": secrets.token_urlsafe(32)}, stream)
    # Local profile state must not make a previously clean Git source dirty.
    if directory == repo / ".brain":
        try:
            excluded = subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-path", "info/exclude"],
                                      capture_output=True, text=True, check=True)
            target = Path(excluded.stdout.strip())
            if not target.is_absolute():
                target = repo / target
            old = target.read_text(encoding="utf-8") if target.exists() else ""
            if "/.brain/" not in old.splitlines():
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("a", encoding="utf-8") as stream:
                    stream.write("\n/.brain/\n")
        except (OSError, subprocess.CalledProcessError):
            pass  # Non-Git directories remain supported snapshots.
    return profile


def configure_lite(directory: Path, profile: Path | None = None) -> None:
    directory = directory.expanduser().resolve()
    data = json.loads(profile.read_text(encoding="utf-8")) if profile and profile.is_file() else {}
    repo = str(Path(data.get("repo", Path.cwd())).resolve())
    os.environ.update(BRAIN_STORAGE_MODE="lite", BRAIN_LITE_DIR=str(directory), REPO_ROOT=repo,
                      TARGET_REPO_PATH=repo, CONTEXT_PACK_OUTPUT_DIR=str(directory / "context_packs"),
                      REPORT_OUTPUT_DIR=str(directory / "reports"), BRAIN_AGENT_CONTEXT_V2_MODE="on",
                      ENVIRONMENT="local", BRAIN_WORKER_POOLS_V2_ENABLED="false", LATE_INTERACTION_ENABLED="false")
    os.environ.setdefault("DEFAULT_EMBEDDING_PROVIDER", "local")
    os.environ.setdefault("DEFAULT_LLM_PROVIDER", "disabled")
    os.environ.setdefault("INDEX_LLM_SUMMARIES_BACKGROUND", "false")
    # Do not inherit incompatible widths from the full stack's dotenv file.
    if os.environ["DEFAULT_EMBEDDING_PROVIDER"] == "local":
        os.environ["EMBEDDING_DIMENSION"] = "384"
    if data.get("api_key"):
        os.environ.setdefault("PROJECT_BRAIN_API_KEY", data["api_key"])


def bootstrap(argv: list[str]) -> list[str]:
    if os.environ.get("BRAIN_STORAGE_MODE") == "full" and "--lite" not in argv:
        return argv
    explicit = "--lite" in argv or os.environ.get("BRAIN_STORAGE_MODE") == "lite"
    args = [arg for arg in argv if arg != "--lite"]
    cwd = Path.cwd().resolve()
    directory = Path(os.environ.get("BRAIN_LITE_DIR", cwd / ".brain")).expanduser().resolve()
    profile = directory / "config.json"
    if not explicit and not profile.is_file():
        for parent in cwd.parents:
            if (parent / ".brain/config.json").is_file():
                directory = parent / ".brain"
                profile = directory / "config.json"
                break
    if explicit or profile.is_file() or (args and args[0] == "init"):
        if args and args[0] in {"init", "serve"}:
            profile = initialize_lite(directory, cwd)
        configure_lite(directory, profile)
    return args


def main() -> None:
    args = bootstrap(sys.argv[1:])
    from apps.cli.main import cli
    cli(args=args)


def mcp_main() -> None:
    bootstrap(sys.argv[1:])
    from apps.mcp_server.server import main as server_main
    server_main()


if __name__ == "__main__":
    main()
