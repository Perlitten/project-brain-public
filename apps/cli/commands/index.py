import asyncio
import typer
from typing import Optional

from brain.config.paths import resolve_repo_path
from brain.indexers.file_indexer import FileIndexer


def index_command(
    repo: Optional[str] = typer.Option(
        None,
        "--repo",
        help="Path to the repository to index. Defaults to REPO_ROOT or the current directory.",
    ),
    clean: bool = typer.Option(
        False,
        "--clean",
        help="Delete existing indexed data for this repository before re-indexing.",
    ),
):
    """Scan and index the repository codebase, generating chunks and embeddings."""
    repo_path = resolve_repo_path(repo)
    if not repo_path.exists() or not repo_path.is_dir():
        typer.secho(f"Error: Path '{repo_path}' is not a valid directory.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if clean:
        typer.echo("Clean mode: wiping existing index data for this repository...")
    typer.echo(f"Indexing repository codebase at '{repo_path}'...")
    indexer = FileIndexer()

    async def run_indexing():
        from brain.database.session import init_db
        await init_db()
        return await indexer.index_repository(repo_path, clean=clean)

    try:
        repo_record = asyncio.run(run_indexing())
        typer.secho("\nIndexing completed successfully!", fg=typer.colors.GREEN, bold=True)
        typer.echo(f"Repository ID:   {repo_record.id}")
        typer.echo(f"Repository Name: {repo_record.name}")
        typer.echo(f"Last Commit:     {repo_record.last_indexed_commit}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        typer.secho(f"Indexing failed: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
