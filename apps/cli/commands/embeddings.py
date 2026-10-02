"""CLI commands for embedding integrity reporting."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import typer

from brain.config.settings import settings
from brain.database.repository_utils import get_repository_by_path
from brain.database.session import init_db
from brain.embeddings.backfill import backfill_embeddings
from brain.embeddings.integrity import collect_embedding_inventory, verify_embeddings
from brain.late_interaction.backfill import backfill_late_interaction
from brain.late_interaction.provider import (
    close_lfm_colbert_provider,
    get_lfm_colbert_provider,
)
from brain.late_interaction.remote_sync import sync_remote_late_interaction
from brain.late_interaction.store import collect_late_interaction_inventory

embeddings_app = typer.Typer(help="Embedding integrity status and verification.")


async def _resolve_repo_async(repo: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    repo_path = Path(repo or settings.TARGET_REPO_PATH).resolve()
    record = await get_repository_by_path(repo_path)
    if record:
        return record.id, record.path
    return None, repo_path.as_posix()


@embeddings_app.command("status")
def embeddings_status(
    repo: Optional[str] = typer.Option(None, "--repo", help="Repository path (default: TARGET_REPO_PATH)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Report eligible chunks, current/stale/incompatible embeddings, pgvector coverage."""

    async def _run():
        await init_db()
        repository_id, repository_path = await _resolve_repo_async(repo)
        return await collect_embedding_inventory(repository_id, repository_path)

    inventory = asyncio.run(_run())
    payload = inventory.to_dict()

    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return

    typer.echo(f"Repository: {payload['repository_path']} (id={payload['repository_id']})")
    typer.echo(f"Configured: {payload['configured']}")
    typer.echo(f"Eligible chunks: {payload['total_eligible_chunks']}")
    typer.echo(f"Current embeddings: {payload['chunks_with_current_embeddings']}")
    typer.echo(f"Stale: {payload['stale_embeddings']}")
    typer.echo(f"Incompatible: {payload['incompatible_embeddings']}")
    typer.echo(f"Missing: {payload['missing_embeddings']}")
    typer.echo(f"pgvector populated: {payload['pgvector_populated']} ({payload['pgvector_coverage_pct']}%)")
    typer.echo(f"pgvector extension: {payload['pgvector_extension']}")
    typer.echo(f"pgvector index: {payload['pgvector_index_exists']}")


@embeddings_app.command("verify")
def embeddings_verify(
    repo: Optional[str] = typer.Option(None, "--repo", help="Repository path (default: TARGET_REPO_PATH)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Verify all eligible chunks use configured model/dimension with pgvector populated."""

    async def _run():
        await init_db()
        repository_id, repository_path = await _resolve_repo_async(repo)
        return await verify_embeddings(repository_id, repository_path)

    report = asyncio.run(_run())

    if json_output:
        typer.echo(json.dumps(report, indent=2))
    else:
        typer.echo(json.dumps(report, indent=2))

    if report["pass"]:
        typer.secho("PASS", fg=typer.colors.GREEN, bold=True)
        raise typer.Exit(code=0)
    typer.secho("FAIL", fg=typer.colors.RED, bold=True)
    for issue in report.get("issues", []):
        typer.echo(f"  - {issue}")
    raise typer.Exit(code=1)


@embeddings_app.command("backfill")
def embeddings_backfill(
    repo: Optional[str] = typer.Option(None, "--repo", help="Repository path (default: TARGET_REPO_PATH)"),
    pgvector_only: bool = typer.Option(
        False,
        "--pgvector-only",
        help="Only sync JSON vector_data into pgvector (no API regeneration)",
    ),
    limit: Optional[int] = typer.Option(None, "--limit", help="Max chunks to regenerate"),
    batch_size: int = typer.Option(8, "--batch-size", help="Embedding API batch size"),
    provider: Optional[str] = typer.Option(None, "--provider", help="Override embedding provider"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report work items without writing"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Sync pgvector from JSON and regenerate incompatible embeddings."""

    async def _run():
        await init_db()
        repository_id, repository_path = await _resolve_repo_async(repo)
        result = await backfill_embeddings(
            repository_id,
            pgvector_only=pgvector_only,
            limit=limit,
            batch_size=batch_size,
            provider_name=provider,
            dry_run=dry_run,
        )
        inventory = await collect_embedding_inventory(repository_id, repository_path)
        return result, inventory

    result, inventory = asyncio.run(_run())
    payload = {
        **result.to_dict(),
        "coverage_after": inventory.to_dict(),
    }

    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(f"pgvector synced: {result.pgvector_synced}")
        typer.echo(f"regenerated: {result.regenerated}")
        typer.echo(f"failed: {result.failed}")
        if dry_run:
            typer.echo(f"would regenerate: {result.skipped}")
        typer.echo(
            f"Coverage after: {inventory.pgvector_populated}/{inventory.total_eligible_chunks} "
            f"({inventory.pgvector_coverage_pct}%)"
        )

    if result.failed:
        raise typer.Exit(code=1)


@embeddings_app.command("late-status")
def late_interaction_status(
    repo: Optional[str] = typer.Option(None, "--repo", help="Repository path (default: TARGET_REPO_PATH)"),
    probe: bool = typer.Option(False, "--probe", help="Also probe the loopback ColBERT sidecar"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Report additive ColBERT coverage without changing dense embeddings."""

    async def _run():
        await init_db()
        repository_id, repository_path = await _resolve_repo_async(repo)
        if repository_id is None:
            raise LookupError(f"Repository is not indexed: {repository_path}")
        inventory = await collect_late_interaction_inventory(repository_id)
        provider_health = None
        if probe:
            try:
                provider_health = await get_lfm_colbert_provider().health()
            finally:
                await close_lfm_colbert_provider()
        return repository_path, inventory, provider_health

    repository_path, inventory, provider_health = asyncio.run(_run())
    payload = {
        "repository_path": repository_path,
        "inventory": inventory.to_dict(),
        "provider_health": provider_health,
        "gates": {
            "enabled": settings.LATE_INTERACTION_ENABLED,
            "dual_write": settings.LATE_INTERACTION_DUAL_WRITE_ENABLED,
            "shadow": settings.LATE_INTERACTION_SHADOW_ENABLED,
            "shadow_persistence": settings.LATE_INTERACTION_SHADOW_PERSIST_ENABLED,
            "rerank": settings.LATE_INTERACTION_RERANK_ENABLED,
            "canary_percent": settings.LATE_INTERACTION_CANARY_PERCENT,
            "experiment_authorized": settings.LATE_INTERACTION_EXPERIMENT_AUTHORIZED,
            "production_gates_passed": (
                settings.LATE_INTERACTION_PRODUCTION_GATES_PASSED
            ),
            "approved_repository_id": (
                settings.LATE_INTERACTION_APPROVED_REPOSITORY_ID
            ),
            "approved_build_sha": settings.LATE_INTERACTION_APPROVED_BUILD_SHA,
            "approved_source_digest": (
                settings.LATE_INTERACTION_APPROVED_SOURCE_DIGEST
            ),
            "approved_model_revision": (
                settings.LATE_INTERACTION_APPROVED_MODEL_REVISION
            ),
            "approved_index_revision": (
                settings.LATE_INTERACTION_APPROVED_INDEX_REVISION
            ),
            "approved_lineage_id": settings.LATE_INTERACTION_APPROVED_LINEAGE_ID,
            "approved_identity_digest": (
                settings.LATE_INTERACTION_APPROVED_IDENTITY_DIGEST
            ),
            "approved_document_count": (
                settings.LATE_INTERACTION_APPROVED_DOCUMENT_COUNT
            ),
            "production_evidence_path": (
                settings.LATE_INTERACTION_PRODUCTION_EVIDENCE_PATH
            ),
            "production_evidence_sha256": (
                settings.LATE_INTERACTION_PRODUCTION_EVIDENCE_SHA256
            ),
        },
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(f"Repository: {repository_path} (id={inventory.repository_id})")
    typer.echo(
        f"Current: {inventory.current_chunks}/{inventory.total_chunks} "
        f"({inventory.coverage_pct}%), stale={inventory.stale_chunks}, missing={inventory.missing_chunks}"
    )
    typer.echo(f"Stored: {inventory.stored_bytes} bytes, token vectors={inventory.token_vectors}")
    if provider_health:
        typer.echo(f"Sidecar: {provider_health['status']} ({provider_health['model']})")


@embeddings_app.command("late-backfill")
def late_interaction_backfill(
    repo: Optional[str] = typer.Option(None, "--repo", help="Repository path (default: TARGET_REPO_PATH)"),
    limit: int = typer.Option(
        100,
        "--limit",
        min=1,
        help="Maximum chunks in this bounded resumable batch",
    ),
    write: bool = typer.Option(False, "--write", help="Persist vectors; without this flag the command is dry-run"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Backfill ColBERT matrices; dry-run unless --write is explicit."""

    async def _run():
        await init_db()
        repository_id, repository_path = await _resolve_repo_async(repo)
        if repository_id is None:
            raise LookupError(f"Repository is not indexed: {repository_path}")
        try:
            result = await backfill_late_interaction(repository_id, limit=limit, dry_run=not write)
            inventory = await collect_late_interaction_inventory(repository_id)
            return repository_path, result, inventory
        finally:
            await close_lfm_colbert_provider()

    repository_path, result, inventory = asyncio.run(_run())
    payload = {
        "repository_path": repository_path,
        "result": result.to_dict(),
        "coverage_after": inventory.to_dict(),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(
            f"Repository: {repository_path}; examined={result.examined}, written={result.written}, "
            f"current_seen={result.current}, failed={result.failed}, truncated={result.truncated}, "
            f"dry_run={result.dry_run}; coverage={inventory.coverage_pct}%"
        )
    if result.failed:
        raise typer.Exit(code=1)


@embeddings_app.command("late-remote-status")
def late_interaction_remote_status(
    repo: Optional[str] = typer.Option(
        None,
        "--repo",
        help="Repository path (default: TARGET_REPO_PATH)",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON",
    ),
):
    """Report the co-located GPU matrix inventory for one repository."""

    async def _run():
        from brain.late_interaction.client import get_late_interaction_client

        await init_db()
        repository_id, repository_path = await _resolve_repo_async(repo)
        if repository_id is None:
            raise LookupError(f"Repository is not indexed: {repository_path}")
        status = await get_late_interaction_client().status(repository_id)
        return repository_path, status

    repository_path, status = asyncio.run(_run())
    payload = {
        "repository_path": repository_path,
        **status.__dict__,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(
            f"Repository: {repository_path} (id={status.repository_id}); "
            f"remote={status.status}; documents={status.document_count}; "
            f"bytes={status.bytes}; model={status.model_revision}; "
            f"index={status.index_revision}"
        )
    if status.status != "ready":
        raise typer.Exit(code=1)


@embeddings_app.command("late-remote-sync")
def late_interaction_remote_sync(
    repo: Optional[str] = typer.Option(
        None,
        "--repo",
        help="Repository path (default: TARGET_REPO_PATH)",
    ),
    batch_size: Optional[int] = typer.Option(
        None,
        "--batch-size",
        min=1,
        max=64,
        help=(
            "Documents per authenticated service mutation "
            "(default: LATE_INTERACTION_REMOTE_SYNC_BATCH_SIZE)"
        ),
    ),
    max_chunks: Optional[int] = typer.Option(
        None,
        "--max-chunks",
        min=1,
        help="Optional bounded canary batch; omit for a complete sync",
    ),
    write: bool = typer.Option(
        False,
        "--write",
        help="Write matrices; without this flag the command is a DB-only dry-run",
    ),
    prune: bool = typer.Option(
        False,
        "--prune",
        help="After a complete write, remove remote chunk IDs absent from Postgres",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON",
    ),
):
    """Synchronize Postgres chunks to the co-located GPU matrix store."""

    async def _run():
        await init_db()
        repository_id, repository_path = await _resolve_repo_async(repo)
        if repository_id is None:
            raise LookupError(f"Repository is not indexed: {repository_path}")
        result = await sync_remote_late_interaction(
            repository_id,
            batch_size=batch_size,
            max_chunks=max_chunks,
            dry_run=not write,
            prune=prune,
        )
        return repository_path, result

    repository_path, result = asyncio.run(_run())
    payload = {
        "repository_path": repository_path,
        **result.to_dict(),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(
            f"Repository: {repository_path}; examined={result.examined}; "
            f"accepted={result.accepted}; unchanged={result.unchanged}; "
            f"deleted={result.deleted}; skipped_empty={result.skipped_empty}; "
            f"failed_batches={result.failed_batches}; "
            f"dry_run={result.dry_run}; index={result.index_revision or 'n/a'}"
        )
    if not result.passed:
        raise typer.Exit(code=1)
