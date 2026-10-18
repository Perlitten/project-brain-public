"""Shared CLI handling for memory writes made as ``BRAIN_LOCAL_PRINCIPAL_KEY``.

Without the key the CLI is the operator (local administrator) and writes
directly; with it, writes follow the same team-state governance as the HTTP API.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine, Literal, Optional, TypeVar

import typer

from brain.config.settings import settings

T = TypeVar("T")


def _local_key_configured() -> bool:
    return bool((settings.BRAIN_LOCAL_PRINCIPAL_KEY or "").strip())


def write_as_local_principal(kind: Literal["decision", "rule"], fields: dict[str, Any]) -> bool:
    """True when a configured local principal handled the write (output printed); False: operator write."""
    if not _local_key_configured():
        return False
    from apps.api import local_memory  # lazy: keeps FastAPI out of the operator's CLI path

    result: Optional[dict[str, Any]] = asyncio.run(local_memory.local_write(kind, fields))
    if result is None:
        return False
    status = result.get("status")
    if status == "success":
        row_id = result.get("decision_id") if kind == "decision" else result.get("rule_id")
        typer.secho(f"Successfully recorded {kind} with ID {row_id}", fg=typer.colors.GREEN)
        return True
    if status == "proposed":
        typer.secho(
            f"Submitted {kind} proposal #{result.get('proposal_id')} for review; "
            "it is not approved knowledge until an independent reviewer approves it.",
            fg=typer.colors.YELLOW,
        )
        return True
    typer.secho(f"Failed to record {kind} ({status}): {result.get('error')}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def run_as_operator(action: str, work: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run a bulk command that writes shared memory directly; refuse it for a governed local principal.

    The check and the work share one event loop: pooled database connections are bound to the loop.
    A refusal raises ``LocalPrincipalError``, which the command reports as its failure.
    """
    if not _local_key_configured():
        return asyncio.run(work())
    from apps.api import local_memory

    async def _guarded() -> T:
        await local_memory.require_local_administrator(action)
        return await work()

    return asyncio.run(_guarded())
