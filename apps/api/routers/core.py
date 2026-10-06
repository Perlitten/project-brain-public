import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from loguru import logger

from apps.api.auth import require_api_key, require_scope
from apps.api.helpers import (
    format_datetime_utc,
    get_cache_status,
    _short_commit_ref,
    get_db_counts,
    get_embedding_retrieval_health,
    get_latest_critic_status,
    get_latest_eval_metrics,
    get_mcp_runtime_status,
    get_mcp_readiness_status,
    get_neo4j_counts,
    get_reports_list,
    get_self_diagnosis_status,
    get_top_connected_nodes,
)
from apps.api.schemas import (
    EpisodeDecisionRequest,
    EpisodeSearchRequest,
    SkillCreate,
    SkillMatchRequest,
    AskRequest,
    ContextRequest,
    DecisionCreate,
    DiffReviewRequest,
    FeatureCreate,
    ImpactRequest,
    IndexRequest,
    LearningCreate,
    RelatedRequest,
    RuleCreate,
    SearchRequest,
)
from brain import __version__
from brain.analyzers.impact_analyzer import ImpactAnalyzer
from brain.config.paths import get_repo_root, resolve_repo_path
from brain.config.settings import settings
from brain.context.context_pack_builder import ContextPackBuilder
from brain.context.budget import BudgetedPayloadBuilder, truncate_utf8
from brain.context.runtime_context_builder import RuntimeContextBuilder
from brain.database.models import BrainInsight, Repository
from brain.database.repository_utils import require_repository_by_path
from brain.database.session import async_session_factory, check_health, redis_client
from brain.graph.graph_client import GraphClient
from brain.graph.schema import NodeType
from brain.indexers.file_indexer import FileIndexer
from brain.indexers.repo_indexer import get_git_commit_hash
from brain.llm.presets import llm_provider_ready
from brain.llm.router import TaskKind, get_model_router
from brain.late_interaction.client import (
    get_late_interaction_client,
    late_interaction_release_active,
)
from brain.late_interaction.operator_status import (
    collect_late_interaction_operator_status,
)
from brain.late_interaction.provider import LfmColbertProvider
from brain.memory.decision_store import DecisionStore
from brain.memory.learning_store import LearningStore
from brain.memory.repo_freshness import assess_repository_freshness
from brain.memory.repo_scope import normalize_repo_scope
from brain.memory.rule_store import RuleStore
from brain.search.code_search import search_code as hybrid_search_code
from brain.retrieval.service import RetrievalService
from brain.workers.queue import worker_pool_status
from brain.version import build_info

ASK_RETRIEVAL_LIMIT = 10
# Recorded decisions are the highest-signal memory the Brain holds, but the
# list grows forever — cap it so it informs the answer instead of crowding the
# retrieved code out of the prompt.
ASK_DECISION_LIMIT = 12
ASK_DECISION_FIELD_CHARS = 280
# Raised 2026-10-05 from 700: answers were truncated mid-sentence. The latency
# concern (MCP proxy timeout) is real, but 700 tokens is too low for a useful
# answer once the prompt context is large. Thinking blocks are stripped
# separately (see _strip_thinking_blocks) so these tokens go to the answer.
ASK_MAX_ANSWER_TOKENS = 2500
EPISODES_MAX_LIMIT = 200


def _strip_thinking_blocks(text: str) -> str:
    """Remove LLM reasoning/thinking blocks from a response.

    Reasoning models (e.g. Nemotron) emit their chain-of-thought inline in
    several formats: <think>...</think> tags, prose like "Here's a thinking
    process: ...", or a numbered analysis ("1. **Analyze User Query**: ...").
    Users should never see this — it leaks internal reasoning and wastes
    the token budget meant for the answer.
    """
    import re

    if not text:
        return text
    # Primary: the /ask prompt requires the model to end with 'FINAL ANSWER:'
    # followed by the answer. Extract only that part.
    m = re.search(r"FINAL ANSWER:\s*(.*)", text, flags=re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # <think>...</think>, <reasoning>...</reasoning>, <thought>...</thought> (any case).
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<thought>.*?</thought>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Prose thinking preamble: "Here's a thinking process:" etc.
    text = re.sub(
        r"^(?:here'?s|here is) (?:a |my )?thinking process:.*?(?=\n\n|\Z)",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Numbered chain-of-thought: "1. **Analyze User Query:** ..." — a leading
    # sequence of numbered bold-headed steps is reasoning, not the answer.
    # Only strip if the numbered block is at the very start. Steps may be
    # separated by blank lines. Handles both "**: ..." and ":**" formats.
    text = re.sub(
        r"\A(\d+\.\s+\*\*[^*\n]+?\*\*:?.*?(?:\n\n|\n(?!\d+\.\s)|\Z))+",
        "",
        text,
        flags=re.DOTALL,
    )
    return text.strip()


def _late_interaction_request_id(request: Request) -> str | None:
    """Return a bounded caller identity; raw values are never persisted."""
    value = request.headers.get("x-request-id") or request.headers.get("x-correlation-id")
    return value[:256] if value else None


def _decision_line(decision) -> str:
    def trim(value: str | None) -> str:
        text = " ".join((value or "").split())
        return text[:ASK_DECISION_FIELD_CHARS] + ("…" if len(text) > ASK_DECISION_FIELD_CHARS else "")

    parts = [f"- {decision.id}: {decision.title}"]
    for label, value in (("why", decision.reason), ("consequences", decision.consequences)):
        trimmed = trim(value)
        if trimmed:
            parts.append(f"  {label}: {trimmed}")
    return "\n".join(parts)


def _agent_v2_mode() -> str:
    return settings.BRAIN_AGENT_CONTEXT_V2_MODE


def _agent_repo_path(repo_path: str | None) -> str:
    return repo_path or str(get_repo_root())


def _v2_repo_projection(repository: dict) -> dict:
    freshness = repository.get("freshness") or {}
    return {
        "path": repository.get("repository_path") or repository.get("requested_path"),
        "indexed_revision": repository.get("indexed_revision") or repository.get("last_indexed_commit"),
        "source_revision": freshness.get("source_head_commit"),
        "freshness": freshness.get("status") or "unknown",
    }


def _record_shadow_metric(surface: str, payload: dict) -> None:
    """Shadow telemetry is hashes and compact counts only — never raw source."""
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    logger.info(
        "agent_context_v2_shadow surface={} bytes={} sha256={}",
        surface,
        len(encoded),
        hashlib.sha256(encoded).hexdigest()[:16],
    )


async def _compact_locator(body: SearchRequest) -> dict:
    cap = min(settings.AGENT_LOCATOR_MAX_BYTES, body.max_tokens * 4)
    try:
        result = await RetrievalService().retrieve(
            body.query,
            _agent_repo_path(body.repo_path),
            intent="locator",
            candidate_budget=body.limit,
            deadline_s=settings.AGENT_RETRIEVAL_DEADLINE_S,
        )
        builder = BudgetedPayloadBuilder(
            cap,
            metadata={"repo": _v2_repo_projection(result.repository), "degraded": result.degraded},
        )
        builder.add("results", [candidate.to_locator_dict() for candidate in result.candidates], priority=1)
        if body.include_debug:
            builder.add("debug", {"timings_ms": result.timings_ms}, priority=-1)
        return builder.build()
    except Exception as exc:
        logger.warning("v2 locator degraded: {}", type(exc).__name__)
        return BudgetedPayloadBuilder(
            cap,
            metadata={
                "repo": {"path": body.repo_path, "freshness": "unknown"},
                "degraded": [f"locator_error:{type(exc).__name__}"],
            },
        ).build()


async def _ask_v2(body: AskRequest) -> dict:
    started = time.perf_counter()
    try:
        runtime = await RuntimeContextBuilder().build(
            body.retrieval_query or body.query,
            _agent_repo_path(body.repo_path),
            max_tokens=3500,
            include_debug=body.include_debug,
        )
    except Exception as exc:
        logger.warning("/ask v2 retrieval degraded: {}", type(exc).__name__)
        runtime = {"status": "partial", "missing": [f"context_error:{type(exc).__name__}"]}
    if runtime.get("status") != "ok":
        return (
            BudgetedPayloadBuilder(
                settings.AGENT_ASK_OUTPUT_MAX_BYTES,
                metadata={"status": "partial", "degraded": runtime.get("missing", ["insufficient_context"])},
            )
            .add("answer", "Current indexed context is not fresh enough to answer safely.", priority=1)
            .build()
        )

    context_json = json.dumps(runtime, ensure_ascii=False, separators=(",", ":"))
    prompt_context = truncate_utf8(context_json, settings.AGENT_ASK_INPUT_MAX_BYTES)
    prompt = (
        "You are the technical assistant for Project Brain. Answer in the same language as the question. "
        "Use only the supplied evidence; cite paths/ranges, and state when evidence is insufficient. "
        "Keep the answer concise but complete — aim for a focused response, not an exhaustive dump.\n\n"
        f"Question:\n{body.query}\n\nEvidence:\n{prompt_context}"
    )
    remaining = settings.AGENT_ASK_DEADLINE_S - (time.perf_counter() - started)
    if remaining <= 0:
        return (
            BudgetedPayloadBuilder(
                settings.AGENT_ASK_OUTPUT_MAX_BYTES,
                metadata={"status": "partial", "degraded": ["ask_deadline_exceeded"]},
            )
            .add(
                "answer",
                "Retrieval exceeded the answer deadline; no speculative synthesis was attempted.",
                priority=1,
            )
            .build()
        )
    try:
        response = await asyncio.wait_for(
            get_model_router()
            .llm(TaskKind.SYNTHESIS)
            .generate(
                prompt=prompt,
                system_instruction="Be concise, evidence-grounded, and do not invent code facts. Keep the answer focused and complete — do not cut off mid-sentence.",
                max_tokens=min(2000, ASK_MAX_ANSWER_TOKENS),
                cache_hit=bool(runtime.get("_cache_hit")),
            ),
            timeout=min(15.0, remaining),
        )
        status = "ok"
        degraded: list[str] = []
    except asyncio.TimeoutError:
        response = "Synthesis timed out; the retrieved evidence is returned without a speculative answer."
        status = "partial"
        degraded = ["model_deadline_exceeded"]
    except Exception as exc:
        logger.warning("/ask v2 synthesis degraded: {}", type(exc).__name__)
        response = "Synthesis is unavailable; the retrieved evidence is insufficient for a safe answer."
        status = "partial"
        degraded = [f"model_error:{type(exc).__name__}"]
    builder = BudgetedPayloadBuilder(
        settings.AGENT_ASK_OUTPUT_MAX_BYTES,
        metadata={"status": status, "degraded": degraded},
    )
    builder.add("answer", truncate_utf8(response, settings.AGENT_ASK_OUTPUT_MAX_BYTES // 2), priority=2)
    builder.add("evidence", runtime.get("candidates", []), priority=1)
    return builder.build()


async def _impact_v2(body: ImpactRequest) -> dict:
    try:
        impact = await asyncio.wait_for(
            ImpactAnalyzer(resolve_repo_path(body.repo_path)).analyze_bounded(body.change_request),
            timeout=settings.AGENT_IMPACT_DEADLINE_S,
        )
    except asyncio.TimeoutError:
        impact = {"status": "partial", "affected": [], "degraded": ["impact_deadline_exceeded"]}
    except Exception as exc:
        logger.warning("/impact v2 degraded: {}", type(exc).__name__)
        impact = {"status": "partial", "affected": [], "degraded": [f"impact_error:{type(exc).__name__}"]}
    builder = BudgetedPayloadBuilder(
        settings.AGENT_IMPACT_MAX_BYTES,
        metadata={"status": impact.get("status", "partial"), "degraded": impact.get("degraded", [])},
    )
    builder.add("risk", impact.get("risk", {}), priority=3)
    builder.add("keywords", impact.get("keywords", []), priority=2)
    builder.add("affected", impact.get("affected", []), priority=1)
    return builder.build()


router = APIRouter()


@router.get("/health")
async def health_check():
    from brain.workers.scheduler import scheduler_status

    details = await check_health()
    is_healthy = all(svc.get("status") == "healthy" for svc in details.values())
    try:
        scheduler: dict[str, Any] = await scheduler_status(redis_client)
    except Exception as exc:  # noqa: BLE001 — Redis down is already reported in details
        scheduler = {"error": f"{type(exc).__name__}: {exc}", "stale": [], "jobs": []}
    status = "error" if not is_healthy else "degraded" if scheduler.get("stale") else "ok"
    return {
        "status": status,
        "version": __version__,
        "details": details,
        "scheduler": {
            "enabled": scheduler.get("enabled"),
            "stale": scheduler.get("stale", []),
            "jobs": {j["job_type"]: j["status"] for j in scheduler.get("jobs", [])},
            **({"error": scheduler["error"]} if "error" in scheduler else {}),
        },
    }


@router.get("/ready")
async def readiness_check():
    """Strict production readiness: dependencies, release identity, worker, alerts."""
    details = await check_health()
    build = build_info()
    checks = {
        "datastores": all(svc.get("status") == "healthy" for svc in details.values()),
        "release_identity": (
            settings.ENVIRONMENT.lower() != "production"
            or (
                build.get("build_sha") not in {None, "", "unknown"}
                and build.get("source_digest") not in {None, "", "unknown"}
            )
        ),
        "self_diagnosis": (
            not settings.SELF_DIAGNOSIS_ENABLED
            or (
                settings.TELEGRAM_ALERTS_ENABLED
                and bool(settings.TELEGRAM_ALERT_BOT_TOKEN and settings.TELEGRAM_ALERT_CHAT_ID)
            )
        ),
        # Any OpenAI-compatible provider must have its base URL, model and
        # (unless it is a keyless local endpoint) API key configured.
        "llm_provider": (settings.ENVIRONMENT.lower() != "production" or llm_provider_ready(settings)[0]),
        "worker_heartbeat": True,
    }
    worker_pools = {}
    if settings.ENVIRONMENT.lower() == "production":
        try:
            if settings.BRAIN_WORKER_POOLS_V2_ENABLED:
                worker_pools = await worker_pool_status(
                    redis_client,
                    settings.WORKER_REDIS_PREFIX,
                    pools_enabled=True,
                )
                checks["worker_heartbeat"] = all(pool["heartbeat"] for pool in worker_pools.values())
                checks["worker_capacity"] = all(pool["ready"] for pool in worker_pools.values())
            else:
                # The legacy single worker publishes only a heartbeat, not
                # queue capacity. Do not call the v2 statistics path before
                # its flag is enabled: that would turn a healthy existing
                # deployment into a false negative during the rollout.
                heartbeat = await redis_client.get(f"{settings.WORKER_REDIS_PREFIX}:heartbeat")
                checks["worker_heartbeat"] = bool(heartbeat)
                worker_pools = {"maintenance": {"heartbeat": bool(heartbeat), "legacy": True}}
        except Exception:
            checks["worker_heartbeat"] = False
            if settings.BRAIN_WORKER_POOLS_V2_ENABLED:
                checks["worker_capacity"] = False
    optional_dependencies = {"late_interaction_provider": "disabled"}
    if late_interaction_release_active():
        strict_release_readiness = settings.ENVIRONMENT.lower() == "production"
        if settings.LATE_INTERACTION_REMOTE_ENABLED:
            repository_id = settings.LATE_INTERACTION_APPROVED_REPOSITORY_ID
            if repository_id is None:
                late_health = await get_late_interaction_client().ready()
                healthy = late_health.status == "ready"
            else:
                late_health = await get_late_interaction_client().status(repository_id)
                healthy = late_health.status == "ready"
            if strict_release_readiness:
                checks["late_interaction_release"] = healthy
            optional_dependencies["late_interaction_provider"] = "healthy" if healthy else "unhealthy"
        else:
            probe = LfmColbertProvider(timeout_s=2.0)
            try:
                late_health = await probe.health()
                optional_dependencies["late_interaction_provider"] = late_health["status"]
                if strict_release_readiness:
                    checks["late_interaction_release"] = late_health["status"] in {
                        "healthy",
                        "ok",
                        "ready",
                    }
            except Exception:
                optional_dependencies["late_interaction_provider"] = "unhealthy"
                if strict_release_readiness:
                    checks["late_interaction_release"] = False
            finally:
                await probe.aclose()

    ready = all(checks.values())
    payload = {
        "status": "ready" if ready else "not_ready",
        "checks": checks,
        "optional_dependencies": optional_dependencies,
        "worker_pools": worker_pools,
        "build": build,
    }
    return JSONResponse(status_code=200 if ready else 503, content=payload)


@router.get("/api/version")
async def api_version():
    return build_info()


@router.get("/late-interaction/status", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def late_interaction_status(repo_path: str):
    """Operator evidence for canary coverage, provider health and fail-open use."""
    repository = await require_repository_by_path(resolve_repo_path(repo_path))
    return await collect_late_interaction_operator_status(repository)


@router.post("/index", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def index_repository(body: IndexRequest):
    try:
        repo_path = resolve_repo_path(body.repo_path)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    try:
        indexer = FileIndexer()
        repo = await indexer.index_repository(repo_path)
        return {
            "status": "success",
            "repo_id": repo.id,
            "path": repo.path,
            "commit_hash": repo.last_indexed_commit,
        }
    except Exception as exc:
        logger.error(f"Indexing failed for {body.repo_path}: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Indexing failed due to an internal error") from exc


@router.post("/ask", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def ask_project(body: AskRequest, request: Request):
    mode = _agent_v2_mode()
    if mode == "on":
        return await _ask_v2(body)
    if mode == "shadow":
        try:
            runtime = await RuntimeContextBuilder().build(
                body.retrieval_query or body.query,
                _agent_repo_path(body.repo_path),
                max_tokens=3500,
            )
            _record_shadow_metric("ask_runtime", runtime)
        except Exception as exc:
            logger.warning("/ask v2 shadow retrieval failed open: {}", type(exc).__name__)
    try:
        retrieval_kwargs: dict[str, Any] = {
            "limit": ASK_RETRIEVAL_LIMIT,
            "repo_path": body.repo_path,
        }
        request_id = _late_interaction_request_id(request)
        if request_id:
            retrieval_kwargs["request_id"] = request_id
        code_results = await hybrid_search_code(
            body.retrieval_query or body.query,
            **retrieval_kwargs,
        )

        repository_scope = code_results.get("repository_scope") or {}
        rule_scope = repository_scope.get("repository_path") if repository_scope.get("found") else body.repo_path
        active_rules = await RuleStore.list_active_rules(rule_scope)
        rules_str = "\n".join([f"- {r.id}: {r.name} - {r.description}" for r in active_rules])
        try:
            decision_scope = normalize_repo_scope(rule_scope)
            decisions = await DecisionStore.list_decisions()
            active_decisions = [
                decision
                for decision in decisions
                if (decision.status or "").lower() == "active"
                # repo_path is NULL for cross-project decisions; scoping them out
                # would hide exactly the architectural memory worth recalling.
                and (decision.repo_path is None or normalize_repo_scope(decision.repo_path) == decision_scope)
            ]
            recorded_decisions_str = (
                "\n".join(
                    _decision_line(d)
                    for d in sorted(active_decisions, key=lambda d: d.id, reverse=True)[:ASK_DECISION_LIMIT]
                )
                or "None"
            )
        except Exception as exc:
            logger.warning(f"Decision retrieval skipped for /ask: {exc}")
            recorded_decisions_str = "Unavailable"

        learnings = []
        try:
            # Query-ranked: most relevant learnings first, not just newest.
            # Uses pre-computed embeddings (stored at write time) so this
            # costs one query embedding, not N. Falls back to confidence
            # order if ranking fails.
            from brain.context.context_pack_builder import _load_active_learnings

            learnings = await _load_active_learnings(
                rule_scope,
                query=body.retrieval_query or body.query,
                limit=ASK_DECISION_LIMIT,
            )
            learnings_str = "\n".join(f"- {lrng.statement}" for lrng in learnings) or "None"
        except Exception as exc:
            logger.warning(f"Learning retrieval skipped for /ask: {exc}")
            learnings_str = "Unavailable"

        repository_freshness = repository_scope.get("freshness", {}) if isinstance(repository_scope, dict) else {}
        freshness_status = repository_freshness.get("status")
        freshness_warning = ""
        if freshness_status and freshness_status != "current":
            freshness_warning = (
                "### Repository Freshness Warning:\n"
                f"- status: {freshness_status}\n"
                f"- source_present: {repository_freshness.get('source_present')}\n"
                f"- source_head_commit: {repository_freshness.get('source_head_commit')}\n"
                f"- indexed_commit: {repository_freshness.get('indexed_commit')}\n"
                f"- last_indexed_at: {repository_freshness.get('last_indexed_at')}\n"
                f"- age_seconds: {repository_freshness.get('age_seconds')}\n"
                f"- commits_behind: {repository_freshness.get('commits_behind')}\n"
                f"- note: {repository_freshness.get('note')}\n\n"
            )

        files_str = "\n".join([f"- {f['path']}: {f['summary']}" for f in code_results["files"]])
        symbols_str = "\n".join([f"- {s['name']} ({s['kind']}): {s['summary']}" for s in code_results["symbols"]])
        chunks_str = "\n\n".join([f"--- Chunk ---\n{c['content']}" for c in code_results["chunks"]])

        context = (
            f"{freshness_warning}"
            f"### Relevant Files:\n{files_str or 'None'}\n\n"
            f"### Relevant Symbols:\n{symbols_str or 'None'}\n\n"
            f"### Relevant Code Chunks:\n{chunks_str or 'None'}\n\n"
            f"### Recorded Decisions:\n{recorded_decisions_str}\n\n"
            f"### Active Rules:\n{rules_str or 'None'}\n\n"
            f"### Consolidated Learnings:\n{learnings_str}"
        )

        prompt = (
            f"You are the technical assistant for Project Brain.\n"
            f"Answer the following user query about the project codebase:\n"
            f'"""\n{body.query}\n"""\n\n'
            f"Using the gathered project context:\n{context}\n\n"
            f"Answer densely and concretely: cite file paths and decision ids from the "
            f"context instead of restating the question, say plainly when the "
            f"context does not contain the answer rather than guessing, and "
            f"explicitly disclose any freshness warning before relying on indexed "
            f"context for the answer."
        )

        llm = get_model_router().llm(TaskKind.SYNTHESIS)
        # Harsh benchmark revealed /ask hanging 3+ min on slow LLM API.
        # 120s timeout with graceful 500 (not a hang) is the honest behavior.
        import asyncio as _asyncio
        response = await _asyncio.wait_for(llm.generate(
            prompt=prompt,
            system_instruction=(
                "You are an expert developer working on Project Brain. Be specific and "
                "brief — at most ~8 sentences unless the question demands more. "
                "If you need to reason, do it in at most 3 short sentences. "
                "You MUST end your response with a line containing exactly "
                "'FINAL ANSWER:' followed by the answer itself. Only the text "
                "after 'FINAL ANSWER:' is shown to the user. "
                # Without this the model refuses non-English questions outright
                # ("I couldn't understand your query as it seems to be in a
                # different language"), which makes the Telegram bot useless to
                # a Russian-speaking owner asking about an English codebase.
                "Always answer in the same language the question was asked in, "
                "keeping code, file paths and identifiers verbatim."
            ),
            # Latency is a correctness concern here: callers reach /ask through an MCP
            # proxy whose client-side tool timeout is shorter than an unbounded
            # llama-3.1-70b answer (measured 38-90s), and a timed-out answer is worth
            # nothing no matter how good it would have been. Capping generation keeps
            # the endpoint inside that budget; the wider retrieval above is what makes
            # the shorter answer better rather than thinner.
            max_tokens=ASK_MAX_ANSWER_TOKENS,
        ), timeout=120)
        return {
            "answer": _strip_thinking_blocks(response),
            "learnings_used": [
                {"id": lrng.id, "statement": lrng.statement, "confidence": lrng.confidence} for lrng in learnings
            ],
        }
    except Exception as exc:
        # The exception type carries the diagnosis here: httpx timeouts and
        # asyncio cancellations both stringify to "", so the old message was
        # literally "Query failed: " — a 500 with nothing to act on. The same
        # blind spot hid a latency problem in the MCP proxy for weeks.
        logger.exception(f"/ask failed for repo={body.repo_path}: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Query failed due to an internal error") from exc


@router.post("/context", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def build_task_context(body: ContextRequest):
    # `persist=false` is a public no-write contract, independent of rollout
    # mode. The legacy pack builder always creates both a Markdown artifact and
    # a ContextPack row, so it must only be reachable via explicit persistence.
    if not body.persist:
        try:
            return await RuntimeContextBuilder().build(
                body.task_description,
                _agent_repo_path(body.repo_path),
                max_tokens=body.max_tokens,
                include_debug=body.include_debug,
            )
        except Exception as exc:
            logger.warning("/context v2 degraded: {}", type(exc).__name__)
            return BudgetedPayloadBuilder(
                min(settings.AGENT_RUNTIME_CONTEXT_MAX_BYTES, body.max_tokens * 4),
                metadata={"status": "partial", "missing": [f"context_error:{type(exc).__name__}"]},
            ).build()
    try:
        builder = ContextPackBuilder()
        repo_path = resolve_repo_path(body.repo_path)
        result = await builder.build_context_pack(body.task_description, repo_path)
        return result
    except Exception as exc:
        logger.error(f"Context pack failed for {body.repo_path}: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Context pack compilation failed due to an internal error") from exc


@router.post("/impact", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def analyze_change_impact(body: ImpactRequest):
    if _agent_v2_mode() == "on":
        return await _impact_v2(body)
    if _agent_v2_mode() == "shadow":
        try:
            _record_shadow_metric("impact", await _impact_v2(body))
        except Exception as exc:
            logger.warning("/impact v2 shadow failed open: {}", type(exc).__name__)
    try:
        analyzer = ImpactAnalyzer(resolve_repo_path(body.repo_path))
        return await analyzer.analyze_impact(body.change_request, max_results=body.max_results, fast=body.fast)
    except Exception as exc:
        logger.error(f"Impact analysis failed for {body.repo_path}: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Impact analysis failed due to an internal error") from exc


@router.post("/diff-review", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def review_git_diff(body: DiffReviewRequest):
    """Start an async diff review. Returns immediately with a job ID.

    The review runs in the background (LLM calls can take minutes with retries).
    Poll GET /diff-review/{job_id} for the result.
    """
    try:
        from brain.database.session import redis_client
        from brain.workers.queue import queue_for_job

        queue = queue_for_job(
            redis_client,
            settings.WORKER_REDIS_PREFIX,
            "diff_review",
            pools_enabled=settings.BRAIN_WORKER_POOLS_V2_ENABLED,
        )
        job_id = await queue.enqueue(
            "diff_review",
            {"repo_path": body.repo_path, "base": body.base, "head": body.head},
        )
        return {
            "status": "processing",
            "job_id": job_id,
            "message": "Diff review queued. Poll GET /diff-review/{job_id} for results.",
        }
    except Exception as exc:
        logger.error(f"Failed to queue diff review for {body.repo_path}: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to queue diff review") from exc


@router.get("/diff-review/{job_id}", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def get_diff_review_result(job_id: str):
    """Get the result of an async diff review."""
    try:
        from brain.database.session import redis_client
        from brain.workers.queue import queue_for_job

        queue = queue_for_job(
            redis_client,
            settings.WORKER_REDIS_PREFIX,
            "diff_review",
            pools_enabled=settings.BRAIN_WORKER_POOLS_V2_ENABLED,
        )
        job = await queue.get_job(job_id)
        if not job or job.get("type") != "diff_review":
            raise HTTPException(status_code=404, detail="Review job not found")
        status = job.get("status", "unknown")
        if status == "completed":
            return {
                "status": "completed",
                "result": job.get("result", {}),
            }
        elif status == "failed":
            return {
                "status": "failed",
                "error": job.get("error", "Unknown error"),
            }
        else:
            return {
                "status": status,  # queued, running, etc.
                "message": "Review still in progress.",
            }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to get review result {job_id}: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to get review result") from exc


# Cap the second-hop graph fan-out: one Neo4j round-trip per direct neighbour,
# so a densely-connected file can't open thousands of sequential sessions.
_MAX_RELATED_FANOUT = 50


@router.post("/search", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def search_code_endpoint(body: SearchRequest, request: Request):
    """Hybrid file/symbol/chunk search — the HTTP form of the MCP `search_code` tool."""
    mode = _agent_v2_mode()
    if mode == "on" and body.response_mode == "locator":
        return await _compact_locator(body)
    if mode == "shadow" and body.response_mode == "locator":
        try:
            _record_shadow_metric("search", await _compact_locator(body))
        except Exception as exc:
            logger.warning("/search v2 shadow failed open: {}", type(exc).__name__)
    try:
        search_kwargs: dict[str, Any] = {"limit": body.limit, "repo_path": body.repo_path}
        request_id = _late_interaction_request_id(request)
        if request_id:
            search_kwargs["request_id"] = request_id
        return await hybrid_search_code(body.query, **search_kwargs)
    except Exception as exc:
        logger.error(f"Search failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Search failed due to an internal error") from exc


@router.get("/repositories", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def list_repositories():
    try:
        async with async_session_factory() as session:
            stmt = select(Repository).order_by(Repository.id.asc())
            rows = await session.execute(stmt)
            repos = rows.scalars().all()

        return {
            "repositories": [
                {
                    "id": repo.id,
                    "name": repo.name,
                    "path": repo.path,
                    "type": repo.type,
                    "language_stack": repo.language_stack,
                    "indexing_status": repo.indexing_status,
                    "last_indexed_commit": repo.last_indexed_commit,
                    # The model has no last_indexed_at column; updated_at is the
                    # timestamp indexing actually bumps. Reading the missing
                    # attribute directly made this endpoint 500 on every call.
                    "last_indexed_at": repo.updated_at,
                    "freshness": await assess_repository_freshness(repo),
                }
                for repo in repos
            ]
        }
    except Exception as exc:
        logger.error(f"Failed to fetch repositories: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch repositories") from exc


@router.post("/related", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def related_files_endpoint(body: RelatedRequest):
    """Graph neighborhood lookup — the HTTP form of the MCP `find_related_files` tool."""
    try:
        repository = await require_repository_by_path(
            resolve_repo_path(body.repo_path) if body.repo_path else get_repo_root()
        )
        client = GraphClient(repository_id=repository.id)
        directly, indirectly, visited = [], [], set()
        for nb in await client.get_neighbors(body.file_path):
            props = nb["target"]["properties"]
            tgt = props.get("name") or props.get("path")
            if tgt and "File" in nb["target"]["labels"] and tgt != body.file_path and tgt not in visited:
                visited.add(tgt)
                directly.append(tgt)
        for d_file in directly[:_MAX_RELATED_FANOUT]:
            for nb in await client.get_neighbors(d_file):
                props = nb["target"]["properties"]
                tgt = props.get("name") or props.get("path")
                if (
                    tgt
                    and "File" in nb["target"]["labels"]
                    and tgt != body.file_path
                    and tgt not in directly
                    and tgt not in visited
                ):
                    visited.add(tgt)
                    indirectly.append(tgt)
        return {"file": body.file_path, "directly_related_files": directly, "indirectly_related_files": indirectly}
    except Exception as exc:
        logger.error(f"Related lookup failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Related lookup failed due to an internal error") from exc


@router.get("/decisions", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def get_decisions():
    try:
        return await DecisionStore.list_decisions()
    except Exception as exc:
        logger.error(f"Failed to fetch decisions: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch decisions") from exc


@router.post("/decisions", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def create_decision(body: DecisionCreate):
    try:
        decision_id = await DecisionStore.add_decision(
            title=body.title,
            repo_path=body.repo_path,
            description=body.description,
            status=body.status,
            date=body.date,
            reason=body.reason,
            consequences=body.consequences,
            affected_features=body.affected_features,
            affected_modules=body.affected_modules,
            affected_files=body.affected_files,
        )
        return {"status": "success", "decision_id": decision_id}
    except Exception as exc:
        logger.error(f"Failed to add decision: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to add decision") from exc


@router.get("/rules", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def get_rules():
    try:
        return await RuleStore.list_rules()
    except Exception as exc:
        logger.error(f"Failed to fetch rules: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch rules") from exc


@router.post("/rules", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def create_rule(body: RuleCreate):
    try:
        rule_id = await RuleStore.add_rule(
            name=body.name,
            repo_path=body.repo_path,
            description=body.description,
            type=body.type,
            severity=body.severity,
            status=body.status,
            applies_to=body.applies_to,
            rule_id=body.rule_id,
        )
        return {"status": "success", "rule_id": rule_id}
    except Exception as exc:
        logger.error(f"Failed to add rule: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to add rule") from exc


@router.get("/learnings", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def get_learnings(repo_path: Optional[str] = None):
    try:
        learnings = await LearningStore.list_active_learnings(repo_path)
        return [
            {
                "id": lrng.id,
                "statement": lrng.statement,
                "category": lrng.category,
                "confidence": lrng.confidence,
                "repo_scope": lrng.repo_scope,
                "status": lrng.status,
            }
            for lrng in learnings
        ]
    except Exception as exc:
        logger.error(f"Failed to fetch learnings: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch learnings") from exc


@router.get("/episodes", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def get_episodes(
    status: Optional[Literal["pending", "promoted", "rejected", "duplicate", "merged"]] = None,
    topic: Optional[str] = None,
    limit: int = Query(50, ge=1, le=EPISODES_MAX_LIMIT),
):
    """L2 episodic memory: list episodes, newest first."""
    try:
        from brain.database.models import MemoryEpisode
        from sqlalchemy import desc

        async with async_session_factory() as session:
            q = select(MemoryEpisode).order_by(desc(MemoryEpisode.created_at)).limit(limit)
            if status:
                q = q.where(MemoryEpisode.status == status)
            if topic:
                q = q.where(MemoryEpisode.topic == topic)
            episodes = (await session.execute(q)).scalars().all()
            return [
                {
                    "id": ep.id,
                    "distilled_summary": ep.distilled_summary,
                    "topic": ep.topic,
                    "status": ep.status,
                    "confidence": ep.confidence,
                    "source_event_ids": ep.source_event_ids,
                    "promoted_to_learning_id": ep.promoted_to_learning_id,
                    "duplicate_of_learning_id": ep.duplicate_of_learning_id,
                    "gate_reasons": ep.gate_reasons,
                    "created_at": ep.created_at.isoformat() if ep.created_at else None,
                }
                for ep in episodes
            ]
    except Exception as exc:
        logger.error(f"Failed to fetch episodes: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch episodes") from exc


@router.post("/episodes/search", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def search_episodes(body: EpisodeSearchRequest):
    """L2 episodic memory: semantic search over episode summaries."""
    try:
        from brain.database.models import MemoryEpisode
        from brain.embeddings.constants import EMBEDDING_DIMENSION
        from brain.embeddings.pgvector_sql import truncate_vector_for_index
        from brain.llm import get_embedding_provider

        query_vec = await get_embedding_provider().embed(body.query)
        if not query_vec:
            raise HTTPException(status_code=500, detail="Embedding failed")
        query_vec = truncate_vector_for_index(list(query_vec), EMBEDDING_DIMENSION)

        async with async_session_factory() as session:
            q = select(MemoryEpisode).where(MemoryEpisode.embedding.isnot(None))
            if body.status:
                q = q.where(MemoryEpisode.status == body.status)
            q = q.order_by(MemoryEpisode.embedding.cosine_distance(query_vec)).limit(body.limit)
            episodes = (await session.execute(q)).scalars().all()
            return [
                {
                    "id": ep.id,
                    "distilled_summary": ep.distilled_summary,
                    "topic": ep.topic,
                    "status": ep.status,
                    "confidence": ep.confidence,
                    "created_at": ep.created_at.isoformat() if ep.created_at else None,
                }
                for ep in episodes
            ]
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Episode search failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Episode search failed") from exc


async def _decide_episode(episode_id: int, body: Optional[EpisodeDecisionRequest], *, approve: bool) -> dict:
    from brain.memory.consolidation import EpisodeStateError, approve_episode, reject_episode

    reason = body.reason if body else None
    try:
        if approve:
            return await approve_episode(episode_id, reason)
        return await reject_episode(episode_id, reason)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except EpisodeStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(f"Episode decision failed for {episode_id}: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Episode decision failed") from exc


@router.post(
    "/episodes/{episode_id}/approve",
    dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))],
)
async def approve_pending_episode(episode_id: int, body: Optional[EpisodeDecisionRequest] = None):
    """Promote a pending (needs_approval) L2 episode into an L3 learning."""
    return await _decide_episode(episode_id, body, approve=True)


@router.post(
    "/episodes/{episode_id}/reject",
    dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))],
)
async def reject_pending_episode(episode_id: int, body: Optional[EpisodeDecisionRequest] = None):
    """Reject a pending L2 episode."""
    return await _decide_episode(episode_id, body, approve=False)


# === L4 Procedural Memory: Skills Registry ===

@router.get("/skills", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def get_skills(status: Optional[str] = "active", limit: int = 50):
    """L4 procedural memory: list learned skills and workflows."""
    try:
        from brain.database.models import MemorySkill
        from sqlalchemy import desc

        async with async_session_factory() as session:
            q = select(MemorySkill).order_by(desc(MemorySkill.confidence)).limit(limit)
            if status:
                q = q.where(MemorySkill.status == status)
            skills = (await session.execute(q)).scalars().all()
            return [
                {
                    "id": s.id,
                    "name": s.name,
                    "description": s.description,
                    "triggers": s.triggers,
                    "workflow": s.workflow,
                    "times_used": s.times_used,
                    "times_successful": s.times_successful,
                    "success_rate": round(s.times_successful / s.times_used, 3) if s.times_used > 0 else None,
                    "status": s.status,
                    "confidence": s.confidence,
                }
                for s in skills
            ]
    except Exception as exc:
        logger.error(f"Failed to fetch skills: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch skills") from exc


@router.post("/skills", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def create_skill(body: SkillCreate):
    """L4 procedural memory: register a new skill/workflow."""
    try:
        from brain.database.models import MemorySkill
        from brain.llm import get_embedding_provider

        name = body.name.strip()
        description = body.description.strip()
        if not name or not description:
            raise HTTPException(status_code=400, detail="name and description are required")

        async with async_session_factory() as session:
            existing = await session.execute(select(MemorySkill).where(MemorySkill.name == name))
            if existing.scalar_one_or_none():
                raise HTTPException(status_code=409, detail=f"Skill '{name}' already exists")

            embedding = None
            try:
                vec = await get_embedding_provider().embed(f"{name}: {description}")
                embedding = list(vec) if vec else None
            except Exception as emb_exc:
                logger.warning(f"Skill embedding failed, storing NULL: {emb_exc}")

            skill = MemorySkill(
                name=name,
                description=description,
                triggers=body.triggers,
                workflow=body.workflow,
                source_episode_ids=body.source_episode_ids,
                source_learning_ids=body.source_learning_ids,
                confidence=body.confidence,
                embedding=embedding,
                repo_scope=body.repo_scope,
            )
            session.add(skill)
            await session.commit()
            return {"id": skill.id, "name": skill.name, "status": "created"}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to create skill: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to create skill") from exc


@router.post("/skills/match", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def match_skills(body: SkillMatchRequest):
    """L4 procedural memory: find skills relevant to a task (embedding order, trigger re-rank)."""
    try:
        from brain.database.models import MemorySkill
        from brain.llm import get_embedding_provider

        query_vec = await get_embedding_provider().embed(body.query)

        async with async_session_factory() as session:
            q = select(MemorySkill).where(
                MemorySkill.status == "active",
                MemorySkill.embedding.isnot(None),
            )
            if query_vec:
                q = q.order_by(MemorySkill.embedding.cosine_distance(query_vec))
            q = q.limit(body.limit * 2)  # over-fetch for trigger re-ranking
            skills = (await session.execute(q)).scalars().all()

            query_lower = body.query.lower()
            scored = []
            for s in skills:
                score = 0.0
                for trigger in (s.triggers or []):
                    if trigger.lower() in query_lower:
                        score += 1.0
                if s.times_used > 0:
                    score += (s.times_successful / s.times_used) * 0.5
                scored.append((score, s))
            scored.sort(key=lambda x: x[0], reverse=True)

            return [
                {
                    "id": s.id,
                    "name": s.name,
                    "description": s.description,
                    "workflow": s.workflow,
                    "confidence": s.confidence,
                    "trigger_score": score,
                }
                for score, s in scored[: body.limit]
            ]
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Skill matching failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Skill matching failed") from exc


@router.post("/learnings", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def create_learning(body: LearningCreate):
    try:
        valid_until = None
        if body.valid_until:
            from datetime import datetime

            valid_until = datetime.fromisoformat(body.valid_until)
        learning_id = await LearningStore.add_learning(
            statement=body.statement,
            category=body.category,
            confidence=body.confidence,
            repo_scope=body.repo_scope,
            valid_until=valid_until,
        )
        return {"status": "success", "learning_id": learning_id}
    except Exception as exc:
        logger.error(f"Failed to add learning: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to add learning") from exc


@router.delete(
    "/learnings/{learning_id}", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))]
)
async def delete_learning(learning_id: int):
    try:
        await LearningStore.reject(learning_id)
        return {"status": "success", "learning_id": learning_id}
    except Exception as exc:
        logger.error(f"Failed to reject learning: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to reject learning") from exc


@router.get("/features", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def list_features():
    from brain.database.session import neo4j_driver

    query = "MATCH (f:Feature) RETURN f"
    features = []
    try:
        async with neo4j_driver.session() as session:
            result = await session.run(query)
            async for record in result:
                node = record["f"]
                features.append({"name": node.get("name"), "properties": dict(node)})
    except Exception as exc:
        logger.error(f"Failed to list features from Neo4j: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to list features") from exc
    return features


@router.post("/features", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def create_feature(body: FeatureCreate):
    try:
        repository = await require_repository_by_path(
            resolve_repo_path(body.repo_path) if body.repo_path else get_repo_root()
        )
        client = GraphClient(repository_id=repository.id)
        await client.create_node(NodeType.FEATURE.value, body.name, body.properties)
        return {"status": "success", "message": f"Feature '{body.name}' created in Neo4j."}
    except Exception as exc:
        logger.error(f"Failed to create feature: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to create feature") from exc


@router.get("/api/status", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def api_overall_status():
    services = await check_health()
    is_healthy = all(svc.get("status") == "healthy" for svc in services.values())
    repo_path = get_repo_root()
    commit_hash = get_git_commit_hash(repo_path)

    async with async_session_factory() as session:
        counts = await get_db_counts(session)

    return {
        "status": "ok" if is_healthy else "error",
        "version": __version__,
        "services": services,
        "repo_path": repo_path.as_posix(),
        "commit_hash": commit_hash,
        "counts": counts,
    }


@router.get("/api/status/diagnostics", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def diagnostics_status():
    return await get_self_diagnosis_status()


@router.get("/insights", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def list_insights(status: str | None = None, limit: int = 20):
    safe_limit = max(1, min(int(limit or 20), 100))
    async with async_session_factory() as session:
        stmt = select(BrainInsight).order_by(BrainInsight.last_seen_at.desc(), BrainInsight.id.desc()).limit(safe_limit)
        if status:
            stmt = stmt.where(BrainInsight.status == status)
        rows = (await session.execute(stmt)).scalars().all()
        return {
            "insights": [
                {
                    "id": row.id,
                    "insight_type": row.insight_type,
                    "severity": row.severity,
                    "title": row.title,
                    "summary": row.summary,
                    "evidence": row.evidence or [],
                    "recommended_action": row.recommended_action,
                    "confidence": row.confidence,
                    "status": row.status,
                    "source": row.source,
                    "source_model": row.source_model,
                    "engine_version": row.engine_version,
                    "occurrence_count": row.occurrence_count,
                    "created_at": format_datetime_utc(row.created_at),
                    "last_seen_at": format_datetime_utc(row.last_seen_at),
                }
                for row in rows
            ]
        }


@router.get("/api/status/services", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def api_services_status():
    return await check_health()


@router.get("/api/status/indexing", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def api_indexing_status():
    from brain.database.models import IndexingRun

    async with async_session_factory() as session:
        counts = await get_db_counts(session)
        stmt = select(IndexingRun).order_by(IndexingRun.id.desc()).limit(5)
        res = await session.execute(stmt)
        runs = res.scalars().all()
        runs_list = [
            {
                "id": run.id,
                "started_at": format_datetime_utc(run.started_at),
                "commit_hash": run.commit_hash,
                "status": run.status,
                "completed_at": format_datetime_utc(run.completed_at),
                "repository_id": run.repository_id,
                "updated_at": format_datetime_utc(getattr(run, "updated_at", None)),
                "progress": getattr(run, "progress", None) or {},
                "verification": getattr(run, "verification", None),
            }
            for run in runs
        ]

    embedding_health = await get_embedding_retrieval_health()

    return {
        "counts": counts,
        "recent_runs": runs_list,
        "evaluation_score": get_latest_eval_metrics(),
        "cache_status": get_cache_status(),
        "embedding_health": embedding_health,
    }


@router.get("/api/status/graph", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def api_graph_status():
    services = await check_health()
    counts = await get_neo4j_counts()
    top_connected, top_error = await get_top_connected_nodes(5)
    return {
        "neo4j_status": services.get("neo4j"),
        "counts": counts,
        "top_connected": top_connected,
        "top_connected_error": top_error,
        "cache_status": get_cache_status(),
        "evaluation_score": get_latest_eval_metrics(),
    }


@router.get("/api/status/mcp", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def api_mcp_status():
    runtime = await get_mcp_runtime_status()
    tools = runtime.get("tools", [])
    available = bool(runtime.get("available"))
    server_status = runtime.get("server_status", "online" if available else "unavailable")
    return {
        "server_status": server_status,
        "available": available,
        "total_tools": len(tools),
        "tools": tools,
        "status": runtime.get("status", server_status),
        "diagnostics": runtime.get("diagnostics", {}),
    }


@router.get("/api/status/mcp/readiness", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def api_mcp_readiness_status(timeout_seconds: float = 30.0):
    return await get_mcp_readiness_status(timeout_seconds=timeout_seconds)


@router.get("/api/status/recent-activity", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
async def api_recent_activity():
    from brain.database.models import ContextPack, IndexingRun

    reports = get_reports_list()
    async with async_session_factory() as session:
        stmt_idx = select(IndexingRun).order_by(IndexingRun.id.desc()).limit(3)
        res_idx = await session.execute(stmt_idx)
        runs = res_idx.scalars().all()

        stmt_cp = select(ContextPack).order_by(ContextPack.id.desc()).limit(3)
        res_cp = await session.execute(stmt_cp)
        packs = res_cp.scalars().all()

    activity = []
    latest_critic = "UNKNOWN"

    for run in runs:
        activity.append(
            {
                "title": f"Indexing Run #{run.id}",
                "timestamp": format_datetime_utc(run.started_at),
                "description": f"Status: {run.status}, Commit: {_short_commit_ref(run.commit_hash)}",
            }
        )
    for pack in packs:
        critic_status = get_latest_critic_status(pack.path)
        if latest_critic == "UNKNOWN":
            latest_critic = critic_status
        activity.append(
            {
                "title": f"Context Pack #{pack.id}",
                "timestamp": format_datetime_utc(pack.created_at),
                "description": f"Generated path: {Path(pack.path).name}, Critic: {critic_status}",
            }
        )
    for report in reports[:3]:
        activity.append(
            {
                "title": f"Report: {report['name']}",
                "timestamp": report["created_at"],
                "description": f"Size: {report['size']} bytes",
            }
        )
    activity.sort(key=lambda item: item["timestamp"] or "", reverse=True)

    return {
        "activity": activity[:10],
        "latest_critic_status": latest_critic,
        "evaluation_score": get_latest_eval_metrics(),
    }
