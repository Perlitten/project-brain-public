import asyncio
import typer
from brain.database.session import check_health

def health_command():
    """Check health of databases (PostgreSQL, Redis, Neo4j)."""
    typer.echo("Checking database connections...")
    try:
        results = asyncio.run(check_health())
    except Exception as e:
        typer.secho(f"Error executing health check: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    all_healthy = True
    for service, status_info in results.items():
        status = status_info.get("status")
        if status == "healthy":
            msg = status_info.get("message", "OK")
            typer.secho(f"[{service.upper()}] Status: {status.upper()} - {msg}", fg=typer.colors.GREEN)
        else:
            err = status_info.get("error", "Unknown error")
            typer.secho(f"[{service.upper()}] Status: {status.upper()} - {err}", fg=typer.colors.RED, bold=True)
            all_healthy = False

    if all_healthy:
        typer.secho("\nAll database services are HEALTHY.", fg=typer.colors.GREEN, bold=True)
    else:
        typer.secho("\nSome database services are UNHEALTHY.", fg=typer.colors.RED, bold=True)
        raise typer.Exit(code=1)
