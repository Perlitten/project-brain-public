import typer
import asyncio
from brain.config.paths import resolve_repo_path
from brain.database.repository_utils import require_repository_by_path
from brain.graph.graph_client import GraphClient

graph_cli = typer.Typer(help="Graph database operations.")

@graph_cli.command(name="neighbors")
def neighbors_command(
    name_or_path: str = typer.Argument(..., help="Name or path of the starting node"),
    repo: str = typer.Option(None, "--repo", help="Indexed repository path"),
):
    """Get directly connected nodes and relationships for a target node name or path."""
    typer.echo(f"Querying graph neighbors for: {name_or_path}")
    async def _run():
        repository = await require_repository_by_path(resolve_repo_path(repo))
        client = GraphClient(repository_id=repository.id)
        return await client.get_neighbors(name_or_path)

    try:
        results = asyncio.run(_run())
    except Exception as e:
        typer.secho(f"Failed to query neighbors: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if not results:
        typer.echo("No neighbors found or node does not exist.")
        return

    typer.secho(f"\nFound {len(results)} neighbor relationship(s):", fg=typer.colors.CYAN, bold=True)
    for r in results:
        src = r["source"]["properties"].get("name") or r["source"]["properties"].get("path", "Unknown")
        src_label = r["source"]["labels"][0] if r["source"]["labels"] else "Unknown"
        tgt = r["target"]["properties"].get("name") or r["target"]["properties"].get("path", "Unknown")
        tgt_label = r["target"]["labels"][0] if r["target"]["labels"] else "Unknown"
        rel = r["relationship"]["type"]
        typer.echo(f"  ({src_label}: {src}) -[{rel}]-> ({tgt_label}: {tgt})")
