import typer
from typing import Optional

from brain.config.paths import reports_dir, resolve_repo_path
from brain.indexers.repo_indexer import RepoIndexer


def audit_command(
    repo: Optional[str] = typer.Option(
        None,
        "--repo",
        help="Path to the repository to audit. Defaults to REPO_ROOT or the current directory.",
    )
):
    """Scan a repository and generate an audit report."""
    repo_path = resolve_repo_path(repo)
    if not repo_path.exists() or not repo_path.is_dir():
        typer.secho(f"Error: Path '{repo_path}' is not a valid directory.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Starting repository audit for '{repo_path}'...")

    try:
        indexer = RepoIndexer(repo_path)
        report_file = reports_dir(repo_path) / "initial-audit.md"
        data = indexer.scan(repo_path=repo_path, report_path=report_file)
    except Exception as e:
        typer.secho(f"Audit scan failed: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.secho("\n=== Audit Scan Summary ===", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"Repository Path: {data.get('repo_path')}")
    typer.echo(f"Commit Hash:     {data.get('commit_hash')}")
    typer.echo(f"Timestamp:       {data.get('timestamp')}")
    typer.echo(f"Total Src Files: {data.get('total_source_files')}")

    typer.echo("\nLanguages Detected:")
    langs = data.get("languages", {})
    if langs:
        for lang, stats in langs.items():
            typer.echo(f"  - {lang}: {stats.get('count')} files ({stats.get('lines'):,} lines)")
    else:
        typer.echo("  - None")

    typer.echo("\nFrameworks Detected:")
    frameworks = data.get("frameworks", [])
    if frameworks:
        for fw in frameworks:
            typer.echo(f"  - {fw}")
    else:
        typer.echo("  - None")

    typer.echo("\nBuild Systems:")
    build_systems = data.get("build_systems", [])
    if build_systems:
        for bs in build_systems:
            typer.echo(f"  - {bs}")
    else:
        typer.echo("  - None")

    typer.echo("\nTest Suites:")
    test_suites = data.get("test_suites", [])
    if test_suites:
        for ts in test_suites:
            typer.echo(f"  - {ts}")
    else:
        typer.echo("  - None")

    typer.secho("\nAudit completed successfully!", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"Report written to: {report_file.resolve().as_posix()}")
