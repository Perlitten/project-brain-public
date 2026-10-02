"""API Router for Shadow Execution in Project Brain v0.5.3."""

from fastapi import APIRouter, Depends, HTTPException
from brain.shadow.runner import ShadowRunner
from apps.api.auth import require_api_key, require_scope

router = APIRouter(prefix="/shadow", tags=["shadow"])
runner = ShadowRunner()


@router.get("/executions", dependencies=[Depends(require_scope("shadow:read"))])
def list_shadow_executions(authenticated: bool = Depends(require_api_key)):
    return {"sessions": [s.model_dump() for s in runner.list_sessions()]}


@router.get("/executions/{shadow_id}", dependencies=[Depends(require_scope("shadow:read"))])
def get_shadow_execution(shadow_id: str, authenticated: bool = Depends(require_api_key)):
    sess = runner._sessions.get(shadow_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Shadow session not found")
    return sess.model_dump()


@router.post("/executions/{shadow_id}/cancel", dependencies=[Depends(require_scope("shadow:write"))])
def cancel_shadow_execution(shadow_id: str, authenticated: bool = Depends(require_api_key)):
    success = runner.cancel_session(shadow_id)
    if not success:
        raise HTTPException(status_code=404, detail="Shadow session not found")
    return {"shadow_id": shadow_id, "status": "cancelled"}


@router.get("/summary", dependencies=[Depends(require_scope("shadow:read"))])
def get_shadow_summary(authenticated: bool = Depends(require_api_key)):
    return runner.get_summary()
