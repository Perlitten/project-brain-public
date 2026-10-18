"""``GET /team/doctor``: the read-only team diagnostics of ``brain team doctor`` as JSON.

Administrators only, through the central ``team_administrator_access``: in solo
state any authenticated key holding ``operations:read`` (a team of one), in team
state a live team administrator. While a restore fence is active the API refuses
named credentials, so ``brain team doctor`` on the server is the break-glass path.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from apps.api.auth import require_scope
from apps.api.team_access import team_administrator_access
from brain.database.session import async_session_factory
from brain.team import doctor

router = APIRouter(prefix="/team", tags=["experimental-team"])


@router.get("/doctor", dependencies=[Depends(require_scope("operations:read")), Depends(team_administrator_access())])
async def team_doctor() -> dict[str, Any]:
    async with async_session_factory() as session:
        return doctor.summary(await doctor.run_checks(session))
