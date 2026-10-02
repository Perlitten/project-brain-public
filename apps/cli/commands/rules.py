import typer
import asyncio
from typing import Optional
import json
from brain.memory.rule_store import RuleStore

rules_cli = typer.Typer(help="Manage compliance and architecture rules.")

@rules_cli.command(name="add")
def add_rule_cmd(
    name: str = typer.Argument(..., help="Name of the rule"),
    repo_path: Optional[str] = typer.Option(None, "--repo", help="Canonical repository scope"),
    description: Optional[str] = typer.Option(None, "--desc", help="Rule description"),
    rule_type: Optional[str] = typer.Option("architecture", "--type", help="Rule type (e.g. architecture, design, security)"),
    severity: Optional[str] = typer.Option("medium", "--severity", help="Severity level (e.g. low, medium, high, critical)"),
    status: Optional[str] = typer.Option("active", "--status", help="Status of the rule"),
    applies_to_json: Optional[str] = typer.Option(None, "--applies-to", help="JSON string defining modules/features this applies to"),
    rule_id: Optional[str] = typer.Option(None, "--id", help="Optional custom slug rule ID")
):
    """Add a new rule or update an existing rule."""
    applies_to = None
    if applies_to_json:
        try:
            applies_to = json.loads(applies_to_json)
        except json.JSONDecodeError:
            typer.secho("Error: --applies-to must be valid JSON.", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)

    try:
        r_id = asyncio.run(RuleStore.add_rule(
            name=name,
            repo_path=repo_path,
            description=description,
            type=rule_type,
            severity=severity,
            status=status,
            applies_to=applies_to,
            rule_id=rule_id
        ))
        typer.secho(f"Successfully added/updated rule: '{r_id}'", fg=typer.colors.GREEN)
    except Exception as e:
        typer.secho(f"Failed to add rule: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

@rules_cli.command(name="list")
def list_rules_cmd():
    """List all rules."""
    try:
        rules = asyncio.run(RuleStore.list_rules())
    except Exception as e:
        typer.secho(f"Failed to list rules: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if not rules:
        typer.echo("No rules found.")
        return

    for r in rules:
        typer.echo(f"ID: {r.id} | Name: {r.name} | [{r.status.upper()}] | Severity: {r.severity.upper()} | Type: {r.type}")
        if r.description:
            typer.echo(f"  Description: {r.description}")

@rules_cli.command(name="sync")
def sync_rules_cmd(
    yaml_path: str = typer.Argument(..., help="Path to the rules YAML file")
):
    """Synchronize rules from a YAML configuration file."""
    try:
        asyncio.run(RuleStore.sync_rules_from_yaml(yaml_path))
        typer.secho(f"Successfully synchronized rules from {yaml_path}", fg=typer.colors.GREEN)
    except Exception as e:
        typer.secho(f"Failed to sync rules: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
