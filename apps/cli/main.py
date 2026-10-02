import typer
from brain.version import build_info
from apps.cli.commands.doctor import doctor_command
from apps.cli.commands.health import health_command
from apps.cli.commands.audit import audit_command
from apps.cli.commands.index import index_command
from apps.cli.commands.graph import graph_cli
from apps.cli.commands.decision import decision_cli
from apps.cli.commands.rules import rules_cli
from apps.cli.commands.context import context_command
from apps.cli.commands.impact import impact_command
from apps.cli.commands.review_diff import review_diff_command
from apps.cli.commands.eval import eval_command
from apps.cli.commands.cache import cache_app
from apps.cli.commands.embeddings import embeddings_app
from apps.cli.commands.import_cmd import import_app

# The main Typer application instance (referenced as the entrypoint in pyproject.toml)
cli = typer.Typer(
    name="brain",
    help="Project Brain command-line interface for foundation services, database health, and auditing.",
    invoke_without_command=True,
)


def _version_callback(value: bool):
    if value:
        info = build_info()
        typer.echo(f"{info['name']} {info['version']} ({info['release_codename']})")
        raise typer.Exit()


@cli.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show Project Brain version and exit.",
    ),
):
    pass

# Register the subcommands
cli.command(name="doctor")(doctor_command)
cli.command(name="health")(health_command)
cli.command(name="audit")(audit_command)
cli.command(name="index")(index_command)
cli.add_typer(graph_cli, name="graph")
cli.add_typer(decision_cli, name="decision")
cli.add_typer(rules_cli, name="rules")
cli.command(name="context")(context_command)
cli.command(name="impact")(impact_command)
cli.command(name="review-diff")(review_diff_command)
cli.command(name="eval")(eval_command)
cli.add_typer(cache_app, name="cache")
cli.add_typer(embeddings_app, name="embeddings")
cli.add_typer(import_app, name="import")


if __name__ == "__main__":
    cli()
