import typer
import asyncio
from typing import Optional
from brain.memory.decision_store import DecisionStore

decision_cli = typer.Typer(help="Manage technical and architectural decisions (ADRs).")

@decision_cli.command(name="add")
def add_decision_cmd(
    title: str = typer.Argument(..., help="Title of the decision"),
    description: Optional[str] = typer.Option(None, "--desc", help="Detailed description"),
    status: Optional[str] = typer.Option("active", "--status", help="Status of the decision"),
    reason: Optional[str] = typer.Option(None, "--reason", help="Why this decision was made"),
    consequences: Optional[str] = typer.Option(None, "--consequences", help="Impact/consequences of the decision"),
    features: Optional[str] = typer.Option(None, "--features", help="Comma-separated list of affected features"),
    modules: Optional[str] = typer.Option(None, "--modules", help="Comma-separated list of affected modules"),
    files: Optional[str] = typer.Option(None, "--files", help="Comma-separated list of affected files")
):
    """Add a new architectural decision (ADR) to the store."""
    features_list = [f.strip() for f in features.split(",")] if features else []
    modules_list = [m.strip() for m in modules.split(",")] if modules else []
    files_list = [f.strip() for f in files.split(",")] if files else []

    try:
        dec_id = asyncio.run(DecisionStore.add_decision(
            title=title,
            description=description,
            status=status,
            reason=reason,
            consequences=consequences,
            affected_features=features_list,
            affected_modules=modules_list,
            affected_files=files_list
        ))
        typer.secho(f"Successfully added decision with ID {dec_id}", fg=typer.colors.GREEN)
    except Exception as e:
        typer.secho(f"Failed to add decision: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

@decision_cli.command(name="list")
def list_decisions_cmd():
    """List all recorded decisions."""
    try:
        decs = asyncio.run(DecisionStore.list_decisions())
    except Exception as e:
        typer.secho(f"Failed to list decisions: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if not decs:
        typer.echo("No decisions found.")
        return

    for d in decs:
        typer.echo(f"ID {d.id} | [{str(d.status or '').upper()}] {d.title} (Date: {d.date})")
        if d.description:
            typer.echo(f"  Description: {d.description}")

@decision_cli.command(name="search")
def search_decisions_cmd(
    query: str = typer.Argument(..., help="Query term to search in title/description")
):
    """Search decisions by keyword in title/description."""
    try:
        decs = asyncio.run(DecisionStore.search_decisions(query))
    except Exception as e:
        typer.secho(f"Failed to search decisions: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if not decs:
        typer.echo(f"No decisions matched query '{query}'.")
        return

    for d in decs:
        typer.echo(f"ID {d.id} | [{str(d.status or '').upper()}] {d.title} (Date: {d.date})")

@decision_cli.command(name="deprecate")
def deprecate_decision_cmd(
    decision_id: int = typer.Argument(..., help="ID of decision to deprecate")
):
    """Deprecate a decision by its ID."""
    try:
        success = asyncio.run(DecisionStore.deprecate_decision(decision_id))
        if success:
            typer.secho(f"Successfully deprecated decision with ID {decision_id}", fg=typer.colors.GREEN)
        else:
            typer.secho(f"Decision with ID {decision_id} not found.", fg=typer.colors.YELLOW)
    except Exception as e:
        typer.secho(f"Failed to deprecate decision: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
