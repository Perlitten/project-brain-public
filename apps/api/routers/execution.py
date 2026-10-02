"""API Router for Project Brain v0.5.2 Phased Agent Execution Loop."""

import uuid
from fastapi import APIRouter, Depends
from brain.execution.models import ExecutionSession, ExecutionState, ExecutionContract
from apps.api.auth import require_api_key, require_scope

router = APIRouter(prefix="/execution", tags=["execution"])


@router.post("/sessions", dependencies=[Depends(require_scope("execution:write"))])
def create_execution_session(repo_id: str, task_category: str, authenticated: bool = Depends(require_api_key)):
    contract = ExecutionContract(
        task_intent="API Session Task",
        success_criteria=["Pass"],
        prohibited_outcomes=[],
        repository_scope=[repo_id],
    )
    session = ExecutionSession(
        execution_id=f"exec-{uuid.uuid4().hex[:8]}",
        repository_id=repo_id,
        repository_revision="head",
        task_fingerprint="fp-api",
        task_category=task_category,
        selected_brain_route="observe",
        evidence_pack_id="pack-api",
        model_provider="NVIDIA NIM API",
        exact_model_identifier="meta/llama-3.1-70b-instruct",
        contract=contract,
    )
    return {"execution_id": session.execution_id, "state": session.state.value}


@router.get("/sessions/{execution_id}", dependencies=[Depends(require_scope("execution:read"))])
def get_execution_session(execution_id: str, authenticated: bool = Depends(require_api_key)):
    return {"execution_id": execution_id, "state": ExecutionState.RECEIVED.value}
