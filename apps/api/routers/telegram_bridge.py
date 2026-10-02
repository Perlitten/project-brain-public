"""Compatibility surface for the existing Telegram poller.

The poller (external-brain's `app/workers/telegram_poller.py`) is a live,
working piece of software whose only problem is that the API it was pointed at
no longer serves the endpoints it calls — every message died on a 404 before it
could reach anyone. Rather than edit a running system, this reproduces the two
endpoints it expects so it can be repointed here with a single env var, and
rolled back the same way.

Its contract, read from the poller source:
  POST /v1/intake/capture   -> any 2xx
  POST /v1/assistant/reply  {workspace_slug, project_slug, question} -> {"answer": str}
  Authorization: Bearer <token>
"""

import hmac
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select

from apps.api.auth import DEV_PRINCIPAL, LEGACY_PRINCIPAL
from apps.api.schemas import AskRequest
from brain.auth.principals import resolve_credential
from brain.config.settings import settings
from brain.database.models import Repository
from brain.database.session import async_session_factory

router = APIRouter(prefix="/v1", tags=["telegram-bridge"])

class CaptureRequest(BaseModel):
    model_config = {"extra": "allow"}


class AssistantReplyRequest(BaseModel):
    question: str
    workspace_slug: Optional[str] = None
    project_slug: Optional[str] = None


async def _authorize(
    request: Request, authorization: Optional[str], x_api_key: Optional[str]
) -> None:
    """Authenticate the poller: legacy shared key still works; a scoped

    ``pbk_`` credential (Bearer or X-API-Key) must carry ``bridge:write``.
    The resolved principal lands on request.state so the audit middleware
    attributes these calls like every other authenticated mutation."""
    expected = settings.PROJECT_BRAIN_API_KEY or ""
    presented = x_api_key or ""
    if not presented and authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()

    if expected and presented and hmac.compare_digest(presented, expected):
        request.state.principal = LEGACY_PRINCIPAL
        return
    if not presented:
        if expected:
            raise HTTPException(status_code=401, detail="Unauthorized")
        if settings.ENVIRONMENT.lower() == "production":
            raise HTTPException(status_code=503, detail="API authentication is not configured")
        request.state.principal = DEV_PRINCIPAL
        return

    async with async_session_factory() as session:
        async with session.begin():
            principal = await resolve_credential(session, presented)
    if principal is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not principal.has_scope("bridge:write"):
        raise HTTPException(
            status_code=403,
            detail=f"Principal '{principal.name}' lacks required scope",
        )
    request.state.principal = principal


async def _resolve_repo(question: str, project_slug: Optional[str]) -> tuple[Optional[str], str]:
    """Pick which indexed repository a question is about.

    Returns (repo_path, question_without_prefix). A message may name its target
    with a `forex: ...` style prefix, because one bot serving nine indexed
    repositories is useless if every question hits the same one.
    """
    from pathlib import Path as _Path

    async with async_session_factory() as session:
        rows = (await session.execute(select(Repository))).scalars().all()
        repos = [(r.path, (r.name or r.path.rstrip("/").split("/")[-1]).lower()) for r in rows]

    # Repositories whose source no longer exists still answer, and their names
    # often collide with live ones ("brain:" matched a dead D:/Brain/project-brain
    # row before /app). Live sources win; a stale row is only a last resort.
    def _alive(item: tuple[str, str]) -> int:
        try:
            return 0 if _Path(item[0]).is_dir() else 1
        except Exception:
            return 1

    repos.sort(key=_alive)

    head, sep, tail = question.partition(":")
    if sep and len(head) <= 40:
        wanted = head.strip().lower()
        for path, name in repos:
            if wanted and (wanted == name or wanted in name):
                return path, tail.strip() or question

    if project_slug:
        wanted = project_slug.strip().lower()
        for path, name in repos:
            if wanted == name or wanted in name:
                return path, question

    return settings.BRAIN_TELEGRAM_DEFAULT_REPO or None, question


@router.post("/intake/capture")
async def intake_capture(
    body: CaptureRequest,
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    """Acknowledge the raw message. The Brain stores decisions, not chat logs."""
    await _authorize(request, authorization, x_api_key)
    return {"status": "accepted"}


def _is_non_latin(text: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in text)


async def _translate(text: str, target: str) -> str:
    """Best-effort translation via the fast model; returns the input on failure."""
    from brain.llm.router import TaskKind, get_model_router

    try:
        llm = get_model_router().llm(TaskKind.CLASSIFICATION)
        out = await llm.generate(
            prompt=f"Translate the following text to {target}. Output only the translation, "
            f"keeping code identifiers, file paths and technical terms verbatim:\n\n{text}",
            system_instruction="You are a precise translator for software engineering text.",
            max_tokens=600,
        )
        return (out or "").strip() or text
    except Exception as exc:
        logger.warning(f"telegram bridge: translation to {target} failed: {exc}")
        return text


@router.post("/assistant/reply")
async def assistant_reply(
    body: AssistantReplyRequest,
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    await _authorize(request, authorization, x_api_key)

    from apps.api.routers.core import ask_project

    repo_path, question = await _resolve_repo(body.question, body.project_slug)

    # Retrieval is English-and-code only: extract_keywords matches [a-zA-Z]{3,}
    # so Cyrillic yields no lexical terms at all, and the embedding model is
    # nv-embedcode, trained on code. A Russian question therefore retrieves
    # nothing and the answer becomes "the context does not contain...". Translate
    # for retrieval, answer in the language that was asked.
    # One translation, not two: the English rendering steers retrieval while the
    # question as asked still reaches the model, which answers in that language
    # by itself. The earlier translate → ask → translate chain took long enough
    # that the poller's read timeout (poll_timeout + 10s) fired and the user got
    # silence instead of an answer.
    russian = _is_non_latin(question)
    search_question = await _translate(question, "English") if russian else None

    try:
        result = await ask_project(
            AskRequest(query=question, repo_path=repo_path, retrieval_query=search_question),
            request,
        )
        answer = result.get("answer") if isinstance(result, dict) else str(result)
    except Exception as exc:  # a chat bot must answer, not 500 into silence
        logger.warning(f"telegram bridge: ask failed for repo={repo_path}: {exc}")
        return {"answer": f"Не смог ответить: {exc}"}

    scope = repo_path or "репозиторий по умолчанию"
    return {"answer": f"[{scope}]\n{answer}"}
