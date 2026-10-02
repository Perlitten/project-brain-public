from pathlib import Path
from typing import Optional

import typer
import yaml
import asyncio

from brain.config.paths import resolve_repo_path
from brain.context.evaluator import GoldenTaskEvaluator

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _validate_eval_yaml(path: Path) -> list[str]:
    """Return list of validation errors (empty if valid)."""
    errors: list[str] = []
    if not path.exists():
        return [f"File not found: {path}"]
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"YAML parse error: {exc}"]
    if not isinstance(data, dict):
        return ["Root must be a mapping"]
    tasks_key = None
    for key in ("golden_tasks",):
        if key in data:
            tasks_key = key
            break
    if tasks_key is None:
        return ["Expected golden_tasks key"]
    tasks = data[tasks_key]
    if not isinstance(tasks, list) or not tasks:
        return [f"{tasks_key} must be a non-empty list"]
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            errors.append(f"Task {i}: must be a mapping")
            continue
        if not task.get("id"):
            errors.append(f"Task {i}: missing id")
        if not task.get("description"):
            errors.append(f"Task {i}: missing description")
    return errors


def eval_command(
    repo: Optional[str] = typer.Option(
        None,
        "--repo",
        help="Path to the target repository. Defaults to REPO_ROOT or the current directory.",
    ),
    golden: str = typer.Option(
        "rules/golden_tasks.yaml",
        "--golden",
        help="Path to the golden tasks YAML configuration file.",
    ),
    validate_only: bool = typer.Option(
        False,
        "--validate",
        help="Only validate the selected golden task YAML file and exit.",
    ),
):
    """Run context quality evaluation against a caller-provided golden task file."""

    repo_path = resolve_repo_path(repo)
    if not repo_path.exists() or not repo_path.is_dir():
        typer.secho(f"Error: Path '{repo_path}' is not a valid directory.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    golden_path = Path(golden)
    if not golden_path.is_absolute():
        resolved_golden = repo_path / golden
        if not resolved_golden.exists():
            resolved_golden = Path.cwd() / golden
    else:
        resolved_golden = golden_path

    if validate_only:
        errors = _validate_eval_yaml(resolved_golden)
        if errors:
            typer.secho(f"Eval YAML invalid ({resolved_golden}):", fg=typer.colors.RED, err=True)
            for err in errors:
                typer.echo(f"  - {err}")
            raise typer.Exit(code=1)
        typer.secho(f"Eval YAML valid: {resolved_golden}", fg=typer.colors.GREEN, bold=True)
        return

    typer.echo(f"Starting Context Quality Evaluation against repository: {repo_path}")
    evaluator = GoldenTaskEvaluator(golden_path=resolved_golden)
    try:
        result = asyncio.run(evaluator.run_evaluation(repo_path))
        typer.secho("\nEvaluation Completed Successfully!", fg=typer.colors.GREEN, bold=True)
        typer.echo(f"Average Precision@10: {result['avg_precision']}")
        typer.echo(f"Average Recall@10:    {result['avg_recall']}")
        typer.echo(f"Detailed Report Path:  {result['report_path']}")

        typer.echo("\n--- Golden Tasks Metrics Table ---")
        typer.echo(f"{'Task Description':<60} | {'Prec@10':<7} | {'Recall@10':<9} | {'Critic Status':<15}")
        typer.echo("-" * 102)
        for t in result["tasks"]:
            desc = t["description"]
            if len(desc) > 57:
                desc = desc[:57] + "..."
            typer.echo(f"{desc:<60} | {t['precision']:<7.2f} | {t['recall']:<9.2f} | {t['critic_status']:<15}")
    except Exception as e:
        typer.secho(f"Evaluation failed: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
