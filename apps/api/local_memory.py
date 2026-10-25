"""Memory writes from the local surfaces (stdio MCP server, ``brain`` CLI).

The local surfaces run in the operator's own process with the operator's
database credentials, so by default they act as the operator: the local
administrator of this Brain, writing directly and unattributed, exactly like a
team administrator over HTTP.

``BRAIN_LOCAL_PRINCIPAL_KEY`` names a credential instead. The local surfaces
then act as that principal and run the very same HTTP handlers
(``core_memory.create_decision`` / ``create_rule`` / ``create_learning``), so team-state governance
(proposal for non-administrators, direct write for administrators and solo)
has a single implementation. A configured key that does not resolve fails
closed: the write is refused, never silently performed as the operator.
"""

from __future__ import annotations

import json
from typing import Any, Literal, Optional

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from apps.api.auth import named_credential_required, resolve_api_key
from apps.api.schemas import DecisionCreate, LearningCreate, RuleCreate
from brain.auth.principals import ResolvedPrincipal
from brain.config.settings import settings


class LocalPrincipalError(PermissionError):
    """The configured local credential cannot act: refuse rather than fall back to the operator."""


async def local_principal() -> Optional[ResolvedPrincipal]:
    """The principal the local surfaces act as, or None for the operator (no key configured)."""
    raw = (settings.BRAIN_LOCAL_PRINCIPAL_KEY or "").strip()
    if not raw:
        return None
    principal = await resolve_api_key(raw)
    if principal is None:
        raise LocalPrincipalError(
            "BRAIN_LOCAL_PRINCIPAL_KEY does not resolve to a live credential; refusing the write")
    if await named_credential_required(principal):
        raise LocalPrincipalError(
            "BRAIN_LOCAL_PRINCIPAL_KEY must be a named credential: this Brain has team members")
    return principal


async def require_local_administrator(action: str) -> None:
    """Operator-only bulk commands: the operator, or a configured key that is a team administrator (or solo)."""
    from apps.api.routers import memory_review

    principal = await local_principal()
    if principal is not None and await memory_review.governed_write(principal):
        raise LocalPrincipalError(
            f"{action} writes shared memory directly and needs a team administrator; "
            f"BRAIN_LOCAL_PRINCIPAL_KEY belongs to '{principal.name}'")


def _failure(exc: HTTPException) -> dict[str, Any]:
    detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail)
    if exc.status_code == 409:
        return {"status": "conflict", "error": detail}
    if exc.status_code in (401, 403):
        return {"status": "denied", "error": detail}
    return {"status": "failed", "error": detail}


async def governed_write(
    kind: Literal["decision", "rule", "learning"],
    body: DecisionCreate | RuleCreate | LearningCreate,
    principal: ResolvedPrincipal,
) -> dict[str, Any]:
    """Run the HTTP write handler as ``principal`` and return its answer as a plain dict.

    ``status`` is ``success`` (written), ``proposed`` (pending review), ``conflict``,
    ``denied`` or ``failed``.
    """
    from apps.api.routers import core_memory

    if not principal.has_scope("core:write"):
        return {"status": "denied", "error": f"Principal '{principal.name}' lacks required scope"}
    try:
        if kind == "decision":
            assert isinstance(body, DecisionCreate)
            result: Any = await core_memory.create_decision(body, caller=principal)
        elif kind == "rule":
            assert isinstance(body, RuleCreate)
            result = await core_memory.create_rule(body, caller=principal)
        else:
            assert isinstance(body, LearningCreate)
            result = await core_memory.create_learning(body, principal=principal)
    except HTTPException as exc:
        return _failure(exc)
    if isinstance(result, JSONResponse):
        return json.loads(bytes(result.body))
    return dict(result)


async def local_write(
    kind: Literal["decision", "rule", "learning"], fields: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """A governed memory write from a local surface as the configured principal.

    None: no key is configured, so the caller writes as the operator. Otherwise the
    handler's answer (see ``governed_write``); an unusable key answers ``denied``.
    """
    try:
        principal = await local_principal()
    except LocalPrincipalError as exc:
        return {"status": "denied", "error": str(exc)}
    if principal is None:
        return None
    body: DecisionCreate | RuleCreate | LearningCreate
    if kind == "decision":
        body = DecisionCreate(**fields)
    elif kind == "rule":
        body = RuleCreate(**fields)
    else:
        body = LearningCreate(**fields)
    return await governed_write(kind, body, principal)
