import asyncio
from contextlib import asynccontextmanager
from contextlib import suppress

from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from apps.api.audit_middleware import AuditLogMiddleware
from apps.api.request_id_middleware import RequestIdMiddleware
from apps.api.request_size_middleware import RequestSizeLimitMiddleware
from apps.api.routers import telegram_bridge, core, harness, jobs, scheduler, setup, drift, graph_v2, coupling, impact, remediation, workspace, portfolio, freshness, lab, experiments, ledger, control, execution, routing, shadow, operations, improvement, autonomy, audit, admin, web, quality, app_settings
from brain.config.paths import allowed_repo_roots
from brain.config.settings import settings
from brain.database.session import close_database_connections, init_db
from brain.workers.queue import QueueDepthExceeded
from brain.late_interaction.client import (
    LateInteractionReleaseUnavailableError,
    close_late_interaction_client,
    validate_remote_late_interaction_release,
)
from brain.late_interaction.provider import (
    close_lfm_colbert_provider,
    warmup_lfm_colbert_provider,
)
from apps.api.logging_setup import configure_file_log_sink
from brain.version import build_info

configure_file_log_sink()


@asynccontextmanager
async def lifespan(app: FastAPI):
    late_warmup_task: asyncio.Task | None = None
    # Sandbox misconfiguration is only visible at request time otherwise.
    logger.warning(
        "Repo path sandbox roots: {}",
        ", ".join(str(base) for base in allowed_repo_roots()),
    )
    try:
        await init_db()
    except Exception as exc:
        # Keep serving health and diagnostics; readiness remains red until
        # schema initialization succeeds on a later worker task or restart.
        logger.error(f"Startup schema initialization deferred: {type(exc).__name__}: {exc}")
    if settings.LATE_INTERACTION_ENABLED and (
        settings.LATE_INTERACTION_SHADOW_ENABLED
        or settings.LATE_INTERACTION_RERANK_ENABLED
    ) and not settings.LATE_INTERACTION_REMOTE_ENABLED:
        async def _warm_late_interaction() -> None:
            try:
                await asyncio.wait_for(warmup_lfm_colbert_provider(), timeout=60.0)
                logger.info("Late-interaction sidecar warmup completed")
            except Exception as exc:
                logger.warning(
                    "Late-interaction sidecar warmup failed open: {}",
                    str(exc).strip() or type(exc).__name__,
                )

        late_warmup_task = asyncio.create_task(_warm_late_interaction())
    if settings.LATE_INTERACTION_REMOTE_ENABLED:
        try:
            validated_index = await validate_remote_late_interaction_release()
        except LateInteractionReleaseUnavailableError as exc:
            # The precision layer is fail-open at query time. A transient
            # sidecar outage must not take the core Brain API down with it;
            # deterministic identity mismatches still raise and fail startup.
            validated_index = None
            logger.warning(
                "Late-interaction sidecar unavailable at startup; "
                "continuing with baseline retrieval: {}",
                str(exc),
            )
        if validated_index is not None:
            logger.info(
                "Active late-interaction release validated: repo={} index={}",
                validated_index.repository_id,
                validated_index.index_revision,
            )
    yield
    if late_warmup_task is not None and not late_warmup_task.done():
        late_warmup_task.cancel()
        with suppress(asyncio.CancelledError):
            await late_warmup_task
    try:
        await close_late_interaction_client()
    finally:
        try:
            await close_lfm_colbert_provider()
        finally:
            await close_database_connections()


app = FastAPI(
    title="Project Brain API",
    description="Backend API services for Project Brain",
    version=build_info()["version"],
    lifespan=lifespan,
)


# --- Security headers middleware ---
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if settings.ENVIRONMENT.lower() == "production":
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response


app.add_middleware(SecurityHeadersMiddleware)

# --- CORS ---
_cors_origins = [origin.strip() for origin in (getattr(settings, "CORS_ALLOWED_ORIGINS", "") or "").split(",") if origin.strip()]
if not _cors_origins and settings.ENVIRONMENT.lower() != "production":
    _cors_origins = ["*"]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["X-API-Key", "X-CSRF-Token", "Authorization", "Content-Type"],
    )

app.add_middleware(AuditLogMiddleware)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(RequestSizeLimitMiddleware)


@app.exception_handler(QueueDepthExceeded)
async def queue_depth_exceeded_handler(request: FastAPIRequest, exc: QueueDepthExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "detail": "job queue depth budget exceeded",
            "depth": exc.depth,
            "limit": exc.limit,
        },
        headers={"Retry-After": "30"},
    )

def _web_url() -> str | None:
    value = getattr(settings, "BRAIN_WEB_URL", None)
    return value.strip() if isinstance(value, str) and value.strip() else None


@app.get("/", include_in_schema=False)
async def service_root() -> dict[str, str | None]:
    """Service identity. The human UI is the separately deployed web app."""
    return {"service": "project-brain", "ui": _web_url()}


@app.get("/dashboard", include_in_schema=False)
@app.get("/dashboard/{path:path}", include_in_schema=False)
async def retired_dashboard(path: str = "") -> Response:
    """The server-rendered dashboard was removed; send old bookmarks to the web UI."""
    web_url = _web_url()
    if web_url is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "The built-in dashboard was removed; no web UI (BRAIN_WEB_URL) is configured."},
        )
    return RedirectResponse(url=web_url, status_code=301)


app.include_router(core.router)
app.include_router(setup.router)
app.include_router(app_settings.router)
app.include_router(jobs.router)
app.include_router(scheduler.router)
# JSON read API for the web UI (apps/web).
app.include_router(web.router)
app.include_router(quality.router)
app.include_router(harness.router)
app.include_router(telegram_bridge.router)
app.include_router(drift.router)
app.include_router(graph_v2.router)
app.include_router(coupling.router)
app.include_router(impact.router)
app.include_router(remediation.router)
# v0.5.0 multi-repository intelligence: registry, portfolio graph, freshness.
app.include_router(workspace.router)
app.include_router(portfolio.router)
app.include_router(freshness.router)
# v0.5.0 change laboratory: disposable workspaces for candidate patch validation.
app.include_router(lab.router)
# v0.5.0 remediation experiments: compare candidate patches, recommend, export.
app.include_router(experiments.router)
# v0.5.0 evidence ledger: read-only. Writes happen through product actions.
app.include_router(ledger.router)
# v0.5.0 control plane: operator summary, human action queue, budgets, metrics.
app.include_router(control.router)
# v0.5.2 phased execution loop: typed session management, contracts, journals.
app.include_router(execution.router)
# v0.5.3 selective policy routing: category-aware execution, observe/shadow/selective modes.
app.include_router(routing.router)
# v0.5.3 shadow execution: non-authoritative candidate evaluation in isolated workspaces.
app.include_router(shadow.router)
# v0.5.3 night operations: correlated incidents, freshness repair, deduplicated alerts.
app.include_router(operations.router)
# v0.7.0 continuous improvement factory: trajectory vault, bundles, replay, promotion gates, rollbacks.
app.include_router(improvement.router)
app.include_router(autonomy.router)
# v0.7.0 enterprise admin: principal + credential lifecycle over the API.
app.include_router(admin.router)
# v0.7.0 enterprise admin: audit log read/export for operator + SIEM review.
app.include_router(audit.router)
