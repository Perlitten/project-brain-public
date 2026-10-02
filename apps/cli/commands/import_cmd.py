"""CLI commands for importing external knowledge sources (Grafify, Obsidian)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer

from brain.config.settings import settings
from brain.config.paths import resolve_repo_path
from brain.database.repository_utils import require_repository_by_path
from brain.integrations.grafify import GrafifyIntegration, resolve_grafify_output_path
from brain.integrations.obsidian import ObsidianIntegration

import_app = typer.Typer(help="Import external graph or vault data into Project Brain.")


@import_app.command("grafify")
def import_grafify(
    path: Optional[str] = typer.Option(
        None,
        "--path",
        help="Grafify JSON file (default: GRAFIFY_OUTPUT_PATH or ../graphify-out/graph.json)",
    ),
    repo: Optional[str] = typer.Option(
        None,
        "--repo",
        help="Indexed repository path that owns the imported graph",
    ),
):
    """Import a Grafify graph JSON export into Neo4j."""
    target = resolve_grafify_output_path(path)
    if not target.exists():
        typer.secho(f"Grafify output not found: {target}", fg=typer.colors.RED, err=True)
        typer.echo("Set GRAFIFY_OUTPUT_PATH in .env or pass --path to the JSON file.")
        raise typer.Exit(code=1)

    async def _run() -> None:
        repository = await require_repository_by_path(resolve_repo_path(repo))
        integration = GrafifyIntegration(repository_id=repository.id)
        await integration.import_json_file(str(target))

    typer.echo(f"Importing Grafify graph from {target} ...")
    try:
        asyncio.run(_run())
    except Exception as exc:
        typer.secho(f"Grafify import failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.secho("Grafify import completed.", fg=typer.colors.GREEN, bold=True)


@import_app.command("obsidian")
def import_obsidian(
    vault: Optional[str] = typer.Option(
        None,
        "--vault",
        help="Obsidian vault directory (default: OBSIDIAN_VAULT_PATH env)",
    ),
):
    """Scan an Obsidian vault and import rules/decisions into PostgreSQL."""
    vault_str = vault or settings.OBSIDIAN_VAULT_PATH
    if not vault_str:
        typer.secho(
            "Obsidian vault path required. Set OBSIDIAN_VAULT_PATH in .env or pass --vault.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    vault_path = Path(vault_str).resolve()
    if not vault_path.exists():
        typer.secho(f"Obsidian vault not found: {vault_path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    async def _run() -> dict:
        integration = ObsidianIntegration(str(vault_path))
        return await integration.import_to_stores()

    typer.echo(f"Importing Obsidian vault from {vault_path} ...")
    try:
        results = asyncio.run(_run())
    except Exception as exc:
        typer.secho(f"Obsidian import failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.secho(
        f"Imported {len(results.get('decisions', []))} decisions and "
        f"{len(results.get('rules', []))} rules.",
        fg=typer.colors.GREEN,
        bold=True,
    )
