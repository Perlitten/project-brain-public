import typer
import asyncio
from typing import Optional
from brain.config.paths import resolve_repo_path
from brain.analyzers.diff_analyzer import DiffAnalyzer

def review_diff_command(
    base: Optional[str] = typer.Option(
        None,
        "--base",
        help="Base branch/commit (defaults to the repository's origin/HEAD)",
    ),
    head: Optional[str] = typer.Option("current", "--head", help="Head branch/commit (defaults to 'current' for unstaged/working modifications)"),
    repo: Optional[str] = typer.Option(
        None,
        "--repo",
        help="Path to the repository to review. Defaults to REPO_ROOT or the current directory.",
    )
):
    """Review git diff against architectural rules and test requirements."""
    repo_path = resolve_repo_path(repo)
    if not repo_path.exists() or not repo_path.is_dir():
        typer.secho(f"Error: Path '{repo_path}' is not a valid directory.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Auditing diff in repository '{repo_path}' between {base} and {head}...")
    analyzer = DiffAnalyzer(repo_path)
    try:
        result = asyncio.run(analyzer.review_diff(base=base, head=head))
        if result.get("status") == "failed":
            typer.secho(f"Failed to review diff: {result.get('error')}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)

        typer.secho("\n=== Diff Review Summary ===", fg=typer.colors.CYAN, bold=True)
        typer.echo(f"Final Status:     {str(result.get('status') or '').upper()}")
        typer.echo(f"Modified Files:   {len(result.get('modified_files', []))}")

        violations = result.get("rule_violations", [])
        typer.echo(f"Rule Violations:  {len(violations)}")
        for v in violations:
            typer.secho(f"  - [{v.get('rule_id')}] {v.get('details')}", fg=typer.colors.RED)

        missing = result.get("missing_tests", [])
        typer.echo(f"Missing Tests:    {len(missing)}")
        for m in missing:
            typer.secho(f"  - {m}", fg=typer.colors.YELLOW)

        typer.echo(f"\nReport written to: {result.get('report_path')}")
    except Exception as e:
        typer.secho(f"Diff review failed: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
