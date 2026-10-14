"""MCP protocol surface shared by the local and remote Brain servers.

The tools are the product; this module is everything around them that the MCP
spec lets a server say about itself and ask of its client:

* **Tool annotations** - which tools only read, which append to durable memory,
  which reach outside the Brain. Clients use them to decide what to auto-approve,
  so one table keeps both servers telling the same story.
* **Prompts** - the multi-tool workflows (plan a change, review a diff, capture a
  lesson) that an agent otherwise has to rediscover from tool descriptions.
* **Resources** - active rules and decisions, readable without a tool call.
* **Elicitation** - an opt-in human confirmation before a durable memory write.
* **Sampling** - a fallback that lets the client's own model answer from Brain
  context when the server-side model is unavailable.

Every client-facing feature degrades to the old behaviour when the client does
not advertise the capability: a server that only works with the newest clients
is a regression for everyone else. Like ``deferred_payload``, nothing here
imports ``brain/`` - ``remote_server`` must stay installable without the core.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Awaitable, Callable

from mcp.server.mcpserver import Context
from mcp.types import (
    ClientCapabilities,
    ElicitationCapability,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    CreateMessageRequest,
    CreateMessageRequestParams,
    CreateMessageResult,
    InputRequiredResult,
    SamplingCapability,
    SamplingMessage,
    TextContent,
    ToolAnnotations,
)
from pydantic import BaseModel, Field
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

RULES_URI = "brain://rules"
DECISIONS_URI = "brain://decisions"

CONFIRM_WRITES_ENV = "BRAIN_MCP_CONFIRM_WRITES"
CONFIRMATION_TIMEOUT_SECONDS = 30
SAMPLING_MAX_TOKENS = 1_500


def _annotations(
    title: str,
    *,
    read_only: bool,
    idempotent: bool = False,
    open_world: bool = False,
) -> ToolAnnotations:
    # Nothing the Brain exposes deletes or overwrites user data: memory writes
    # are append-only (superseded, never erased) and jobs only add results. So
    # destructiveHint is False across the board rather than left to the spec's
    # pessimistic default of True for non-read-only tools.
    return ToolAnnotations(
        title=title,
        read_only_hint=read_only,
        destructive_hint=None if read_only else False,
        idempotent_hint=idempotent if not read_only else None,
        open_world_hint=open_world,
    )


TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    "ask_project": _annotations("Ask the project", read_only=True),
    "search_code": _annotations("Locate code", read_only=True),
    "prepare_task_context": _annotations("Prepare task context", read_only=True),
    "find_related_files": _annotations("Find related files", read_only=True),
    "impact_analysis": _annotations("Analyze change impact", read_only=True),
    "review_diff": _annotations("Review a diff", read_only=True),
    "get_diff_review": _annotations("Read a diff review", read_only=True),
    "get_background_job": _annotations("Read a background job", read_only=True),
    # Queues work and, with notify=True, messages the operator outside the
    # Brain - the one tool whose effects leave the closed world.
    "prepare_deep_context": _annotations("Queue deep context", read_only=False, open_world=True),
    # Decisions are keyed by title, so a repeat call lands on the same row.
    "record_decision": _annotations("Record a decision", read_only=False, idempotent=True),
    "record_rule": _annotations("Record a rule", read_only=False),
    "record_task_lesson": _annotations("Record a task lesson", read_only=False, idempotent=True),
    "record_task_observation": _annotations("Record a task observation", read_only=False, idempotent=True),
    "record_procedure_outcome": _annotations("Record a skill outcome", read_only=False, idempotent=True),
}


def tool_meta(name: str) -> dict[str, Any]:
    """Keyword arguments for ``@mcp.tool`` so every tool carries its annotations."""
    annotations = TOOL_ANNOTATIONS[name]
    return {"title": annotations.title, "annotations": annotations}


# --- client capabilities -----------------------------------------------------


def _client_supports(ctx: Context | None, capability: ClientCapabilities) -> bool:
    if ctx is None:
        return False
    try:
        return ctx.session.check_client_capability(capability)
    except Exception:  # noqa: BLE001 - no live session (direct call, tests)
        return False


def supports_elicitation(ctx: Context | None) -> bool:
    return _client_supports(ctx, ClientCapabilities(elicitation=ElicitationCapability()))


def supports_sampling(ctx: Context | None) -> bool:
    return _client_supports(ctx, ClientCapabilities(sampling=SamplingCapability()))


# --- elicitation ---------------------------------------------------------------


class WriteConfirmation(BaseModel):
    approve: bool = Field(description="Store this in the Brain's shared, long-lived memory?")
    note: str = Field(default="", description="Optional correction or reason, kept with the decision.")


def confirm_writes_enabled() -> bool:
    """Opt-in, because most Brain clients are autonomous agents.

    Rules and decisions steer every later agent on the repository, so a team
    may want a human to sign each one off. But an unattended agent whose client
    supports elicitation would stall on the prompt, so asking is the operator's
    call, made where the client launches (or the API runs) the server.
    """
    return os.environ.get(CONFIRM_WRITES_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


async def confirm_memory_write(ctx: Context | None, summary: str) -> dict[str, Any] | InputRequiredResult | None:
    """Ask the human to approve a durable write.

    Returns ``None`` to proceed, or the tool result to return instead when the
    human declined. Without opt-in or client support the write goes ahead
    exactly as before - confirmation is an addition, never a new failure mode.
    """
    if not confirm_writes_enabled() or not supports_elicitation(ctx):
        return None
    assert ctx is not None
    if ctx.protocol_version in MODERN_PROTOCOL_VERSIONS:
        # Modern MCP carries interaction as a retry result, without a backchannel.
        response = (ctx.input_responses or {}).get("confirm-memory-write")
        if response is None:
            return InputRequiredResult(input_requests={
                "confirm-memory-write": ElicitRequest(params=ElicitRequestFormParams(
                    message=summary, requested_schema=WriteConfirmation.model_json_schema(),
                )),
            })
        if not isinstance(response, ElicitResult):
            return {"status": "cancelled", "reason": "Invalid memory write confirmation.", "note": ""}
        try:
            data = WriteConfirmation.model_validate(response.content) if response.action == "accept" else None
        except (ValueError, TypeError):
            return {"status": "cancelled", "reason": "Invalid memory write confirmation.", "note": ""}
        if data is not None and data.approve:
            return None
        return {
            "status": "cancelled",
            "reason": f"The user {'dismissed' if response.action == 'cancel' else 'declined'} this memory write.",
            "note": data.note if data is not None else "",
        }
    try:
        result = await asyncio.wait_for(
            ctx.elicit(message=summary, schema=WriteConfirmation),
            timeout=CONFIRMATION_TIMEOUT_SECONDS,
        )
        if result.action == "accept" and not isinstance(result.data, WriteConfirmation):
            return {"status": "cancelled", "reason": "Invalid memory write confirmation.", "note": ""}
    except Exception:  # noqa: BLE001 - opted-in confirmation must fail closed
        return {"status": "cancelled", "reason": "Memory write confirmation failed.", "note": ""}
    if result.action == "accept" and result.data.approve:
        return None
    note = result.data.note if result.action == "accept" else ""
    return {
        "status": "cancelled",
        "reason": f"The user {'declined' if result.action != 'cancel' else 'dismissed'} this memory write.",
        "note": note,
    }


# --- sampling ------------------------------------------------------------------


async def sample_answer(ctx: Context | None, prompt: str, *, system: str) -> str | InputRequiredResult | None:
    """Answer with the client's model, or ``None`` when the client cannot sample.

    Used only as a fallback: the server-side model sees the same context and
    keeps costs on the operator's key, but when it is down an answer from the
    agent's own model beats an error string.
    """
    if not supports_sampling(ctx):
        return None
    assert ctx is not None
    if ctx.protocol_version in MODERN_PROTOCOL_VERSIONS:
        response = (ctx.input_responses or {}).get("sample-grounded-answer")
        if response is None:
            return InputRequiredResult(input_requests={
                "sample-grounded-answer": CreateMessageRequest(params=CreateMessageRequestParams(
                    messages=[SamplingMessage(role="user", content=TextContent(type="text", text=prompt))],
                    max_tokens=SAMPLING_MAX_TOKENS, system_prompt=system,
                )),
            })
        if not isinstance(response, CreateMessageResult):
            return None
        return response.content.text if isinstance(response.content, TextContent) else None
    try:
        result = await ctx.session.create_message(
            messages=[SamplingMessage(role="user", content=TextContent(type="text", text=prompt))],
            max_tokens=SAMPLING_MAX_TOKENS,
            system_prompt=system,
        )
    except Exception:  # noqa: BLE001 - the caller already has an error to report
        return None
    content = result.content
    return content.text if isinstance(content, TextContent) else None


# --- resources -----------------------------------------------------------------


def register_memory_resources(
    mcp: Any,
    *,
    list_rules: Callable[[], Awaitable[list[dict[str, Any]]]],
    list_decisions: Callable[[], Awaitable[list[dict[str, Any]]]],
) -> None:
    """Expose active rules and decisions as resources.

    They are context an agent should load before editing, not a question it
    asks - which is exactly the line the spec draws between resources and tools.
    """

    @mcp.resource(RULES_URI, name="active_rules", title="Active rules", mime_type="application/json")
    async def active_rules() -> str:
        """Normative rules recorded for the indexed repositories."""
        return json.dumps(await list_rules(), ensure_ascii=False, default=str)

    @mcp.resource(DECISIONS_URI, name="active_decisions", title="Active decisions", mime_type="application/json")
    async def active_decisions() -> str:
        """Architectural decisions currently in force."""
        return json.dumps(await list_decisions(), ensure_ascii=False, default=str)


def only_active(rows: Any) -> list[dict[str, Any]]:
    """Keep active rows from an API list response; anything else becomes an error row."""
    if not isinstance(rows, list):
        return [rows] if isinstance(rows, dict) else []
    return [row for row in rows if isinstance(row, dict) and (row.get("status") or "active").lower() == "active"]


# --- prompts -------------------------------------------------------------------


def register_prompts(mcp: Any) -> None:
    """Workflows that chain several tools; clients surface them as slash commands."""

    @mcp.prompt(name="plan_change", title="Plan a change with the Brain")
    def plan_change(change: str, repo_path: str = "") -> str:
        """Locate, scope and plan a code change before editing anything."""
        repo = f' (repo_path="{repo_path}")' if repo_path else ""
        return (
            f"Plan this change{repo} before editing any file:\n\n{change}\n\n"
            "1. Call search_code to locate the code involved; read only the files it points at.\n"
            "2. Call impact_analysis with the change to see what else it reaches.\n"
            f"3. Read the {RULES_URI} and {DECISIONS_URI} resources and treat them as constraints.\n"
            "4. If the locator was not enough, call prepare_task_context for bounded code slices.\n"
            "5. Reply with: files to change, the risks impact_analysis raised, which rules or "
            "decisions apply, and the tests that will prove the change."
        )

    @mcp.prompt(name="review_changes", title="Review changes against the Brain")
    def review_changes(base: str = "", repo_path: str = "") -> str:
        """Review the working diff against recorded rules and decisions."""
        args = ", ".join(
            part for part in (f'base="{base}"' if base else "", f'repo_path="{repo_path}"' if repo_path else "") if part
        )
        return (
            f"Review the current changes: call review_diff({args}).\n"
            "If it returns a job_id instead of a report, poll get_diff_review(job_id) until it completes.\n"
            f"Check the findings against the {RULES_URI} and {DECISIONS_URI} resources.\n"
            "Report blocking problems first, each with file and line, then risks, then nits. "
            "Say plainly when nothing blocks the change."
        )

    @mcp.prompt(name="capture_lesson", title="Capture a lesson in Brain memory")
    def capture_lesson(task_id: str, what_happened: str) -> str:
        """Turn what a task taught into L1 memory evidence."""
        return (
            f"Task {task_id} produced this outcome:\n\n{what_happened}\n\n"
            "State the reusable lesson in one sentence that would still be true for a different task.\n"
            "If a check you ran and an artifact it produced prove it, call record_task_lesson with "
            "their validation_id and artifact_ids. Otherwise call record_task_observation - weaker "
            "evidence that consolidation needs to see more than once.\n"
            f"Use an idempotency_key derived from the task and the lesson (for example "
            f'"{task_id}:<short-slug>") so a retry does not record it twice.'
        )
