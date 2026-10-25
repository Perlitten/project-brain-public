"""Execution artifacts: caps, redaction, storage and reads."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from brain.auth.principals import ResolvedPrincipal
from brain.config.settings import settings
from brain.execution.bounded import REDACTED, ExecutorBackend, ExecutorError, ToolSpec, read_confined
from brain.team._util import _iso
from brain.team.authorization import Permission, authorize
from brain.team.models import AgentRunExecution, AgentRunExecutionArtifact
from brain.team.execution._common import ExecutionNotFound, _run_and_row


ARTIFACT_KINDS = ("log", "test-report", "patch")


# Extra bytes read beyond a cap so a secret cut by the read boundary is still redacted before the final cut.
_REDACTION_MARGIN = 4096


_ARTIFACT_COLUMNS = (AgentRunExecutionArtifact.id, AgentRunExecutionArtifact.name, AgentRunExecutionArtifact.kind,
                     AgentRunExecutionArtifact.step, AgentRunExecutionArtifact.content_type,
                     AgentRunExecutionArtifact.size_bytes, AgentRunExecutionArtifact.original_bytes,
                     AgentRunExecutionArtifact.truncated, AgentRunExecutionArtifact.sha256,
                     AgentRunExecutionArtifact.created_at)


@dataclass(frozen=True)
class ArtifactDraft:
    """An artifact prepared outside the transaction; capped and stored by ``_store_artifacts``."""
    name: str
    kind: str
    step: int | None
    content_type: str
    content: bytes
    original_bytes: int
    truncated: bool
    keep_tail: bool = False


def artifact_caps() -> tuple[int, int]:
    """(per-artifact cap, per-run cap) in bytes."""
    each = max(1024, int(settings.BRAIN_EXECUTION_ARTIFACT_MAX_BYTES))
    return each, max(each, int(settings.BRAIN_EXECUTION_ARTIFACTS_MAX_BYTES))


def redact_bytes(data: bytes, secrets: set[str] | frozenset[str]) -> bytes:
    for value in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        data = data.replace(value.encode("utf-8"), REDACTED.encode())
    return data


def _artifact_meta(row: Any) -> dict:
    return {"id": str(row.id), "name": row.name, "kind": row.kind, "step": row.step,
            "content_type": row.content_type, "bytes": row.size_bytes, "original_bytes": row.original_bytes,
            "truncated": row.truncated, "sha256": row.sha256, "created_at": _iso(row.created_at)}


async def _artifact_list(session: AsyncSession, run_id: uuid.UUID) -> list[dict]:
    rows = (await session.execute(select(*_ARTIFACT_COLUMNS).where(AgentRunExecutionArtifact.run_id == run_id)
                                  .order_by(AgentRunExecutionArtifact.created_at,
                                            AgentRunExecutionArtifact.name))).all()
    return [_artifact_meta(row) for row in rows]


async def _store_artifacts(session: AsyncSession, row: AgentRunExecution, token: int, drafts: list[ArtifactDraft],
                           secrets: set[str]) -> None:
    """Redact, cap (per artifact and per run) and add the drafts in the caller's fenced transaction."""
    each, total = artifact_caps()
    used = int((await session.execute(select(func.coalesce(func.sum(AgentRunExecutionArtifact.size_bytes), 0))
                                      .where(AgentRunExecutionArtifact.run_id == row.run_id))).scalar_one())
    for draft in drafts:
        room = min(each, total - used)
        if room <= 0:
            logger.warning("Execution {} reached its artifact cap; {} not stored", row.run_id, draft.name)
            break
        content, truncated = redact_bytes(draft.content, secrets), draft.truncated
        if len(content) > room:
            content = content[-room:] if draft.keep_tail else content[:room]
            truncated = True
        session.add(AgentRunExecutionArtifact(
            run_id=row.run_id, name=draft.name[:128], kind=draft.kind, step=draft.step,
            content_type=draft.content_type, size_bytes=len(content), original_bytes=draft.original_bytes,
            truncated=truncated, sha256=hashlib.sha256(content).hexdigest(), content=content,
            fencing_token=token))
        used += len(content)


def _report_type(path: str) -> tuple[str, str]:
    name = path.rsplit("/", 1)[-1]
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    content_type = {"xml": "application/xml", "json": "application/json"}.get(suffix, "text/plain; charset=utf-8")
    return (suffix if re.fullmatch(r"[a-z0-9]{1,8}", suffix) else "txt"), content_type


def step_artifacts(copy: Any, tool: ToolSpec, index: int, log_path: str | None) -> list[ArtifactDraft]:
    """The step's log tail (already redacted by the backend) and the tool's report file, if any."""
    each, _ = artifact_caps()
    drafts: list[ArtifactDraft] = []
    if log_path:
        try:
            with open(log_path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - each))
                content = handle.read()
            drafts.append(ArtifactDraft(f"step-{index:03d}.log", "log", index, "text/plain; charset=utf-8", content,
                                        size, size > len(content), keep_tail=True))
        except OSError as exc:
            logger.warning("Could not read the log of step {}: {}", index, exc)
    if tool.report:
        read = read_confined(Path(copy.path), tool.report, each + _REDACTION_MARGIN)
        if read is not None and read[2] != "symlink":
            content, size, _kind = read
            suffix, content_type = _report_type(tool.report)
            drafts.append(ArtifactDraft(f"step-{index:03d}-report.{suffix}", "test-report", index, content_type,
                                        content, size, size > len(content)))
    return drafts


async def _patch_artifact(backend: ExecutorBackend, copy: Any) -> ArtifactDraft | None:
    """The final patch against the base; ``None`` (advisory, like changed paths) when it cannot be built."""
    builder = getattr(backend, "patch", None)
    if builder is None:
        return None
    each, _ = artifact_caps()
    try:
        content, truncated = await asyncio.to_thread(builder, copy, each + _REDACTION_MARGIN)
    except (ExecutorError, OSError) as exc:
        logger.warning("Could not build the patch for run {}: {}", copy.run_dir.name, exc)
        return None
    if not content:
        return None
    return ArtifactDraft("final.patch", "patch", None, "text/x-diff; charset=utf-8", content,
                         len(content), truncated)


async def list_artifacts(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                         task_id: uuid.UUID, run_id: uuid.UUID) -> list[dict]:
    await authorize(session, caller, project_id, Permission.READ, task_id=task_id)
    run, _ = await _run_and_row(session, task_id, run_id)
    return await _artifact_list(session, run.id)


async def get_artifact(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                       task_id: uuid.UUID, run_id: uuid.UUID, artifact_id: uuid.UUID) -> tuple[dict, bytes]:
    """Metadata and content of one artifact, for project members with read access to the task."""
    await authorize(session, caller, project_id, Permission.READ, task_id=task_id)
    run, _ = await _run_and_row(session, task_id, run_id)
    row = (await session.execute(select(AgentRunExecutionArtifact).where(
        AgentRunExecutionArtifact.id == artifact_id, AgentRunExecutionArtifact.run_id == run.id))).scalar_one_or_none()
    if row is None:
        raise ExecutionNotFound("Artifact not found")
    return _artifact_meta(row), bytes(row.content)
