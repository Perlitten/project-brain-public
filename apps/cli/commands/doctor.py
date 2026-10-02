"""Local install preflight for the single-user profile.

Prints PASS/WARN/FAIL lines with the next action for each failure — never
secret values. Exits 1 on any FAIL so a fresh install can gate on
``brain doctor`` before the first indexing run. ``deploy/doctor.sh`` covers
the production VPS path; this is the local counterpart.
"""

import asyncio
import sys
import tempfile
from pathlib import Path

import typer

from brain.config.paths import context_packs_dir, reports_dir
from brain.config.settings import settings
from brain.database.session import check_health
from brain.version import build_info

_FAIL = "FAIL"
_WARN = "WARN"
_PASS = "PASS"

_PROVIDER_KEYS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
}


def _report(level: str, message: str, failures: list[str]) -> None:
    color = {_PASS: typer.colors.GREEN, _WARN: typer.colors.YELLOW}.get(level, typer.colors.RED)
    typer.secho(f"{level}: {message}", fg=color, err=(level == _FAIL))
    if level == _FAIL:
        failures.append(message)


def _check_python(failures: list[str]) -> None:
    if sys.version_info >= (3, 10):
        _report(_PASS, f"python {sys.version_info.major}.{sys.version_info.minor} (>=3.10 required)", failures)
    else:
        _report(_FAIL, f"python {sys.version_info.major}.{sys.version_info.minor} — Project Brain requires >=3.10", failures)


def _check_env_file(failures: list[str]) -> None:
    if Path(".env").is_file():
        _report(_PASS, ".env present", failures)
    else:
        _report(_WARN, ".env missing — running on defaults/mock providers; copy .env.example to .env", failures)


def _check_services(failures: list[str]) -> None:
    try:
        results = asyncio.run(check_health())
    except Exception as exc:
        _report(_FAIL, f"service health check raised {type(exc).__name__}", failures)
        return
    for service in ("postgres", "redis", "neo4j"):
        info = results.get(service, {})
        if info.get("status") == "healthy":
            _report(_PASS, f"{service} reachable", failures)
        else:
            detail = info.get("error") or info.get("message") or "unreachable"
            _report(
                _FAIL,
                f"{service} {detail} — start services with `docker compose up -d postgres redis neo4j`",
                failures,
            )


def _check_writable_dirs(failures: list[str]) -> None:
    for label, directory in (("context packs", context_packs_dir()), ("reports", reports_dir())):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=directory) as probe:
                probe.write(b"ok")
            _report(_PASS, f"{label} dir writable ({directory})", failures)
        except OSError as exc:
            _report(_FAIL, f"{label} dir {directory} not writable: {exc}", failures)


def _check_target_repo(failures: list[str]) -> None:
    path = Path(settings.TARGET_REPO_PATH)
    if path.is_dir():
        _report(_PASS, f"TARGET_REPO_PATH exists ({path.resolve()})", failures)
    else:
        _report(
            _FAIL,
            f"TARGET_REPO_PATH={settings.TARGET_REPO_PATH} is not a directory — "
            "point it at the repository Brain should index",
            failures,
        )


def _check_provider(failures: list[str]) -> None:
    for kind, provider in (
        ("LLM", settings.DEFAULT_LLM_PROVIDER.lower()),
        ("embedding", settings.DEFAULT_EMBEDDING_PROVIDER.lower()),
    ):
        if provider == "mock":
            _report(
                _WARN,
                f"DEFAULT_{'LLM' if kind == 'LLM' else 'EMBEDDING'}_PROVIDER=mock — "
                "real answers/embeddings need a real provider key",
                failures,
            )
            continue
        key_name = _PROVIDER_KEYS.get(provider)
        if key_name is None:
            _report(_FAIL, f"unknown {kind} provider '{provider}' — supported: mock, openai, anthropic, google, nvidia", failures)
            continue
        key = getattr(settings, key_name, None)
        if not key:
            _report(_FAIL, f"{kind} provider '{provider}' configured but {key_name} is not set", failures)
        elif provider == "nvidia" and not key.startswith("nvapi-"):
            _report(_FAIL, f"{key_name} does not look like an NVIDIA key (expected nvapi-…)", failures)
        else:
            _report(_PASS, f"{kind} provider '{provider}' has {key_name} set", failures)


def _check_api_auth(failures: list[str]) -> None:
    if settings.PROJECT_BRAIN_API_KEY:
        _report(_PASS, "PROJECT_BRAIN_API_KEY set — API requests are authenticated", failures)
    elif settings.ENVIRONMENT.lower() == "production":
        _report(_FAIL, "ENVIRONMENT=production but PROJECT_BRAIN_API_KEY is unset — the API fails closed", failures)
    else:
        _report(_WARN, "PROJECT_BRAIN_API_KEY unset — local dev-open mode; set it before exposing the API beyond localhost", failures)


def doctor_command():
    """Validate this local install end-to-end before first use."""
    info = build_info()
    typer.echo(f"Project Brain {info['version']} — install doctor")

    failures: list[str] = []
    _check_python(failures)
    _check_env_file(failures)
    _check_target_repo(failures)
    _check_writable_dirs(failures)
    _check_provider(failures)
    _check_api_auth(failures)
    _check_services(failures)

    if failures:
        typer.secho(f"\n{len(failures)} check(s) failed — fix the items above and re-run `brain doctor`.", fg=typer.colors.RED, bold=True)
        raise typer.Exit(code=1)
    typer.secho("\nAll checks passed — ready to index and serve.", fg=typer.colors.GREEN, bold=True)
