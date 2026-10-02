import typer
import asyncio
from typing import Optional

from brain.config.paths import resolve_repo_path
from brain.context.context_pack_builder import ContextPackBuilder


def context_command(
    task_description: str = typer.Argument(..., help="Natural language description of the task"),
    repo: Optional[str] = typer.Option(
        None,
        "--repo",
        help="Path to the target repository. Defaults to REPO_ROOT or the current directory.",
    ),
    budget: str = typer.Option(
        "standard",
        "--budget",
        help="Token budget constraint mode ('small' (8k), 'standard' (32k), or 'deep' (128k+)).",
    ),
    debug_retrieval: bool = typer.Option(
        False,
        "--debug-retrieval",
        help="Display debug breakdown for each individual retriever.",
    ),
):
    """Prepare a structured context pack for a development task."""
    repo_path = resolve_repo_path(repo)
    if not repo_path.exists() or not repo_path.is_dir():
        typer.secho(f"Error: Path '{repo_path}' is not a valid directory.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Building context pack for repository: {repo_path} (budget: {budget})")
    builder = ContextPackBuilder()
    try:
        result = asyncio.run(
            builder.build_context_pack(task_description, repo_path, budget, debug_retrieval=debug_retrieval)
        )
        typer.secho("\nContext pack compiled successfully!", fg=typer.colors.GREEN, bold=True)
        typer.echo(f"Task Type:         {result.get('task_type')}")
        typer.echo(f"Critic Status:     {result.get('critic_status')}")
        typer.echo(f"Extracted Keywords: {', '.join(result.get('keywords', []))}")
        typer.echo(f"Output File Path:  {result.get('path')}")
    except Exception as e:
        typer.secho(f"Failed to build context pack: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
