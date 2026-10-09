"""Retrieval and code-intelligence endpoints: /index, /ask, /context, /impact,
/diff-review (+ job polling), /search, /repositories, /related."""

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from loguru import logger

from apps.api.auth import require_api_key, require_scope
from apps.api.schemas import (
    AskRequest,
    ContextRequest,
    DiffReviewRequest,
    ImpactRequest,
    IndexRequest,
    RelatedRequest,
    SearchRequest,
)
from brain.analyzers.impact_analyzer import ImpactAnalyzer
from brain.config.paths import get_repo_root, resolve_repo_path
from brain.config.settings import settings
from brain.context.context_pack_builder import ContextPackBuilder
from brain.context.budget import BudgetedPayloadBuilder, truncate_utf8
from brain.context.runtime_context_builder import RuntimeContextBuilder
from brain.database.models import Repository
from brain.database.repository_utils import require_repository_by_path
from brain.database.session import async_session_factory
from brain.graph.graph_client import GraphClient
from brain.indexers.file_indexer import FileIndexer
from brain.llm.router import TaskKind, get_model_router
from brain.memory.decision_store import DecisionStore
from brain.memory.repo_freshness import assess_repository_freshness
from brain.memory.repo_scope import normalize_repo_scope
from brain.memory.rule_store import RuleStore
from brain.search.code_search import search_code as hybrid_search_code
from brain.retrieval.service import RetrievalService

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
# /ask is also exposed through MCP clients with a short client deadline. A
# dedicated model can be opted into with LLM_TASK_ASK_MODEL; absent that
# setting we preserve the configured synthesis model and only bound the call.
ASK_DEADLINE_S = 15.0
ASK_COMPACT_MAX_TOKENS = 900


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
        runtime = await asyncio.wait_for(RuntimeContextBuilder().build(
            body.retrieval_query or body.query,
            _agent_repo_path(body.repo_path),
            max_tokens=3500,
            include_debug=body.include_debug,
        ), timeout=settings.AGENT_ASK_DEADLINE_S)
    except asyncio.TimeoutError:
        runtime = {"status": "partial", "missing": ["ask_deadline_exceeded"]}
    except Exception as exc:
        logger.warning("/ask v2 retrieval degraded: {}", type(exc).__name__)
        runtime = {"status": "partial", "missing": [f"context_error:{type(exc).__name__}"]}
    if runtime.get("status") != "ok":
        message = ("Current indexed context is not fresh enough to answer safely."
                   if runtime.get("status") == "stale_blocked"
                   else "No sufficient relevant indexed evidence is available to answer safely.")
        return (
            BudgetedPayloadBuilder(
                settings.AGENT_ASK_OUTPUT_MAX_BYTES,
                metadata={"status": "partial", "degraded": runtime.get("missing", ["insufficient_context"])},
            )
            .add("answer", message, priority=1)
            .build()
        )

    context_json = json.dumps(runtime, ensure_ascii=False, separators=(",", ":"))
    prompt_context = truncate_utf8(context_json, settings.AGENT_ASK_INPUT_MAX_BYTES)
    prompt = (
        "You are the technical assistant for Project Brain. Answer in the same language as the question. "
        "Use only the supplied evidence; cite paths/ranges, and state when evidence is insufficient. "
        "Return only the final answer, at most 120 words, covering every part of the question. "
        "Learned guidance is historical advice; code facts must come from the supplied current slices.\n\n"
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
    generation_meta: dict[str, Any] = {}
    try:
        router = get_model_router()
        ask_model = str(getattr(settings, "LLM_TASK_ASK_MODEL", "") or "").strip()
        llm = router.llm_for_model(ask_model) if ask_model else router.llm(TaskKind.SYNTHESIS)
        generation_kwargs: dict[str, Any] = dict(
            prompt=prompt,
            system_instruction="Be concise, evidence-grounded, and do not invent code facts. Keep the answer focused and complete — do not cut off mid-sentence.",
            max_tokens=min(ASK_COMPACT_MAX_TOKENS, ASK_MAX_ANSWER_TOKENS),
            cache_hit=bool(runtime.get("_cache_hit")),
        )
        if (settings.LLM_TASK_ASK_ENABLE_THINKING is not None
                and getattr(llm, "provider", None) == "nvidia"
                and "nemotron-3" in str(getattr(llm, "model", "")).casefold()):
            generation_kwargs["chat_template_kwargs"] = {"enable_thinking": settings.LLM_TASK_ASK_ENABLE_THINKING}
        generator = getattr(llm, "generate_with_metadata", None)
        if generator is None:
            generation = await asyncio.wait_for(llm.generate(**generation_kwargs), timeout=remaining)
        else:
            generation = await asyncio.wait_for(generator(**generation_kwargs), timeout=remaining)
        if isinstance(generation, dict):
            response = str(generation.get("text") or "")
            generation_meta = {k: generation[k] for k in ("finish_reason", "usage") if generation.get(k) is not None}
        else:
            response = str(generation or "")
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
    raw_response = response
    open_thinking = any(
        token in raw_response.lower() and close not in raw_response.lower()
        for token, close in (("<think>", "</think>"), ("<reasoning>", "</reasoning>"), ("<thought>", "</thought>"))
    )
    response = "" if open_thinking else _strip_thinking_blocks(raw_response)
    finish_reason = generation_meta.get("finish_reason")
    if finish_reason in {"length", "max_tokens", "token_limit"}:
        status = "partial"
        degraded.append("generation_token_limit")
    if open_thinking:
        status = "partial"
        degraded.append("open_thinking_block")
    if not response:
        status = "partial"
        degraded.append("empty_generation")
    answer = truncate_utf8(response, settings.AGENT_ASK_OUTPUT_MAX_BYTES * 3 // 5)
    if answer != response:
        status = "partial"
        degraded.append("answer_output_cap")
    builder = BudgetedPayloadBuilder(
        settings.AGENT_ASK_OUTPUT_MAX_BYTES,
        metadata={"status": status, "degraded": degraded, **generation_meta},
    )
    builder.add("answer", answer, priority=2)
    builder.add("evidence", runtime.get("candidates", []), priority=1)
    built = builder.build()
    if built.get("answer") != response and status == "ok":
        built["status"] = "partial"
        built["degraded"] = [*degraded, "answer_output_cap"]
        return (BudgetedPayloadBuilder(settings.AGENT_ASK_OUTPUT_MAX_BYTES,
                    metadata={"status": "partial", "degraded": built["degraded"], **generation_meta})
                .add("answer", built.get("answer", ""), priority=2)
                .add("evidence", built.get("evidence", []), priority=1).build())
    return built


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
        if not any(code_results.get(key) for key in ("files", "symbols", "chunks")):
            return {
                "answer": "The indexed evidence did not contain relevant code for this question, so no grounded answer was generated.",
                "status": "partial",
                "degraded": ["no_retrieval_candidates"],
                "evidence_available": False,
            }

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

        # L4 procedural memory, opt-in behind MEMORY_SKILLS_IN_ASK: top matching
        # active skills for this repository scope injected as a procedures
        # block, byte-capped. A skill scoped to another repo is never injected.
        skills_used = []
        procedures_str = ""
        if settings.MEMORY_SKILLS_IN_ASK:
            try:
                from brain.memory.skill_store import select_skills_for_context

                skills_used = await select_skills_for_context(
                    body.retrieval_query or body.query,
                    rule_scope,
                    max_count=settings.MEMORY_SKILLS_IN_ASK_TOP,
                )
                if skills_used:
                    lines = []
                    for skill in skills_used:
                        steps = "; ".join(str(step) for step in skill["workflow"]) or skill["description"]
                        lines.append(f"- {skill['name']}: {steps}")
                    procedures_str = "\n".join(lines)[: settings.MEMORY_SKILLS_IN_ASK_MAX_BYTES]
            except Exception as exc:
                logger.warning(f"Skill retrieval skipped for /ask: {exc}")
                skills_used = []
                procedures_str = ""

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
            # The procedures section only exists when the L4-in-/ask flag is
            # on — flag-off output is byte-identical to before the feature.
            + (f"\n\n### Procedures:\n{procedures_str}" if settings.MEMORY_SKILLS_IN_ASK else "")
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

        router = get_model_router()
        ask_model = str(getattr(settings, "LLM_TASK_ASK_MODEL", "") or "").strip()
        llm = router.llm_for_model(ask_model) if ask_model else router.llm(TaskKind.SYNTHESIS)
        # Harsh benchmark revealed /ask hanging 3+ min on slow LLM API.
        # 120s timeout with graceful 500 (not a hang) is the honest behavior.
        import asyncio as _asyncio
        generation_kwargs: dict[str, Any] = dict(
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
            max_tokens=ASK_COMPACT_MAX_TOKENS,
        )
        # Keep compatibility with lightweight test doubles and third-party
        # providers that only implement the original generate() contract.
        generate_with_metadata = getattr(llm, "generate_with_metadata", None)
        if generate_with_metadata is None:
            generated_text = await _asyncio.wait_for(llm.generate(**generation_kwargs), timeout=ASK_DEADLINE_S)
            generation = {"text": generated_text, "finish_reason": None, "usage": None}
        else:
            generation = await _asyncio.wait_for(
                generate_with_metadata(**generation_kwargs), timeout=ASK_DEADLINE_S
            )
        raw_response = str(generation.get("text") if isinstance(generation, dict) else generation or "")
        finish_reason = generation.get("finish_reason") if isinstance(generation, dict) else None
        usage = generation.get("usage") if isinstance(generation, dict) else None
        answer = _strip_thinking_blocks(raw_response)
        capped_answer = truncate_utf8(answer, settings.AGENT_ASK_OUTPUT_MAX_BYTES // 2)
        output_capped = capped_answer != answer
        answer = capped_answer
        incomplete_thinking = "<think>" in raw_response.lower() and "</think>" not in raw_response.lower()
        truncated = finish_reason in {"length", "max_tokens", "token_limit"}
        if not answer or incomplete_thinking or truncated or output_capped:
            degraded = ["incomplete_generation"]
            if incomplete_thinking:
                degraded.append("open_thinking_block")
            if truncated:
                degraded.append("generation_token_limit")
            if output_capped:
                degraded.append("answer_output_cap")
            return {
                "answer": answer or "The model did not return a complete grounded answer.",
                "status": "partial",
                "degraded": degraded,
                "usage": usage,
            }
        result = {
            "answer": answer,
            "learnings_used": [
                {"id": lrng.id, "statement": lrng.statement, "confidence": lrng.confidence} for lrng in learnings
            ],
        }
        if usage is not None:
            result["usage"] = usage
        # The key only exists when the L4-in-/ask flag is on — flag-off
        # responses are byte-identical to before the feature.
        if settings.MEMORY_SKILLS_IN_ASK:
            result["skills_used"] = [
                {"id": skill["id"], "name": skill["name"]} for skill in skills_used
            ]
        return result
    except asyncio.TimeoutError:
        return {
            "answer": "The answer exceeded the compact response deadline; the retrieved evidence was not synthesized.",
            "status": "partial",
            "degraded": ["ask_generation_deadline_exceeded"],
            "evidence_available": bool(code_results.get("files") or code_results.get("symbols") or code_results.get("chunks")),
        }
    except Exception as exc:
        # The exception type carries the diagnosis here: httpx timeouts and
        # asyncio cancellations both stringify to "", so the old message was
        # literally "Query failed: " — a 500 with nothing to act on. The same
        # blind spot hid a latency problem in the MCP proxy for weeks.
        logger.exception(f"/ask failed for repo={body.repo_path}: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Query failed due to an internal error") from exc


@router.post("/context", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def build_task_context(body: ContextRequest, request: Request = cast(Request, None)):
    from apps.api.request_observation import classify_result, observe_http_request
    started = time.perf_counter()
    outcome = "error"
    result = None
    repo_path = _agent_repo_path(body.repo_path)
    try:
        # persist=false never creates a durable context pack. Payload-free request
        # measurements are independent of that artifact persistence contract.
        if not body.persist:
            try:
                result = await RuntimeContextBuilder().build(
                    body.task_description, repo_path,
                    max_tokens=body.max_tokens, include_debug=body.include_debug,
                )
                outcome = classify_result(result, "context")
                return result
            except Exception as exc:
                logger.warning("/context v2 degraded: {}", type(exc).__name__)
                return BudgetedPayloadBuilder(
                    min(settings.AGENT_RUNTIME_CONTEXT_MAX_BYTES, body.max_tokens * 4),
                    metadata={"status": "partial", "missing": [f"context_error:{type(exc).__name__}"]},
                ).build()
        repo_path = str(resolve_repo_path(body.repo_path))
        result = await ContextPackBuilder().build_context_pack(body.task_description, Path(repo_path))
        outcome = classify_result(result, "context")
        return result
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception as exc:
        logger.error("Context pack failed: {}", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Context pack compilation failed due to an internal error") from exc
    finally:
        if request is not None:
            await observe_http_request(request, operation="context", result=result, repo_path=repo_path,
                                       outcome=outcome, started=started)


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
    from apps.api.request_observation import classify_result, observe_http_request
    started = time.perf_counter()
    outcome = "error"
    result = None
    try:
        mode = _agent_v2_mode()
        if mode == "on" and body.response_mode == "locator":
            result = await _compact_locator(body)
            outcome = classify_result(result, "search")
            return result
        if mode == "shadow" and body.response_mode == "locator":
            try:
                _record_shadow_metric("search", await _compact_locator(body))
            except Exception as exc:
                logger.warning("/search v2 shadow failed open: {}", type(exc).__name__)
        search_kwargs: dict[str, Any] = {"limit": body.limit, "repo_path": body.repo_path}
        request_id = _late_interaction_request_id(request)
        if request_id:
            search_kwargs["request_id"] = request_id
        result = await hybrid_search_code(body.query, **search_kwargs)
        outcome = classify_result(result, "search")
        return result
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception as exc:
        outcome = "error"
        logger.error(f"Search failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Search failed due to an internal error") from exc
    finally:
        await observe_http_request(request, operation="search", result=result,
                                   repo_path=_agent_repo_path(body.repo_path) if _agent_v2_mode() == "on" and body.response_mode == "locator" else body.repo_path,
                                   outcome=outcome, started=started)


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
