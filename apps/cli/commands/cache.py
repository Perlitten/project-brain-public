import typer
import asyncio
from brain.config.paths import resolve_repo_path
from brain.context.context_pack_builder import ContextPackBuilder
from brain.indexers.repo_indexer import get_git_commit_hash

cache_app = typer.Typer(help="Manage and inspect the Stable Memory Cache.")

@cache_app.command("status")
def cache_status(
    repo: str = typer.Option(
        None,
        "--repo",
        help="Path to the target repository."
    )
):
    """Expose the Stable Memory Cache status, invalidation rules, and contents."""
    repo_path = resolve_repo_path(repo)
    commit_hash = get_git_commit_hash(repo_path)

    typer.echo("Initializing cache from database...")
    builder = ContextPackBuilder()

    # Initialize the static cache
    asyncio.run(builder._cache.initialize())

    cache = builder._cache

    typer.secho("\n--- Stable Memory Cache Status ---", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"Initialized:           {cache.initialized}")
    typer.echo(f"Repository Commit:      {commit_hash}")
    typer.echo(f"Loaded Rules Count:     {len(cache.rules)}")
    typer.echo(f"Loaded Decisions Count: {len(cache.decisions)}")
    typer.echo(f"Loaded Files Count:     {len(cache.files)}")
    typer.echo(f"Loaded Symbols Count:   {len(cache.symbols)}")

    typer.echo("\n--- Invalidation & Lifecycles ---")
    typer.echo("Invalidation rules:")
    typer.echo("  1. Repository Re-indexing: Invalidates current in-memory cache and reloads from DB.")
    typer.echo("  2. Schema changes / manual flush: Forces reload.")
    typer.echo("Invalidation Behavior: Cache is bound to repo commit hash and SQL tables state. When a new index is run, cache re-reads PostgreSQL.")

    # Print some cache keys or summaries if loaded
    if cache.rules:
        typer.echo("\nActive Cache Rules:")
        for r in cache.rules[:3]:
            typer.echo(f"  - [{r.id}] {r.name}")
    if cache.decisions:
        typer.echo("\nActive Cache Decisions:")
        for d in cache.decisions[:3]:
            typer.echo(f"  - [{d.id}] {d.title}")
