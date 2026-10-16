"""REST API Router for the evidence ledger — Phase F6.

Read-only by design. Ledger writes occur through product actions elsewhere in
Project Brain; there is deliberately no generic external write endpoint, and no
endpoint that can amend or remove an event.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from apps.api.auth import require_api_key, require_principal, require_scope
from apps.api.registry_access import registry_authority
from apps.api.team_access import repository_authority, team_administrator_access
from brain.auth.principals import ResolvedPrincipal
from brain.ledger.ledger import EvidenceLedger
from brain.ledger.store import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from brain.team.authorization import Permission
from brain.workspace.registry_store import RegistryStore

router = APIRouter(
    prefix="/ledger",
    tags=["evidence-ledger"],
    dependencies=[Depends(require_api_key), Depends(require_scope("ledger:read"))],
)


def _ledger() -> EvidenceLedger:
    return EvidenceLedger(Path(".brain"))


def _registry() -> RegistryStore:
    return RegistryStore(Path(".brain/workspace"))


async def _authorize_repository(principal: ResolvedPrincipal, repository_id: str) -> None:
    """Read on the registry repository behind a ledger ``repository_id``; empty or unknown is chain-wide."""
    await registry_authority(principal, Permission.READ, _registry().get(repository_id) if repository_id else None)


@router.get("/events", dependencies=[Depends(require_scope("ledger:read"))])
async def list_events(
    event_type: str = Query("", max_length=64),
    repository_id: str = Query("", max_length=128),
    repository_path: str = Query("", max_length=4096),
    entity_type: str = Query("", max_length=64),
    entity_id: str = Query("", max_length=256),
    actor_identity: str = Query("", max_length=256),
    q: str = Query("", max_length=500),
    descending: bool = False,
    since_sequence: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    principal: ResolvedPrincipal = Depends(require_principal),
):
    """Bounded, paginated query over the chain."""
    # Team state: a repository filter needs read on that repository; the whole chain needs a team administrator.
    if repository_id or not repository_path:
        await _authorize_repository(principal, repository_id)
    else:
        await repository_authority(principal, Permission.READ, repo_path=repository_path)
    if repository_path and not repository_id:
        from brain.workspace.models import _generate_repository_id
        repository_id = _generate_repository_id(repository_path)
    return _ledger().query(
        event_type=event_type,
        repository_id=repository_id,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_identity=actor_identity,
        query=q,
        descending=descending,
        since_sequence=since_sequence,
        limit=limit,
        offset=offset,
    )


@router.get("/verify", dependencies=[Depends(require_scope("ledger:read")), Depends(team_administrator_access())])
async def verify_chain():
    """Full chain verification with typed issue codes."""
    return _ledger().verify()


@router.get("/health", dependencies=[Depends(require_scope("ledger:read")), Depends(team_administrator_access())])
async def ledger_health():
    return _ledger().health()


@router.get("/events/{event_id}", dependencies=[Depends(require_scope("ledger:read"))])
async def get_event(event_id: str, principal: ResolvedPrincipal = Depends(require_principal)):
    event = _ledger().get(event_id)
    await _authorize_repository(principal, event.repository_id if event is not None else "")
    if event is None:
        raise HTTPException(status_code=404, detail=f"Unknown ledger event: {event_id}")
    payload = event.to_dict()
    payload["hash_valid"] = event.compute_hash() == event.event_hash
    return payload


@router.get("/entities/{entity_type}/{entity_id}",
            dependencies=[Depends(require_scope("ledger:read")), Depends(team_administrator_access())])
async def entity_history(
    entity_type: str,
    entity_id: str,
    limit: int = Query(MAX_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
):
    result = _ledger().entity_history(entity_type, entity_id, limit=limit, offset=offset)
    result["entity_type"] = entity_type
    result["entity_id"] = entity_id
    return result
