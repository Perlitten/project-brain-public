import typer
import asyncio
from brain.analyzers.impact_analyzer import ImpactAnalyzer
from brain.config.paths import resolve_repo_path

def impact_command(
    change_request: str = typer.Argument(..., help="Natural language description of the proposed change request"),
    repo: str = typer.Option(None, "--repo", help="Indexed repository path"),
):
    """Analyze directly and indirectly affected files, screens, and database entities for a change request."""
    typer.echo("Starting impact analysis...")
    analyzer = ImpactAnalyzer(resolve_repo_path(repo))
    try:
        result = asyncio.run(analyzer.analyze_impact(change_request))
        typer.secho("\n=== Impact Analysis Result ===", fg=typer.colors.CYAN, bold=True)
        typer.echo(f"Assessed Risk Level: {result.get('risk_level')}")
        typer.echo(f"Risk Score:          {result.get('risk_score')}/100")
        typer.echo("\nDirectly Affected Surfaces:")
        for surf in result.get("directly_affected", []):
            typer.echo(f"  - {surf}")
        typer.echo("\nIndirectly Affected Surfaces:")
        for surf in result.get("indirectly_affected", []):
            typer.echo(f"  - {surf}")
        typer.echo(f"\nWritten Report Path: {result.get('report_path')}")
    except Exception as e:
        typer.secho(f"Impact analysis failed: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
