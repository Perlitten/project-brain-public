import sys
import asyncio
from pathlib import Path
from typing import List, Optional
from functools import wraps
from inspect import signature
from time import perf_counter
from uuid import uuid4
from loguru import logger

import mcp.types as _mcp_types
from mcp.server.fastmcp import FastMCP

from brain.config.paths import get_repo_root, resolve_repo_path
from brain.graph.graph_client import GraphClient
from brain.context.context_pack_builder import ContextPackBuilder
from brain.context.budget import BudgetedPayloadBuilder
from brain.context.runtime_context_builder import RuntimeContextBuilder
from brain.database.repository_utils import require_repository_by_path
from brain.database.session import redis_client
from brain.config.settings import settings
from brain.analyzers.impact_analyzer import ImpactAnalyzer
from brain.analyzers.diff_analyzer import DiffAnalyzer
from brain.memory.decision_store import DecisionStore
from brain.memory.repo_scope import normalize_repo_scope
from brain.memory.rule_store import RuleStore
from brain.llm.router import TaskKind, get_model_router
from brain.search.code_search import search_code as hybrid_search_code
from brain.retrieval.service import RetrievalService
from brain.workers.queue import WORKER_POOLS, JobQueue, queue_for_job, worker_pool_prefix

PROJECT_ROOT = get_repo_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

ASK_RETRIEVAL_LIMIT = 10

mcp = FastMCP("project-brain-mcp-server")


def _mcp_outcome(result: object, operation: str) -> str:
    from apps.api.request_observation import classify_result
    return classify_result(result, operation)


def _repository_path_from_result(result: object, requested: object, operation: str = "context") -> str | None:
    if isinstance(result, dict):
        scope = result.get("repo") or result.get("repository_scope")
        if isinstance(scope, dict) and isinstance(scope.get("path"), str):
            return scope["path"]
        if isinstance(scope, dict) and isinstance(scope.get("repository_path"), str):
            return scope["repository_path"]
        repository = result.get("repository")
        if isinstance(repository, dict):
            for key in ("repository_path", "requested_path", "path"):
                if isinstance(repository.get(key), str) and repository[key]:
                    return repository[key]
    return str(requested) if requested else str(PROJECT_ROOT) if operation == "context" else None


def _mcp_telemetry(operation: str):
    """Record direct MCP attempts without changing tool signatures or failures."""
    def decorate(function):
        original_signature = signature(function)

        @wraps(function)
        async def wrapped(*args, **kwargs):
            bound = original_signature.bind_partial(*args, **kwargs)
            requested = bound.arguments.get("repo_path")
            request_id = str(uuid4())
            started = perf_counter()
            result = None
            outcome = "failed"
            try:
                result = await function(*args, **kwargs)
                outcome = _mcp_outcome(result, operation)
                return result
            except BaseException:
                outcome = "failed"
                raise
            finally:
                latency_ms = (perf_counter() - started) * 1000
                try:
                    from apps.api.telemetry import record_request, repository_id_for_path

                    repository_path = _repository_path_from_result(result, requested, operation)
                    async def persist():
                        repository_id = await repository_id_for_path(repository_path)
                        await record_request(operation=operation, surface="mcp", repository_id=repository_id,
                                             repository_path=repository_path,
                                             principal_name="mcp:unattributed", request_id=request_id,
                                             outcome=outcome, latency_ms=latency_ms)
                    await asyncio.wait_for(persist(), timeout=0.5)
                except BaseException:
                    # Observability must never break an MCP tool or mask its error.
                    pass

        return wrapped
    return decorate


def _record_external_client(client_info: Optional[_mcp_types.Implementation]) -> None:
    """Persist proof that a real MCP client connected — the setup checklist
    only claims 'external client connected' from this observation."""
    try:
        from brain.onboarding.setup_state import record_client_activity

        record_client_activity(
            getattr(client_info, "name", None) or "unknown-client",
            getattr(client_info, "version", "") or "",
        )
    except Exception:
        pass  # telemetry must never break a client handshake


def _install_client_activity_recording() -> None:
    """Wrap ServerSession._received_request so each initialize handshake is
    recorded. The SDK handles initialize inside the session (not via
    request_handlers), so this is the only reliable observation point; if the
    SDK internals move, the hook degrades to a no-op."""
    try:
        from mcp.server import session as _mcp_session

        original = getattr(_mcp_session.ServerSession, "_received_request", None)
        if original is None:
            return

        async def _recording_received(self, responder):
            if isinstance(responder.request.root, _mcp_types.InitializeRequest):
                _record_external_client(responder.request.root.params.clientInfo)
            return await original(self, responder)

        _mcp_session.ServerSession._received_request = _recording_received  # type: ignore[method-assign]
    except Exception:
        pass


_install_client_activity_recording()


@mcp.tool()
async def ask_project(query: str, repo_path: Optional[str] = None) -> str:
    """Asks the project LLM assistant a question about the project code,

    leveraging database code searches and active rules.
    """
    if settings.BRAIN_AGENT_CONTEXT_V2_MODE == "on":
        from apps.api.routers.core import _ask_v2
        from apps.api.schemas import AskRequest

        result = await _ask_v2(AskRequest(query=query, repo_path=repo_path))
        return str(result.get("answer") or result)
    try:
        code_results = await hybrid_search_code(query, limit=ASK_RETRIEVAL_LIMIT, repo_path=repo_path)

        repository_scope = code_results.get("repository_scope") or {}
        rule_scope = repository_scope.get("repository_path") if repository_scope.get("found") else repo_path
        active_rules = await RuleStore.list_active_rules(rule_scope)
        rules_str = "\n".join([f"- {r.id}: {r.name} - {r.description}" for r in active_rules])
        try:
            decision_scope = normalize_repo_scope(rule_scope)
            decisions = await DecisionStore.list_decisions()
            active_decisions = [
                decision
                for decision in decisions
                if (decision.status or "").lower() == "active"
                and (decision.repo_path is None or normalize_repo_scope(decision.repo_path) == decision_scope)
            ]
            recorded_decisions_str = (
                "\n".join([f"- {d.id}: {d.title} - {d.description}" for d in active_decisions]) or "None"
            )
        except Exception as exc:
            logger.warning(f"Decision retrieval skipped for MCP ask_project: {exc}")
            recorded_decisions_str = "Unavailable"

        files_str = "\n".join([f"- {f['path']}: {f['summary']}" for f in code_results["files"]])
        symbols_str = "\n".join([f"- {s['name']} ({s['kind']}): {s['summary']}" for s in code_results["symbols"]])
        chunks_str = "\n\n".join(
            [f"--- Chunk (similarity: {c['similarity']:.2f}) ---\n{c['content']}" for c in code_results["chunks"]]
        )

        context = (
            f"### Relevant Files:\n{files_str or 'None'}\n\n"
            f"### Relevant Symbols:\n{symbols_str or 'None'}\n\n"
            f"### Relevant Code Chunks:\n{chunks_str or 'None'}\n\n"
            f"### Recorded Decisions:\n{recorded_decisions_str}\n\n"
            f"### Active Rules:\n{rules_str or 'None'}"
        )

        prompt = (
            f"You are the technical assistant for Project Brain.\n"
            f"Answer the following user query about the project codebase:\n"
            f'"""\n{query}\n"""\n\n'
            f"Using the gathered project context:\n{context}\n\n"
            f"Provide a thorough, direct, and technically accurate response."
        )

        llm = get_model_router().llm(TaskKind.SYNTHESIS)
        response = await llm.generate(
            prompt=prompt, system_instruction="You are an expert software developer working on Project Brain."
        )
        return response
    except Exception as e:
        return f"Error answering project query: {str(e)}"


@mcp.tool()
@_mcp_telemetry("context")
async def prepare_task_context(task_description: str, repo_path: Optional[str] = None) -> dict:
    """Returns bounded runtime slices; durable packs are an explicit deep job."""
    if settings.BRAIN_AGENT_CONTEXT_V2_MODE == "on":
        return await RuntimeContextBuilder().build(task_description, repo_path or PROJECT_ROOT)
    try:
        target_path = Path(repo_path).resolve() if repo_path else PROJECT_ROOT
        builder = ContextPackBuilder()
        result = await builder.build_context_pack(task_description, target_path)
        return result
    except Exception as e:
        return {"error": f"Failed to prepare task context: {str(e)}", "status": "failed"}


@mcp.tool()
async def prepare_deep_context(
    task_description: str,
    repo_path: Optional[str] = None,
    notify: bool = True,
) -> dict:
    """Queue a quality-first context pack using exact CPU LFM reranking."""
    if not settings.LATE_INTERACTION_DEEP_ENABLED:
        return {"status": "failed", "error": "LFM deep lane is disabled"}
    queue = queue_for_job(
        redis_client,
        settings.WORKER_REDIS_PREFIX,
        "deep_context",
        pools_enabled=settings.BRAIN_WORKER_POOLS_V2_ENABLED,
    )
    job_id = await queue.enqueue(
        "deep_context",
        {
            "task_description": task_description,
            "repo_path": repo_path or settings.TARGET_REPO_PATH,
            "notify": notify,
        },
        max_attempts=1,
        timeout_seconds=settings.WORKER_MAX_JOB_TIMEOUT_SECONDS,
    )
    return {
        "job_id": job_id,
        "status": "queued",
        "status_url": f"/jobs/{job_id}",
    }


@mcp.tool()
async def get_background_job(job_id: str) -> dict:
    """Read the status and result of an asynchronous Brain job."""
    queues = [JobQueue(redis_client, prefix=settings.WORKER_REDIS_PREFIX)]
    if settings.BRAIN_WORKER_POOLS_V2_ENABLED:
        # Jobs created before cutover remain readable from the legacy queue;
        # new jobs can be in any v2 pool, so status lookup must not assume the
        # caller remembers the routing class.
        queues = [
            JobQueue(
                redis_client,
                prefix=worker_pool_prefix(settings.WORKER_REDIS_PREFIX, pool, enabled=True),
            )
            for pool in WORKER_POOLS
        ] + queues
    for queue in queues:
        job = await queue.get_job(job_id)
        if job is not None:
            if settings.BRAIN_WORKER_POOLS_V2_ENABLED:
                job["pool"] = queue.prefix.rsplit(":", 1)[-1]
            return job
    return {"status": "failed", "error": "Job not found"}


@mcp.tool()
async def impact_analysis(change_request: str, repo_path: Optional[str] = None) -> dict:
    """Analyzes affected components and estimates risks for a given change request."""
    try:
        analyzer = ImpactAnalyzer(resolve_repo_path(repo_path))
        if settings.BRAIN_AGENT_CONTEXT_V2_MODE == "on":
            return await analyzer.analyze_bounded(change_request)
        result = await analyzer.analyze_impact(change_request)
        return result
    except Exception as e:
        return {"error": f"Failed to perform impact analysis: {str(e)}", "status": "failed"}


@mcp.tool()
async def record_decision(
    title: str,
    repo_path: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = "active",
    reason: Optional[str] = None,
    consequences: Optional[str] = None,
    affected_features: Optional[List[str]] = None,
    affected_modules: Optional[List[str]] = None,
    affected_files: Optional[List[str]] = None,
) -> dict:
    """Records an architectural or design decision to the database."""
    try:
        decision_id = await DecisionStore.add_decision(
            title=title,
            repo_path=repo_path,
            description=description,
            status=status,
            reason=reason,
            consequences=consequences,
            affected_features=affected_features,
            affected_modules=affected_modules,
            affected_files=affected_files,
        )
        return {
            "status": "success",
            "decision_id": decision_id,
            "message": f"Successfully recorded decision '{title}' with ID {decision_id}",
        }
    except Exception as e:
        return {"error": f"Failed to record decision: {str(e)}", "status": "failed"}


@mcp.tool()
async def record_rule(
    name: str,
    repo_path: Optional[str] = None,
    description: Optional[str] = None,
    type: Optional[str] = "architecture",
    severity: Optional[str] = "medium",
    status: Optional[str] = "active",
    applies_to: Optional[dict] = None,
    rule_id: Optional[str] = None,
) -> dict:
    """Record a repository-scoped normative rule in the database."""
    try:
        recorded_id = await RuleStore.add_rule(
            name=name,
            repo_path=repo_path,
            description=description,
            type=type,
            severity=severity,
            status=status,
            applies_to=applies_to,
            rule_id=rule_id,
        )
        return {
            "status": "success",
            "rule_id": recorded_id,
            "message": f"Successfully recorded rule '{name}' with ID {recorded_id}",
        }
    except Exception as e:
        return {"error": f"Failed to record rule: {str(e)}", "status": "failed"}


@mcp.tool()
@_mcp_telemetry("search")
async def search_code(query: str, repo_path: Optional[str] = None) -> dict:
    """Compact paths/symbols/ranges locator; known files can be read locally."""
    try:
        if settings.BRAIN_AGENT_CONTEXT_V2_MODE == "on":
            result = await RetrievalService().retrieve(
                query,
                repo_path or PROJECT_ROOT,
                intent="locator",
                candidate_budget=5,
                deadline_s=settings.AGENT_RETRIEVAL_DEADLINE_S,
            )
            freshness = (result.repository.get("freshness") or {}).get("status") or "unknown"
            return (
                BudgetedPayloadBuilder(
                    settings.AGENT_LOCATOR_MAX_BYTES,
                    metadata={"repo": {"path": result.repository.get("repository_path") or result.repository.get("requested_path"), "freshness": freshness}, "degraded": result.degraded},
                )
                .add("results", [candidate.to_locator_dict() for candidate in result.candidates])
                .build()
            )
        return await hybrid_search_code(query, limit=5, repo_path=repo_path)
    except Exception as e:
        return {"error": f"Failed to search code: {str(e)}", "status": "failed"}


@mcp.tool()
async def find_related_files(file_path: str, repo_path: Optional[str] = None) -> dict:
    """Queries the Neo4j graph for directly and indirectly related files to the target file path."""
    try:
        target_path = resolve_repo_path(repo_path)
        repository = await require_repository_by_path(target_path)
        client = GraphClient(repository_id=repository.id)
        directly = []
        indirectly = []
        visited = set()

        neighbors = await client.get_neighbors(file_path)
        for nb in neighbors:
            tgt = nb["target"]["properties"].get("name") or nb["target"]["properties"].get("path")
            tgt_labels = nb["target"]["labels"]
            if tgt and "File" in tgt_labels and tgt != file_path:
                if tgt not in visited:
                    visited.add(tgt)
                    directly.append(tgt)

        for d_file in directly:
            nb_ind = await client.get_neighbors(d_file)
            for nb in nb_ind:
                tgt = nb["target"]["properties"].get("name") or nb["target"]["properties"].get("path")
                tgt_labels = nb["target"]["labels"]
                if tgt and "File" in tgt_labels and tgt != file_path and tgt not in directly:
                    if tgt not in visited:
                        visited.add(tgt)
                        indirectly.append(tgt)

        return {"file": file_path, "directly_related_files": directly, "indirectly_related_files": indirectly}
    except Exception as e:
        return {"error": f"Failed to find related files: {str(e)}", "status": "failed"}


@mcp.tool()
async def review_diff(
    base: Optional[str] = None,
    head: Optional[str] = "current",
    repo_path: Optional[str] = None,
) -> dict:
    """Review changes; omitted base resolves from the repository's origin/HEAD."""
    try:
        analyzer = DiffAnalyzer(resolve_repo_path(repo_path) if repo_path else PROJECT_ROOT)
        result = await analyzer.review_diff(base=base, head=head)
        return result
    except Exception as e:
        return {"error": f"Failed to review diff: {str(e)}", "status": "failed"}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
