"""REST API Router for the evidence ledger — Phase F6.

Read-only by design. Ledger writes occur through product actions elsewhere in
Project Brain; there is deliberately no generic external write endpoint, and no
endpoint that can amend or remove an event.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from apps.api.auth import require_api_key, require_scope
from brain.ledger.ledger import EvidenceLedger
from brain.ledger.store import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter(
    prefix="/ledger",
    tags=["evidence-ledger"],
    dependencies=[Depends(require_api_key), Depends(require_scope("ledger:read"))],
)


def _ledger() -> EvidenceLedger:
    return EvidenceLedger(Path(".brain"))


@router.get("/events", dependencies=[Depends(require_scope("ledger:read"))])
async def list_events(
    event_type: str = Query("", max_length=64),
    repository_id: str = Query("", max_length=128),
    entity_type: str = Query("", max_length=64),
    entity_id: str = Query("", max_length=256),
    actor_identity: str = Query("", max_length=256),
    since_sequence: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
):
    """Bounded, paginated query over the chain."""
    return _ledger().query(
        event_type=event_type,
        repository_id=repository_id,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_identity=actor_identity,
        since_sequence=since_sequence,
        limit=limit,
        offset=offset,
    )


@router.get("/verify", dependencies=[Depends(require_scope("ledger:read"))])
async def verify_chain():
    """Full chain verification with typed issue codes."""
    return _ledger().verify()


@router.get("/health", dependencies=[Depends(require_scope("ledger:read"))])
async def ledger_health():
    return _ledger().health()


@router.get("/events/{event_id}", dependencies=[Depends(require_scope("ledger:read"))])
async def get_event(event_id: str):
    event = _ledger().get(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"Unknown ledger event: {event_id}")
    payload = event.to_dict()
    payload["hash_valid"] = event.compute_hash() == event.event_hash
    return payload


@router.get("/entities/{entity_type}/{entity_id}", dependencies=[Depends(require_scope("ledger:read"))])
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
