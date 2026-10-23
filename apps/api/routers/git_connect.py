"""Connect a repository from a git URL.

``POST /git/probe`` checks that a remote is reachable and lists its branches;
``POST /git/connect`` registers the repository (``brain/workspace``, and the
team project in team state), then queues the ``git_connect`` job that clones
it into ``BRAIN_MANAGED_REPOS_DIR`` and indexes it. From then on it is an
ordinary attached repository: the origin poller follows the tracked branch
and ``POST /jobs/origin-poll`` syncs it on demand.

A token typed in the UI is used for the probe and handed to the worker
through a one-time file under the managed root; it never reaches the job
params, the database, logs or a response (see :mod:`brain.indexers.git_connect`).
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException

from apps.api import activity_log, team_access
from apps.api.auth import require_api_key, require_principal, require_scope
from apps.api.registry_access import register_for_team, workspace_registry
from apps.api.routers.jobs import _authorize_job, _enqueue_response, _initiator, _queue
from apps.api.routers.workspace import _get_store, _require_registration_write
from apps.api.schemas import GitConnectRequest, GitProbeRequest
from apps.api.team_access import team_administrator_access
from brain.auth.principals import ResolvedPrincipal
from brain.indexers.git_connect import (
    GitUrlError,
    RemoteInfo,
    clone_path,
    ensure_branch,
    managed_root,
    parse_remote_url,
    probe_remote,
    read_handoff,
    transport_url,
    with_token,
    write_handoff,
)
from brain.indexers.source_mirror import redact_url
from brain.lab.laboratory import BUILTIN_PROFILES
from brain.team.authorization import Permission, TeamDenied, authorize
from brain.workspace.capabilities import CapabilitiesManager
from brain.workspace.models import RepositoryRecord, TrustLevel, _generate_repository_id
from brain.workspace.path_sandbox import PathSandbox

router = APIRouter(
    prefix="/git",
    tags=["git"],
    # Connecting a repository is repository registration: the same gate as
    # POST /workspace/repositories (team administrator; workspace:write in solo state).
    dependencies=[
        Depends(require_api_key),
        Depends(require_scope("workspace:read")),
        Depends(team_administrator_access()),
        Depends(_require_registration_write),
    ],
)

_NO_MANAGED_ROOT = (
    "Connecting by URL is off: set the clones directory (Settings → Git & origin sync → Clones directory) "
    "to a writable folder on the server first"
)


def _parse(url: str) -> RemoteInfo:
    try:
        return parse_remote_url(url)
    except GitUrlError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid git URL: {exc}")


def _with_token(info: RemoteInfo, token: Optional[str]) -> str:
    try:
        return with_token(info, token)
    except GitUrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _root_status() -> Dict[str, Any]:
    root = managed_root()
    return {
        "managed_root_configured": root is not None,
        "managed_root_writable": bool(root is not None and root.is_dir() and os.access(str(root), os.W_OK)),
    }


def _safe_error(error: Optional[str], token: Optional[str]) -> str:
    """Error text that may leave the API: no URL credentials and never the caller's token."""
    text = redact_url(error or "Remote is not reachable")
    if token:
        text = text.replace(token, "***")
    return text


@router.post("/probe")
async def probe(body: GitProbeRequest) -> Dict[str, Any]:
    """Is the remote reachable with these credentials, and which branches does it have?"""
    info = _parse(body.url)
    url = _with_token(info, body.token)
    result = await asyncio.to_thread(probe_remote, url)
    del url
    payload = {
        "ok": result.reachable,
        **info.to_dict(),
        **result.to_dict(),
        **_root_status(),
    }
    if payload.get("error"):
        payload["error"] = _safe_error(payload["error"], body.token)
    return payload


def _project_id(raw: Optional[str]) -> Optional[uuid.UUID]:
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        raise HTTPException(status_code=422, detail="project_id must be a UUID")


async def _authorize_clone(principal: ResolvedPrincipal, project_id: Optional[uuid.UUID], path: str) -> None:
    # Cloning is a queued run even when initial indexing is disabled. Check the
    # selected project before registration, so a refusal leaves no empty record.
    if project_id is not None and await team_access.current_team_state() != "solo":
        try:
            async with team_access.async_session_factory() as session:
                await authorize(session, principal, project_id, Permission.REQUEST_RUN)
        except TeamDenied as exc:
            raise HTTPException(status_code=403, detail=f"Cloning requires run.request: {exc}") from exc
    else:
        await _authorize_job(principal, path)


@router.post("/connect", dependencies=[Depends(require_scope("jobs:write"))])
async def connect(body: GitConnectRequest, principal: ResolvedPrincipal = Depends(require_principal)) -> Dict[str, Any]:
    """Register a repository by URL and queue its clone and first index."""
    root = managed_root()
    if root is None:
        raise HTTPException(status_code=409, detail=_NO_MANAGED_ROOT)
    if not (root.is_dir() and os.access(str(root), os.W_OK)):
        raise HTTPException(status_code=409, detail=(
            f"The clones directory {root} does not exist or is not writable by the server"))
    info = _parse(body.url)
    clone_url = _with_token(info, body.token)
    project_id = _project_id(body.project_id)

    branch = (body.branch or "").strip()
    probed_head: Optional[str] = None
    if not branch:
        # The caller skipped the probe: find the default branch ourselves (with the token, if any).
        result = await asyncio.to_thread(probe_remote, clone_url)
        if not result.reachable:
            status = 401 if result.needs_auth else 400
            raise HTTPException(status_code=status, detail=_safe_error(result.error, body.token))
        branch = result.default_branch or "main"
        probed_head = result.head
    try:
        branch = ensure_branch(branch)
    except GitUrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    dest = clone_path(info, branch)
    assert dest is not None  # managed_root() is set
    canonical = os.path.realpath(str(dest))
    normalized = PathSandbox.get_normalized_identity(Path(canonical))
    if len(canonical) > 1024 or len(normalized) > 1024:
        raise HTTPException(status_code=422, detail="Clone path exceeds the 1024-character storage limit")
    await _authorize_clone(principal, project_id, canonical)
    display_name = (body.display_name or "").strip() or info.name

    existing = [
        r for r in (await workspace_registry()).list_all()
        if not r.disabled and PathSandbox.get_canonical_string(Path(r.canonical_root)) == normalized
    ]
    registered = existing[0] if existing else None
    if registered is not None and (dest / ".git").exists():
        raise HTTPException(status_code=409, detail=(
            f"{info.display_url} ({branch}) is already connected as '{registered.display_name}'. "
            "Use Sync to fetch its latest commits."))

    if registered is None:
        trust = TrustLevel.TRUSTED_INTERNAL
        record = RepositoryRecord(
            repository_id=_generate_repository_id(canonical),
            display_name=display_name,
            canonical_root=canonical,
            normalized_root_identity=normalized,
            repository_type="git",
            default_branch=branch,
            current_revision=probed_head or "pending",
            trust_level=trust.value,
            organization="",
            owner="",
            languages=[],
            capabilities=CapabilitiesManager.get_defaults(trust),
            allowed_validation_profiles=sorted(BUILTIN_PROFILES),
        )
        try:
            registered, project_id = await register_for_team(
                principal, canonical, project_id, record,
                lambda: _get_store().register(record, actor="api", source="api"),
                create_index_record=True)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    params: Dict[str, Any] = {
        "url": transport_url(info),
        "branch": branch,
        "dest": canonical,
        "repo_path": canonical,
        "display_name": display_name,
        "index_now": bool(body.index_now),
        "provider": info.provider,
    }
    handoff: Optional[str] = None
    if clone_url != params["url"]:
        try:
            handoff = write_handoff(clone_url)
        except OSError as exc:
            raise HTTPException(status_code=503, detail=f"Could not stage the token for the worker: {exc}")
        params["handoff"] = handoff
    del clone_url
    try:
        job_id = await _queue("git_connect").enqueue(
            "git_connect", params, initiator=_initiator(principal), max_attempts=2, timeout_seconds=1800,
        )
    except Exception as exc:
        if handoff:
            read_handoff(handoff)  # consumes and deletes the staged token
        raise HTTPException(status_code=503, detail=f"Job queue unavailable: {type(exc).__name__}")
    await activity_log.record_job_queued(principal, "git_connect", job_id, repo_path=canonical)
    response = {
        **_enqueue_response(job_id),
        "repository": registered.to_dict(),
        "path": canonical,
        "name": display_name,
        "provider": info.provider,
        "display_url": info.display_url,
        "branch": branch,
    }
    if project_id is not None:
        response["project_id"] = str(project_id)
    return response
