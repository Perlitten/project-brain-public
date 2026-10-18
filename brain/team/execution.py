"""Bounded execution for team runs (M2; ADR docs/adr/team-collaboration.md).

Execution is off unless the operator enables it (``BRAIN_EXECUTION_ENABLED``
plus ``BRAIN_EXECUTION_ROOT``) **and** a team administrator records an
execution grant for the run's initiator on the project. No role implies it.

A run's executor capabilities are data on ``harness.agent_run_executions``:
the tool allowlist, secret references (names; the server resolves values from
its own environment at step time and never stores or returns them), the
egress allowlist (default none) and the budget. Effective authority is the
intersection of:

- the requester's live credential, membership, project policy and task binding
  (``authorize(..., run.request, task_id)``),
- the run's own identity and its whole delegation chain
  (``authorize(run, read, task_id)``),
- the project policy allowing ``execute``,
- the initiator's Contributor/Reviewer role and live execution grant,
- the operator's tools, secrets, egress and budget ceilings, and
- every parent run's executor capabilities (a parent without any denies).

The operator picks the executor backend (``BRAIN_EXECUTION_BACKEND``). With
the container backend every execution names an operator-defined environment
(``BRAIN_EXECUTION_ENVIRONMENTS``: an image and the tools allowed in it); an
unknown environment, or a tool outside it, is denied. Egress stays denied
unless the backend can enforce the allowlist (the container backend with an
egress proxy image configured).

``authorize()`` itself is unchanged: ``Permission.EXECUTE`` stays outside every
role and outside ``RUN_CAPABILITIES``, so no other surface gains execution.

The worker re-runs that whole check (``ExecutionRunner.checkpoint``) before
every tool operation and every transactional write, and every second or so
while a step runs. A failed check requests cancellation, stops the process,
records a ``run.execution.revoked`` event that states what was in flight, and
refuses further effects. Every write also presents the lease owner and
fencing token; an owner whose lease expired or was taken over commits nothing.
``recover_orphaned_runs`` fences runs whose owner stopped renewing its lease.

M3 subtask dependencies (``brain.team.coordination``) are checked when the
run is requested, again when its execution is requested, and once more when the
worker actually starts it: a dependency that is no longer done at that point
(for example a dependency added to the plan since) revokes the execution before
any working copy exists. When an execution completes, the files it changed
(``changed_paths`` on the ``completed`` event) are compared with other active
tasks' declared scopes and live leases (``coordination.overlap``).

The outcome is derived from the ``run.execution.*`` events on the task ledger
(``harness.agent_task_events``); there is no second status column.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from brain.auth.principals import ResolvedPrincipal
from brain.config.settings import settings
from brain.database.harness_models import AgentTaskEvent
from brain.database.models import Repository
from brain.execution.bounded import (ExecutorBackend, ExecutorError, StepRequest, SubprocessBackend, ToolSpec,
                                     validate_args)
from brain.execution.container import (ContainerBackend, ContainerSettings, Environment, default_user, valid_cpus,
                                       valid_image, valid_memory, valid_network, valid_user)
from brain.team.activity import record_event
from brain.team.authorization import (Authority, Permission, TeamConflict, TeamDenied, administrator, authorize,
                                      fresh, permissions, require_team_storage)
from brain.team.models import (AgentRun, AgentRunExecution, ProjectMember, TeamExecutionGrant, TeamMember,
                               TeamProject, TeamTaskBinding)
from brain.team.store import digest

EVENT_PREFIX = "run.execution."
TERMINAL = ("completed", "failed", "revoked", "cancelled", "fenced", "budget_exhausted")
EXECUTION_ROLES = frozenset({"contributor", "reviewer"})
BUDGET_KEYS = ("wall_seconds", "steps", "step_timeout_seconds", "output_bytes")
BACKENDS = ("subprocess", "container")
_ENVIRONMENT_KEYS = frozenset({"image", "tools", "cpus", "memory", "pids"})
MAX_STEPS_PER_REQUEST = 100
MAX_DELEGATION_DEPTH = 32
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_REF = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_HOST = re.compile(r"^[a-z0-9.-]{1,253}(:[0-9]{1,5})?$")

SessionFactory = Callable[[], Any]


class ExecutionNotFound(LookupError):
    pass


class ExecutionDenied(TeamDenied):
    """An execution gate refused; the message names the gate (shown to callers already holding task access)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# --------------------------------------------------------------------------- operator policy


@dataclass(frozen=True)
class ExecutionPolicy:
    enabled: bool
    root: Path | None
    tools: dict[str, ToolSpec]
    secrets: dict[str, str]
    egress: frozenset[str]
    max_wall_seconds: int
    max_steps: int
    step_timeout_seconds: int
    max_output_bytes: int
    lease_seconds: int
    error: str | None = None
    backend: str = "subprocess"
    environments: dict[str, Environment] = field(default_factory=dict)
    container: ContainerSettings = field(default_factory=ContainerSettings)

    def disabled_reason(self) -> str | None:
        if self.error:
            return self.error
        if not self.enabled:
            return "Execution is disabled on this Brain"
        if self.root is None:
            return "Execution is enabled but BRAIN_EXECUTION_ROOT is not set"
        return None

    def budget(self) -> dict[str, int]:
        return {"wall_seconds": self.max_wall_seconds, "steps": self.max_steps,
                "step_timeout_seconds": self.step_timeout_seconds, "output_bytes": self.max_output_bytes}


def operator_policy() -> ExecutionPolicy:
    """The operator ceiling, read from settings on every call (so a change applies at the next checkpoint)."""
    error = None
    tools: dict[str, ToolSpec] = {}
    secrets: dict[str, str] = {}
    try:
        raw_tools = json.loads(settings.BRAIN_EXECUTION_TOOLS or "{}")
        raw_secrets = json.loads(settings.BRAIN_EXECUTION_SECRETS or "{}")
        if not isinstance(raw_tools, dict) or not isinstance(raw_secrets, dict):
            raise ValueError("not an object")
        for name, spec in raw_tools.items():
            argv = spec.get("argv") if isinstance(spec, dict) else None
            if (not _NAME.match(str(name)) or not isinstance(argv, list) or not argv
                    or not all(isinstance(item, str) and item for item in argv)):
                raise ValueError("invalid tool")
            tools[name] = ToolSpec(name=name, argv=tuple(argv), allow_args=bool(spec.get("allow_args", False)))
        for ref, variable in raw_secrets.items():
            if not _REF.match(str(ref)) or not isinstance(variable, str) or not variable:
                raise ValueError("invalid secret reference")
            secrets[ref] = variable
    except (ValueError, TypeError, AttributeError):
        error = "Execution is misconfigured: BRAIN_EXECUTION_TOOLS and BRAIN_EXECUTION_SECRETS must be JSON objects"
        tools, secrets = {}, {}
    egress = frozenset(host.strip().lower() for host in (settings.BRAIN_EXECUTION_EGRESS or "").split(",")
                       if host.strip())
    root = Path(settings.BRAIN_EXECUTION_ROOT).resolve() if settings.BRAIN_EXECUTION_ROOT else None
    backend = str(settings.BRAIN_EXECUTION_BACKEND or "subprocess").strip().lower()
    environments: dict[str, Environment] = {}
    container = ContainerSettings()
    if error is None:
        try:
            if backend not in BACKENDS:
                raise ValueError(f"BRAIN_EXECUTION_BACKEND must be one of {', '.join(BACKENDS)}")
            environments = _parse_environments(settings.BRAIN_EXECUTION_ENVIRONMENTS, tools)
            container = _container_settings()
        except ValueError as exc:
            error, environments = f"Execution is misconfigured: {exc}", {}
    return ExecutionPolicy(
        enabled=bool(settings.BRAIN_EXECUTION_ENABLED), root=root, tools=tools, secrets=secrets, egress=egress,
        max_wall_seconds=max(1, int(settings.BRAIN_EXECUTION_MAX_WALL_SECONDS)),
        max_steps=max(1, int(settings.BRAIN_EXECUTION_MAX_STEPS)),
        step_timeout_seconds=max(1, int(settings.BRAIN_EXECUTION_STEP_TIMEOUT_SECONDS)),
        max_output_bytes=max(1024, int(settings.BRAIN_EXECUTION_MAX_OUTPUT_BYTES)),
        lease_seconds=max(1, int(settings.BRAIN_EXECUTION_LEASE_SECONDS)), error=error,
        backend=backend if backend in BACKENDS else "subprocess", environments=environments, container=container)


def _parse_environments(raw: str | None, tools: Mapping[str, ToolSpec]) -> dict[str, Environment]:
    """``BRAIN_EXECUTION_ENVIRONMENTS``: JSON name -> {"image", "tools", optional "cpus", "memory", "pids"}."""
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        raise ValueError("BRAIN_EXECUTION_ENVIRONMENTS must be a JSON object") from None
    if not isinstance(data, dict):
        raise ValueError("BRAIN_EXECUTION_ENVIRONMENTS must be a JSON object")
    environments: dict[str, Environment] = {}
    for name, spec in data.items():
        if not _NAME.match(str(name)) or not isinstance(spec, dict) or set(spec) - _ENVIRONMENT_KEYS:
            raise ValueError(f"invalid execution environment: {name}")
        image, env_tools = spec.get("image"), spec.get("tools")
        if not isinstance(image, str) or not valid_image(image):
            raise ValueError(f"execution environment {name} needs a valid image")
        if not isinstance(env_tools, list) or not all(isinstance(tool, str) for tool in env_tools):
            raise ValueError(f"execution environment {name} needs a list of tools")
        unknown = sorted(set(env_tools) - set(tools))
        if unknown:
            raise ValueError(f"execution environment {name} names a tool missing from BRAIN_EXECUTION_TOOLS: "
                             f"{unknown[0]}")
        cpus = None if spec.get("cpus") is None else str(spec["cpus"])
        memory = None if spec.get("memory") is None else str(spec["memory"]).lower()
        pids = spec.get("pids")
        if (cpus is not None and not valid_cpus(cpus)) or (memory is not None and not valid_memory(memory)) or (
                pids is not None and (not isinstance(pids, int) or isinstance(pids, bool) or pids < 1)):
            raise ValueError(f"execution environment {name} has an invalid cpus, memory or pids limit")
        environments[name] = Environment(name=name, image=image, tools=frozenset(env_tools), cpus=cpus,
                                         memory=memory, pids=pids)
    return environments


def _container_settings() -> ContainerSettings:
    docker = str(settings.BRAIN_EXECUTION_DOCKER or "docker").strip()
    user = str(settings.BRAIN_EXECUTION_CONTAINER_USER or "").strip() or default_user()
    cpus = str(settings.BRAIN_EXECUTION_CONTAINER_CPUS).strip()
    memory = str(settings.BRAIN_EXECUTION_CONTAINER_MEMORY).strip().lower()
    tmpfs = str(settings.BRAIN_EXECUTION_CONTAINER_TMPFS).strip().lower()
    pids = int(settings.BRAIN_EXECUTION_CONTAINER_PIDS)
    proxy_image = str(settings.BRAIN_EXECUTION_EGRESS_PROXY_IMAGE or "").strip()
    upstream = str(settings.BRAIN_EXECUTION_EGRESS_UPSTREAM_NETWORK or "bridge").strip()
    if not docker:
        raise ValueError("BRAIN_EXECUTION_DOCKER is empty")
    if not valid_user(user):
        raise ValueError("BRAIN_EXECUTION_CONTAINER_USER must be a numeric, non-root uid[:gid]")
    if not valid_cpus(cpus) or not valid_memory(memory) or not valid_memory(tmpfs) or pids < 1:
        raise ValueError("invalid BRAIN_EXECUTION_CONTAINER_CPUS, _MEMORY, _TMPFS or _PIDS")
    if proxy_image and not valid_image(proxy_image):
        raise ValueError("invalid BRAIN_EXECUTION_EGRESS_PROXY_IMAGE")
    if not valid_network(upstream):
        raise ValueError("invalid BRAIN_EXECUTION_EGRESS_UPSTREAM_NETWORK")
    return ContainerSettings(docker=(docker,), user=user, cpus=cpus, memory=memory, pids=pids, tmpfs_size=tmpfs,
                             proxy_image=proxy_image, upstream_network=upstream)


def default_backend(policy: ExecutionPolicy | None = None) -> ExecutorBackend:
    policy = policy or operator_policy()
    if policy.backend == "container":
        return ContainerBackend(policy.container)
    return SubprocessBackend()


def execution_config_health(policy: ExecutionPolicy | None = None,
                            environ: Mapping[str, str] | None = None) -> dict:
    """A pure description of the execution configuration's health, for diagnostics such as ``brain doctor``.

    No docker, filesystem or database calls; secret values are never read
    beyond checking that the named variable is set."""
    policy = policy or operator_policy()
    environ = os.environ if environ is None else environ
    checks: list[dict] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    if policy.error:
        add("config", "error", policy.error)
    if not policy.enabled:
        return {"status": "error" if policy.error else "disabled", "enabled": False, "backend": policy.backend,
                "checks": checks}
    add("root", "ok" if policy.root else "error",
        str(policy.root) if policy.root else "BRAIN_EXECUTION_ROOT is not set")
    add("tools", "ok" if policy.tools else "warn",
        f"{len(policy.tools)} tool(s) allowlisted" if policy.tools else "No tools are allowlisted")
    if policy.backend == "container":
        add("backend", "ok", "Container backend: steps run in hardened containers")
        add("environments", "ok" if policy.environments else "error",
            ", ".join(sorted(policy.environments)) if policy.environments
            else "The container backend needs at least one execution environment")
    else:
        add("backend", "warn", "Subprocess backend: no filesystem or network boundary between steps and the server")
        if policy.environments:
            add("environments", "warn", "BRAIN_EXECUTION_ENVIRONMENTS is ignored by the subprocess backend")
    enforced = policy.backend == "container" and bool(policy.container.proxy_image)
    if policy.egress:
        add("egress", "ok" if enforced else "warn",
            "Egress allowlist enforced by the egress proxy" if enforced
            else "BRAIN_EXECUTION_EGRESS is set but this backend cannot enforce it; requests with egress are denied")
    else:
        add("egress", "ok", "Egress denied for every run")
    missing = sorted(ref for ref, variable in policy.secrets.items() if not environ.get(variable))
    add("secrets", "warn" if missing else "ok",
        f"Secret references without a server value: {', '.join(missing)}" if missing
        else f"{len(policy.secrets)} secret reference(s) configured")
    order = {"ok": 0, "warn": 1, "error": 2}
    status = max((check["status"] for check in checks), key=order.__getitem__, default="ok")
    return {"status": status, "enabled": True, "backend": policy.backend,
            "environments": sorted(policy.environments), "egress_enforced": enforced, "checks": checks}


# --------------------------------------------------------------------------- capabilities


@dataclass(frozen=True)
class Capabilities:
    tools: frozenset[str]
    secret_refs: frozenset[str]
    egress: frozenset[str]
    budget: dict[str, int] = field(default_factory=dict)
    environment: str | None = None

    @classmethod
    def from_row(cls, row: AgentRunExecution) -> Capabilities:
        return cls(frozenset(row.tools), frozenset(row.secret_refs), frozenset(row.egress), dict(row.budget),
                   row.environment)

    def as_dict(self) -> dict:
        return {"tools": sorted(self.tools), "secret_refs": sorted(self.secret_refs), "egress": sorted(self.egress),
                "budget": {key: self.budget[key] for key in BUDGET_KEYS if key in self.budget},
                "environment": self.environment}


def _normalize_request(payload: dict) -> tuple[list[dict], Capabilities, dict]:
    """Shape checks only; authority checks happen in ``authorize_execution``."""
    steps_in = payload.get("steps")
    if not isinstance(steps_in, list) or not steps_in or len(steps_in) > MAX_STEPS_PER_REQUEST:
        raise ExecutionDenied(f"An execution needs between 1 and {MAX_STEPS_PER_REQUEST} steps")
    steps = []
    for step in steps_in:
        tool = str(step.get("tool", "")) if isinstance(step, dict) else ""
        if not _NAME.match(tool):
            raise ExecutionDenied("Every step names an allowlisted tool")
        args = list(step.get("args") or [])
        try:
            validate_args(args)
        except ExecutorError as exc:
            raise ExecutionDenied(str(exc)) from exc
        step_refs = sorted(set(step.get("secrets") or []))
        if not all(isinstance(ref, str) and _REF.match(ref) for ref in step_refs):
            raise ExecutionDenied("Secret references are lowercase names")
        steps.append({"tool": tool, "args": args, "secrets": step_refs})
    tools = frozenset(payload.get("tools") or {step["tool"] for step in steps})
    refs = frozenset(payload.get("secret_refs") or {ref for step in steps for ref in step["secrets"]})
    egress = frozenset(str(host).strip().lower() for host in (payload.get("egress") or []))
    if not all(_NAME.match(name) for name in tools) or not all(_REF.match(ref) for ref in refs):
        raise ExecutionDenied("Invalid tool or secret reference name")
    if not all(_HOST.match(host) for host in egress):
        raise ExecutionDenied("Egress entries are host names")
    environment = payload.get("environment")
    if environment is not None and (not isinstance(environment, str) or not _NAME.match(environment)):
        raise ExecutionDenied("The execution environment is a lowercase name")
    budget_in = payload.get("budget") or {}
    if not isinstance(budget_in, dict) or set(budget_in) - set(BUDGET_KEYS):
        raise ExecutionDenied(f"Budget keys are {', '.join(BUDGET_KEYS)}")
    budget = {}
    for key, value in budget_in.items():
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ExecutionDenied(f"Budget {key} must be a positive integer")
        budget[key] = value
    canonical = {"steps": steps, "tools": sorted(tools), "secret_refs": sorted(refs), "egress": sorted(egress),
                 "budget": dict(sorted(budget.items()))}
    if environment is not None:
        canonical["environment"] = environment  # absent when unset, so earlier request digests stay stable
    return steps, Capabilities(tools, refs, egress, budget, environment), canonical


async def _ceiling(session: AsyncSession, run: AgentRun, policy: ExecutionPolicy) -> Capabilities:
    """The operator ceiling narrowed by every parent run's executor capabilities."""
    tools, refs, egress, budget = set(policy.tools), set(policy.secrets), set(policy.egress), policy.budget()
    environment: str | None = None
    parent_id, depth = run.parent_id, 0
    while parent_id is not None:
        depth += 1
        if depth > MAX_DELEGATION_DEPTH:
            raise ExecutionDenied("Invalid delegation chain")
        parent = await fresh(session, AgentRun, AgentRun.id == parent_id)
        granted = await fresh(session, AgentRunExecution, AgentRunExecution.run_id == parent_id)
        if parent is None or granted is None:
            raise ExecutionDenied("A parent run holds no executor capabilities")
        tools &= set(granted.tools)
        refs &= set(granted.secret_refs)
        egress &= set(granted.egress)
        budget = {key: min(budget[key], int(granted.budget.get(key, 0))) for key in BUDGET_KEYS}
        if granted.environment is not None:
            # A child runs in its parent's environment.
            if environment is not None and environment != granted.environment:
                raise ExecutionDenied("Invalid delegation chain")
            environment = granted.environment
        parent_id = parent.parent_id
    return Capabilities(frozenset(tools), frozenset(refs), frozenset(egress), budget, environment)


def _within(requested: Capabilities, ceiling: Capabilities, backend: ExecutorBackend,
            policy: ExecutionPolicy) -> Capabilities:
    extra = sorted(requested.tools - ceiling.tools)
    if extra:
        raise ExecutionDenied(f"Tool not allowed for this run: {extra[0]}")
    if backend.uses_environments:
        if requested.environment is None:
            raise ExecutionDenied(f"An execution environment is required with the {backend.name} backend")
        environment = policy.environments.get(requested.environment)
        if environment is None:
            raise ExecutionDenied(f"Unknown execution environment: {requested.environment}")
        if ceiling.environment is not None and requested.environment != ceiling.environment:
            raise ExecutionDenied("Execution environment not allowed for this run")
        outside = sorted(requested.tools - environment.tools)
        if outside:
            raise ExecutionDenied(f"Tool not available in environment {environment.name}: {outside[0]}")
    elif requested.environment is not None:
        raise ExecutionDenied("Execution environments need the container backend")
    if requested.secret_refs - ceiling.secret_refs:
        raise ExecutionDenied("Secret reference not allowed for this run")
    if requested.egress and not backend.enforces_egress:
        raise ExecutionDenied("Egress is not available: this executor backend cannot enforce an egress allowlist")
    if requested.egress - ceiling.egress:
        raise ExecutionDenied("Egress destination not allowed for this run")
    budget = {}
    for key in BUDGET_KEYS:
        limit = ceiling.budget.get(key, 0)
        value = requested.budget.get(key, limit)
        if limit < 1 or value > limit:
            raise ExecutionDenied(f"Budget {key} exceeds the allowed ceiling")
        budget[key] = value
    return Capabilities(requested.tools, requested.secret_refs, requested.egress, budget, requested.environment)


async def authorize_execution(session: AsyncSession, requester: ResolvedPrincipal, project_id: uuid.UUID,
                              task_id: uuid.UUID, run_id: uuid.UUID, *, policy: ExecutionPolicy,
                              backend: ExecutorBackend,
                              capabilities: Capabilities) -> tuple[AgentRun, Authority, Capabilities]:
    """Every gate, re-resolved from the database. Raises ``TeamDenied``; returns the effective capabilities."""
    require_team_storage()
    reason = policy.disabled_reason()
    if reason:
        raise ExecutionDenied(reason)
    authority = await authorize(session, requester, project_id, Permission.REQUEST_RUN, task_id=task_id)
    run = await fresh(session, AgentRun, AgentRun.id == run_id, AgentRun.task_id == task_id)
    if run is None or run.revoked_at is not None:
        raise ExecutionDenied("Run access denied")
    if run.requested_by != authority.actor_id:
        raise ExecutionDenied("Only the principal that requested the run may execute it")
    await authorize(session, ResolvedPrincipal(run.principal_id, "", "run", credential_id=run.credential_id),
                    project_id, Permission.READ, task_id=task_id)
    project = await fresh(session, TeamProject, TeamProject.id == project_id)
    if project is None or Permission.EXECUTE not in permissions(project.permissions):
        raise ExecutionDenied("Project policy does not allow execution")
    membership = await fresh(session, ProjectMember, ProjectMember.project_id == project_id,
                             ProjectMember.principal_id == run.initiator_id)
    if membership is None or membership.revoked_at is not None or membership.role not in EXECUTION_ROLES:
        raise ExecutionDenied("Execution needs a Contributor or Reviewer role on the project")
    grant = await fresh(session, TeamExecutionGrant, TeamExecutionGrant.project_id == project_id,
                        TeamExecutionGrant.principal_id == run.initiator_id)
    if grant is None or grant.revoked_at is not None:
        raise ExecutionDenied("The run's initiator holds no execution grant on this project")
    effective = _within(capabilities, await _ceiling(session, run, policy), backend, policy)
    return run, authority, effective


def _check_steps(steps: list[dict], capabilities: Capabilities, policy: ExecutionPolicy) -> None:
    if len(steps) > capabilities.budget["steps"]:
        raise ExecutionDenied("The budget allows fewer steps than requested")
    for step in steps:
        if step["tool"] not in capabilities.tools:
            raise ExecutionDenied(f"Tool not allowed for this run: {step['tool']}")
        if step["args"] and not policy.tools[step["tool"]].allow_args:
            raise ExecutionDenied(f"Tool does not accept arguments: {step['tool']}")
        if not set(step["secrets"]) <= capabilities.secret_refs:
            raise ExecutionDenied("Secret reference not allowed for this run")


# --------------------------------------------------------------------------- ledger evidence


def _event(session: AsyncSession, run: AgentRun, kind: str, key: str, actor: str, payload: dict) -> None:
    session.add(AgentTaskEvent(task_id=run.task_id, event_type=f"{EVENT_PREFIX}{kind}", actor=actor[:100],
                               idempotency_key=f"exec:{run.id}:{key}"[:255],
                               payload_json={"run_id": str(run.id), **payload}))


async def _events(session: AsyncSession, run: AgentRun) -> list[AgentTaskEvent]:
    return list((await session.execute(select(AgentTaskEvent).where(
        AgentTaskEvent.task_id == run.task_id, AgentTaskEvent.idempotency_key.like(f"exec:{run.id}:%"))
        .order_by(AgentTaskEvent.id))).scalars().all())


def derive_state(row: AgentRunExecution, events: list[AgentTaskEvent], now: datetime | None = None) -> str:
    for item in reversed(events):
        kind = item.event_type.removeprefix(EVENT_PREFIX)
        if kind in TERMINAL:
            return kind
    if row.started_at is None:
        return "requested"
    expires = row.lease_expires_at
    if row.released_at is None and expires is not None and expires > (now or _now()):
        return "running"
    return "orphaned"


async def execution_dict(session: AsyncSession, row: AgentRunExecution, run: AgentRun) -> dict:
    events = await _events(session, run)
    return {
        "run_id": str(run.id), "task_id": str(run.task_id), "state": derive_state(row, events),
        "backend": row.backend, **Capabilities.from_row(row).as_dict(), "steps": row.steps,
        "steps_recorded": row.steps_done, "fencing_token": row.fencing_token,
        "lease_expires_at": _iso(row.lease_expires_at), "started_at": _iso(row.started_at),
        "cancel_requested_at": _iso(row.cancel_requested_at), "base_sha": run.base_sha, "head_sha": run.head_sha,
        "events": [{"type": item.event_type, "actor": item.actor, "at": _iso(item.created_at),
                    "payload": item.payload_json} for item in events],
    }


# --------------------------------------------------------------------------- API-facing operations


async def request_execution(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                            task_id: uuid.UUID, run_id: uuid.UUID, payload: dict, *,
                            policy: ExecutionPolicy | None = None, backend: ExecutorBackend | None = None) -> dict:
    """Record a bounded execution for a run after every gate; idempotent on key + content + run identity."""
    policy = policy or operator_policy()
    backend = backend or default_backend(policy)
    require_team_storage()
    reason = policy.disabled_reason()
    if reason:
        raise ExecutionDenied(reason)
    steps, requested, canonical = _normalize_request(payload)
    run, authority, effective = await authorize_execution(session, caller, project_id, task_id, run_id,
                                                          policy=policy, backend=backend, capabilities=requested)
    _check_steps(steps, effective, policy)
    from brain.team import coordination  # lazy: coordination builds on brain.team.store, as this module does

    await coordination.require_dependencies_done(session, task_id)
    key = str(payload.get("idempotency_key") or "")
    if not key or len(key) > 128:
        raise ExecutionDenied("An idempotency key of at most 128 characters is required")
    fingerprint = digest({"run_id": str(run.id), "task_id": str(task_id), "backend": backend.name, **canonical})

    async def existing() -> dict | None:
        found = await fresh(session, AgentRunExecution, AgentRunExecution.run_id == run.id)
        if found is None:
            return None
        if found.request_key != key or found.request_digest != fingerprint:
            raise TeamConflict("This run already has an execution request with different content; request a new run")
        return await execution_dict(session, found, run)

    replay = await existing()
    if replay is not None:
        return replay
    try:
        async with session.begin_nested():
            row = AgentRunExecution(run_id=run.id, request_key=key, request_digest=fingerprint,
                                    requested_by=authority.actor_id, requested_credential_id=caller.credential_id,
                                    steps=steps, tools=sorted(effective.tools),
                                    secret_refs=sorted(effective.secret_refs), egress=sorted(effective.egress),
                                    budget=effective.budget, backend=backend.name,
                                    environment=effective.environment, fencing_token=0, steps_done=0)
            session.add(row)
            _event(session, run, "requested", "requested", f"principal:{authority.actor_id}",
                   {**effective.as_dict(), "backend": backend.name,
                    "steps": [{"tool": s["tool"], "args": s["args"], "secrets": s["secrets"]} for s in steps]})
            await session.flush()
    except IntegrityError:
        replay = await existing()
        if replay is None:
            raise
        return replay
    return await execution_dict(session, row, run)


async def _run_and_row(session: AsyncSession, task_id: uuid.UUID,
                       run_id: uuid.UUID) -> tuple[AgentRun, AgentRunExecution]:
    run = await fresh(session, AgentRun, AgentRun.id == run_id, AgentRun.task_id == task_id)
    row = await fresh(session, AgentRunExecution, AgentRunExecution.run_id == run_id) if run else None
    if run is None or row is None:
        raise ExecutionNotFound("Execution not found")
    return run, row


async def get_execution(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                        task_id: uuid.UUID, run_id: uuid.UUID) -> dict:
    await authorize(session, caller, project_id, Permission.READ, task_id=task_id)
    run, row = await _run_and_row(session, task_id, run_id)
    return await execution_dict(session, row, run)


async def cancel_execution(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                           task_id: uuid.UUID, run_id: uuid.UUID) -> dict:
    """Request cancellation. A queued execution is cancelled at once; a running one at its next checkpoint."""
    authority = await authorize(session, caller, project_id, Permission.READ, task_id=task_id)
    run, _ = await _run_and_row(session, task_id, run_id)
    if authority.actor_id != run.requested_by and authority.initiator_id != run.initiator_id:
        try:
            await administrator(session, caller, project_id)
        except TeamDenied as exc:
            raise ExecutionDenied("Only the requester, the initiator or a team administrator may cancel") from exc
    row = await fresh(session, AgentRunExecution, AgentRunExecution.run_id == run_id, exclusive=True)
    assert row is not None
    if derive_state(row, await _events(session, run)) in TERMINAL:
        return await execution_dict(session, row, run)
    now = _now()
    actor = f"principal:{authority.actor_id}"
    if row.cancel_requested_at is None:
        row.cancel_requested_at = now
        _event(session, run, "cancel_requested", "cancel_requested", actor, {})
    if row.started_at is None and row.released_at is None:
        row.released_at = now
        _event(session, run, "cancelled", "0:cancelled", actor,
               {"reason": "Cancelled before it started", "in_flight": None})
    await session.flush()
    return await execution_dict(session, row, run)


# --------------------------------------------------------------------------- execution grants


def grant_dict(grant: TeamExecutionGrant) -> dict:
    return {"project_id": str(grant.project_id), "principal_id": grant.principal_id, "granted_by": grant.granted_by,
            "granted_at": _iso(grant.granted_at), "revoked_at": _iso(grant.revoked_at),
            "revoked_by": grant.revoked_by, "version": grant.version, "active": grant.revoked_at is None}


async def set_grant(session: AsyncSession, project_id: uuid.UUID, principal_id: int,
                    granted_by: str) -> TeamExecutionGrant:
    """Record (or renew) an execution grant; only for a live verified member with a write-capable role."""
    from brain.team.provisioning import TeamNotFound

    require_team_storage()
    project = await fresh(session, TeamProject, TeamProject.id == project_id)
    if project is None:
        raise TeamNotFound("Project not found")
    member = await fresh(session, TeamMember, TeamMember.principal_id == principal_id,
                         TeamMember.team_id == project.team_id)
    if member is None or member.verified_at is None or member.revoked_at is not None:
        raise TeamConflict("Execution grants need a verified, live team member")
    membership = await fresh(session, ProjectMember, ProjectMember.project_id == project_id,
                             ProjectMember.principal_id == principal_id)
    if membership is None or membership.revoked_at is not None or membership.role not in EXECUTION_ROLES:
        raise TeamConflict("Execution grants need a Contributor or Reviewer role on the project")
    grant = await fresh(session, TeamExecutionGrant, TeamExecutionGrant.project_id == project_id,
                        TeamExecutionGrant.principal_id == principal_id, exclusive=True)
    if grant is None:
        grant = TeamExecutionGrant(project_id=project_id, principal_id=principal_id, granted_by=granted_by[:128],
                                   granted_at=_now(), version=1)
        session.add(grant)
    elif grant.revoked_at is not None:
        grant.revoked_at, grant.revoked_by = None, None
        grant.granted_by, grant.granted_at = granted_by[:128], _now()
        grant.version += 1
    else:
        return grant
    await session.flush()
    await _record_grant_event(session, "execution.granted", "Execution granted", grant, granted_by)
    return grant


async def revoke_grant(session: AsyncSession, project_id: uuid.UUID, principal_id: int,
                       revoked_by: str) -> TeamExecutionGrant:
    from brain.team.provisioning import TeamNotFound

    require_team_storage()
    grant = await fresh(session, TeamExecutionGrant, TeamExecutionGrant.project_id == project_id,
                        TeamExecutionGrant.principal_id == principal_id, exclusive=True)
    if grant is None:
        raise TeamNotFound("Execution grant not found")
    if grant.revoked_at is None:
        grant.revoked_at, grant.revoked_by = _now(), revoked_by[:128]
        grant.version += 1
        await session.flush()
        await _record_grant_event(session, "execution.revoked", "Execution grant revoked", grant, revoked_by)
    return grant


_PRINCIPAL_ACTOR = re.compile(r"^principal:(\d+)$")


async def _record_grant_event(session: AsyncSession, kind: str, summary: str, grant: TeamExecutionGrant,
                              by: str) -> None:
    """Team-administration activity for an effective grant change (``by`` is ``principal:<id>`` or an operator).

    Grants a membership or project-role revocation withdraws are not logged
    here; that revocation's own ``membership``/``project_role`` event covers it."""
    match = _PRINCIPAL_ACTOR.match(by)
    actor_id = int(match.group(1)) if match else None
    await record_event(session, kind=kind, summary=summary, audience="admin", actor_principal_id=actor_id,
                       actor_label=None if actor_id else "operator", subject_principal_id=grant.principal_id,
                       project_id=grant.project_id,
                       details={"principal_id": grant.principal_id, "version": grant.version})


async def list_grants(session: AsyncSession, project_id: uuid.UUID) -> list[dict]:
    rows = (await session.execute(select(TeamExecutionGrant).where(TeamExecutionGrant.project_id == project_id)
                                  .order_by(TeamExecutionGrant.principal_id))).scalars().all()
    return [grant_dict(row) for row in rows]


async def grant_execution(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                          principal_id: int) -> dict:
    await administrator(session, caller, project_id)
    return grant_dict(await set_grant(session, project_id, principal_id, f"principal:{caller.id}"))


async def revoke_execution(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID,
                           principal_id: int) -> dict:
    await administrator(session, caller, project_id)
    return grant_dict(await revoke_grant(session, project_id, principal_id, f"principal:{caller.id}"))


async def list_execution_grants(session: AsyncSession, caller: ResolvedPrincipal, project_id: uuid.UUID) -> list[dict]:
    await administrator(session, caller, project_id)
    return await list_grants(session, project_id)


# --------------------------------------------------------------------------- lease, fencing, recovery


def worker_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"[:128]


@dataclass(frozen=True)
class Lease:
    run_id: uuid.UUID
    owner: str
    token: int


async def acquire_lease(factory: SessionFactory, run_id: uuid.UUID, owner: str, *, lease_seconds: int) -> Lease | None:
    """Start the run's single execution. ``None`` when it already started, finished or was cancelled."""
    async with factory() as session, session.begin():
        now = _now()
        token = (await session.execute(update(AgentRunExecution).where(
            AgentRunExecution.run_id == run_id, AgentRunExecution.started_at.is_(None),
            AgentRunExecution.released_at.is_(None))
            .values(lease_owner=owner[:128], fencing_token=AgentRunExecution.fencing_token + 1,
                    lease_expires_at=now + timedelta(seconds=lease_seconds), heartbeat_at=now, started_at=now)
            .returning(AgentRunExecution.fencing_token))).scalar_one_or_none()
    return Lease(run_id, owner[:128], int(token)) if token is not None else None


async def _fence(session: AsyncSession, lease: Lease, lease_seconds: int) -> AgentRunExecution | None:
    """Compare-and-renew: the row only if this owner still holds this token on an unexpired, unreleased lease."""
    now = _now()
    held = (await session.execute(update(AgentRunExecution).where(
        AgentRunExecution.run_id == lease.run_id, AgentRunExecution.lease_owner == lease.owner,
        AgentRunExecution.fencing_token == lease.token, AgentRunExecution.released_at.is_(None),
        AgentRunExecution.lease_expires_at > now)
        .values(heartbeat_at=now, lease_expires_at=now + timedelta(seconds=lease_seconds))
        .returning(AgentRunExecution.run_id))).scalar_one_or_none()
    if held is None:
        return None
    return await fresh(session, AgentRunExecution, AgentRunExecution.run_id == lease.run_id)


async def recover_orphaned_runs(factory: SessionFactory, *,
                                reaper: Callable[[str], None] | None = None) -> list[str]:
    """Fence every started execution whose lease expired unreleased (owner crashed, hung or lost the database).

    The token is bumped and the lease released, so the old owner can commit
    nothing more; the run is not resumed (request a new run instead). Containers
    and networks of fenced container-backend runs are removed (``reaper``,
    default: the configured container backend's ``reap``)."""
    fenced: list[str] = []
    containers: list[str] = []
    async with factory() as session, session.begin():
        now = _now()
        rows = (await session.execute(select(AgentRunExecution).where(
            AgentRunExecution.released_at.is_(None), AgentRunExecution.started_at.is_not(None),
            AgentRunExecution.lease_expires_at <= now))).scalars().all()
        for row in rows:
            # Capture before the UPDATE: the ORM synchronises the loaded row with the new values.
            previous_owner, steps_done, steps = row.lease_owner, row.steps_done, list(row.steps)
            changed = (await session.execute(update(AgentRunExecution).where(
                AgentRunExecution.run_id == row.run_id, AgentRunExecution.fencing_token == row.fencing_token,
                AgentRunExecution.released_at.is_(None))
                .values(fencing_token=row.fencing_token + 1, released_at=now, lease_owner=None)
                .returning(AgentRunExecution.fencing_token))).scalar_one_or_none()
            if changed is None:
                continue
            run = await fresh(session, AgentRun, AgentRun.id == row.run_id)
            if run is None:
                continue
            in_flight = None
            if steps_done < len(steps):
                in_flight = {"step": steps_done, "tool": steps[steps_done]["tool"], "state": "unknown",
                             "note": "The owner stopped renewing its lease; this step may have run partially. Its "
                                     "effects in the working copy are not recorded and network effects cannot be "
                                     "recalled."}
            _event(session, run, "fenced", f"{changed}:fenced", "system:execution-recovery",
                   {"reason": "The lease expired without renewal", "previous_owner": previous_owner,
                    "fencing_token": changed, "steps_recorded": steps_done, "in_flight": in_flight})
            fenced.append(str(row.run_id))
            if row.backend == ContainerBackend.backend_name:
                containers.append(str(row.run_id))
    if fenced:
        logger.warning("Fenced {} orphaned team execution(s): {}", len(fenced), ", ".join(fenced))
    if containers:
        if reaper is None:
            policy = operator_policy()
            reaper = ContainerBackend(policy.container).reap if not policy.error else None
        for run_id in containers:
            if reaper is not None:
                await asyncio.to_thread(reaper, run_id)
    return fenced


@dataclass
class ExecutionRunner:
    """One owner's handle on one execution: checkpoints, evidence writes and the terminal record."""
    factory: SessionFactory
    lease: Lease
    project_id: uuid.UUID
    task_id: uuid.UUID
    policy: ExecutionPolicy
    backend: ExecutorBackend
    stopped: tuple[str, str] | None = None

    @property
    def actor(self) -> str:
        return f"run:{self.lease.run_id}"

    async def _deny_reason(self, session: AsyncSession, row: AgentRunExecution, *,
                           starting: bool = False) -> str | None:
        if row.backend != self.backend.name:
            return "The executor backend changed since the request"
        requester = ResolvedPrincipal(row.requested_by, "", "", credential_id=row.requested_credential_id)
        try:
            await authorize_execution(session, requester, self.project_id, self.task_id, self.lease.run_id,
                                      policy=self.policy, backend=self.backend,
                                      capabilities=Capabilities.from_row(row))
        except TeamDenied as exc:
            return str(exc)
        if starting:
            from brain.team import coordination  # lazy: see request_execution

            try:
                await coordination.require_dependencies_done(session, self.task_id)
            except TeamConflict as exc:
                return str(exc)
        return None

    async def checkpoint(self, write: Callable[[AsyncSession, AgentRunExecution, AgentRun], Awaitable[None]]
                         | None = None, *, starting: bool = False) -> tuple[str, str]:
        """Fence, re-authorize, check cancellation and budget, then run ``write`` in the same transaction.

        ``starting`` also re-checks the task's M3 dependencies (the checkpoint
        before the first effect). Returns ``("ok", "")`` or ``(outcome, reason)``.
        On revocation the cancellation request is committed even though nothing
        else is."""
        async with self.factory() as session, session.begin():
            row = await _fence(session, self.lease, self.policy.lease_seconds)
            if row is None:
                return "fenced", "The lease was lost; this owner may not commit results"
            if row.cancel_requested_at is not None:
                return "cancelled", "Cancellation was requested"
            reason = await self._deny_reason(session, row, starting=starting)
            if reason is not None:
                row.cancel_requested_at = _now()
                return "revoked", reason
            started = row.started_at or _now()
            # The wall budget gates new effects; recording what already happened stays allowed.
            if write is None and (_now() - started).total_seconds() >= int(row.budget["wall_seconds"]):
                return "budget_exhausted", "The wall-time budget is exhausted"
            if write is not None:
                run = await fresh(session, AgentRun, AgentRun.id == self.lease.run_id)
                assert run is not None
                await write(session, row, run)
            return "ok", ""

    async def probe(self) -> str | None:
        """Called while a step runs: renews the lease and stops the step on any failed check."""
        outcome, reason = await self.checkpoint()
        if outcome == "ok":
            return None
        self.stopped = (outcome, reason)
        return outcome

    async def conclude(self, outcome: str, payload: dict) -> bool:
        """Write the terminal event and release the lease; a fenced owner writes nothing and gets ``False``."""
        async with self.factory() as session, session.begin():
            row = await _fence(session, self.lease, self.policy.lease_seconds)
            if row is None:
                return False
            run = await fresh(session, AgentRun, AgentRun.id == self.lease.run_id)
            assert run is not None
            now = _now()
            if outcome in ("revoked", "cancelled") and row.cancel_requested_at is None:
                row.cancel_requested_at = now
            row.released_at = now
            _event(session, run, outcome, f"{self.lease.token}:{outcome}", self.actor,
                   {"fencing_token": self.lease.token, **payload})
            return True


async def execution_state(factory: SessionFactory, run_id: uuid.UUID) -> dict:
    async with factory() as session:
        row = await fresh(session, AgentRunExecution, AgentRunExecution.run_id == run_id)
        run = await fresh(session, AgentRun, AgentRun.id == run_id)
        if row is None or run is None:
            return {"run_id": str(run_id), "state": "missing"}
        return await execution_dict(session, row, run)


async def _changed_paths(backend: ExecutorBackend, copy: Any) -> list[str] | None:
    """Files the run changed against its base (committed, uncommitted and untracked); ``None`` if unreadable.

    Overlap detection is advisory, so a backend that cannot list changes does
    not fail an otherwise completed run."""
    lister = getattr(backend, "changed_paths", None)
    if lister is None:
        return None
    try:
        return list(await asyncio.to_thread(lister, copy))
    except ExecutorError as exc:
        logger.warning("Could not list changed files for run {}: {}", copy.run_dir.name, exc)
        return None


def _failure(outcome_evidence: dict) -> str:
    if outcome_evidence.get("error"):
        return str(outcome_evidence["error"])
    if outcome_evidence.get("timed_out"):
        return "The step exceeded its timeout"
    return f"The step exited with code {outcome_evidence.get('exit_code')}"


async def execute_run(run_id: uuid.UUID, *, owner: str | None = None, factory: SessionFactory | None = None,
                      policy: ExecutionPolicy | None = None, backend: ExecutorBackend | None = None) -> dict:
    """Run a requested execution to a terminal event (worker entry point). Never raises on a denial."""
    if factory is None:
        from brain.database.session import async_session_factory
        factory = async_session_factory
    policy = policy or operator_policy()
    backend = backend or default_backend(policy)
    owner = owner or worker_owner()
    await recover_orphaned_runs(factory, reaper=getattr(backend, "reap", None))
    async with factory() as session:
        row = await fresh(session, AgentRunExecution, AgentRunExecution.run_id == run_id)
        run = await fresh(session, AgentRun, AgentRun.id == run_id)
        binding = await fresh(session, TeamTaskBinding, TeamTaskBinding.task_id == run.task_id) if run else None
        repository = (await fresh(session, Repository, Repository.id == binding.repository_id)) if binding else None
        if row is None or run is None or binding is None or repository is None:
            return {"run_id": str(run_id), "state": "missing"}
        steps = list(row.steps)
        environment_name, egress = row.environment, tuple(row.egress)
        source, base_sha, branch = Path(repository.path), run.base_sha, run.branch
        project_id, task_id = binding.project_id, run.task_id
    lease = await acquire_lease(factory, run_id, owner, lease_seconds=policy.lease_seconds)
    if lease is None:
        return await execution_state(factory, run_id)
    runner = ExecutionRunner(factory, lease, project_id, task_id, policy, backend)

    async def stop(outcome: str, reason: str, in_flight: dict | None, recorded: int) -> dict:
        if outcome != "fenced":
            await runner.conclude(outcome, {"reason": reason, "steps_recorded": recorded, "in_flight": in_flight})
        return await execution_state(factory, run_id)

    # Every gate (including a known environment for this backend and the task's dependencies) before the first
    # docker call.
    outcome, reason = await runner.checkpoint(starting=True)
    if outcome != "ok":
        return await stop(outcome, reason, None, 0)
    try:
        resolved = await asyncio.to_thread(backend.resolve_environment,
                                           policy.environments.get(environment_name) if environment_name else None)
    except ExecutorError as exc:
        await runner.conclude("failed", {"reason": str(exc), "phase": "environment", "steps_recorded": 0,
                                         "in_flight": None})
        return await execution_state(factory, run_id)
    environment_evidence = resolved.evidence() if hasattr(resolved, "evidence") else None

    async def started(session: AsyncSession, _row: AgentRunExecution, current: AgentRun) -> None:
        _event(session, current, "started", f"{lease.token}:started", runner.actor,
               {"owner": lease.owner, "fencing_token": lease.token, "backend": backend.name, "base_sha": base_sha,
                "branch": branch, "environment": environment_evidence,
                "egress": {"profile": "allowlist" if egress else "none", "hosts": list(egress)}})

    outcome, reason = await runner.checkpoint(started)
    if outcome != "ok":
        return await stop(outcome, reason, None, 0)
    try:
        assert policy.root is not None
        copy = await asyncio.to_thread(backend.prepare, policy.root, str(run_id), source, base_sha, branch)
    except ExecutorError as exc:
        await runner.conclude("failed", {"reason": str(exc), "phase": "prepare", "steps_recorded": 0,
                                         "in_flight": None})
        return await execution_state(factory, run_id)
    try:
        for index, step in enumerate(steps):
            outcome, reason = await runner.checkpoint()
            if outcome != "ok":
                return await stop(outcome, reason, None, index)
            tool = policy.tools.get(step["tool"])
            if tool is None:
                return await stop("revoked", "Tool is no longer allowlisted by the operator", None, index)
            missing = [ref for ref in step["secrets"]
                       if ref not in policy.secrets or not os.environ.get(policy.secrets[ref])]
            if missing:
                await runner.conclude("failed", {"reason": "A secret reference is not configured on the server",
                                                 "failed_step": index, "steps_recorded": index, "in_flight": None})
                return await execution_state(factory, run_id)
            secrets = {ref: os.environ[policy.secrets[ref]] for ref in step["secrets"]}
            async with factory() as session:
                current = await fresh(session, AgentRunExecution, AgentRunExecution.run_id == run_id)
                assert current is not None and current.started_at is not None
                budget = dict(current.budget)
                elapsed = (_now() - current.started_at).total_seconds()
            remaining = max(1.0, float(budget["wall_seconds"]) - elapsed)
            request = StepRequest(index=index, tool=tool, args=tuple(step["args"]), secrets=secrets,
                                  timeout_seconds=min(float(budget["step_timeout_seconds"]), remaining),
                                  output_cap=int(budget["output_bytes"]),
                                  check_interval=max(0.2, min(5.0, policy.lease_seconds / 3)),
                                  environment=resolved, egress=egress)
            result = await backend.run_step(copy, request, runner.probe)
            evidence = result.evidence()
            if result.stopped:
                stopped_outcome, stopped_reason = runner.stopped or (result.stopped, "Stopped")
                return await stop(stopped_outcome, stopped_reason,
                                  {"step": index, "tool": tool.name, "state": "terminated", "evidence": evidence,
                                   "note": "The process was stopped; changes it made in the working copy before "
                                           "that are not undone, and network effects cannot be recalled."}, index)

            async def record(session: AsyncSession, current_row: AgentRunExecution, current: AgentRun,
                             evidence: dict = evidence, index: int = index) -> None:
                current_row.steps_done = index + 1
                _event(session, current, "step", f"{lease.token}:step:{index}", runner.actor, evidence)

            outcome, reason = await runner.checkpoint(record)
            if outcome != "ok":
                return await stop(outcome, reason, {"step": index, "tool": tool.name, "state": "finished_unrecorded",
                                                    "evidence": evidence}, index)
            if result.exit_code != 0 or result.timed_out or result.error:
                await runner.conclude("failed", {"reason": _failure(evidence), "failed_step": index,
                                                 "steps_recorded": index + 1, "in_flight": None})
                return await execution_state(factory, run_id)
        head_sha, dirty = await asyncio.to_thread(backend.head, copy)
        changed_paths = await _changed_paths(backend, copy)

        async def complete(session: AsyncSession, current_row: AgentRunExecution, current: AgentRun) -> None:
            from brain.team import coordination  # lazy: see request_execution

            now = _now()
            current.head_sha, current.worktree_path = head_sha, str(copy.path)
            current_row.released_at = now
            overlaps = await coordination.record_change_overlaps(session, current.task_id, changed_paths,
                                                                 actor_id=current.requested_by)
            _event(session, current, "completed", f"{lease.token}:completed", runner.actor,
                   {"fencing_token": lease.token, "base_sha": base_sha, "head_sha": head_sha, "dirty": dirty,
                    "worktree_path": str(copy.path), "steps_recorded": len(steps),
                    "changed_paths": coordination.changed_scope(changed_paths) if changed_paths is not None else None,
                    "changed_paths_total": len(changed_paths) if changed_paths is not None else None,
                    "overlaps": len(overlaps)})

        outcome, reason = await runner.checkpoint(complete)
        if outcome != "ok":
            return await stop(outcome, reason, {"state": "finished_unrecorded", "head_sha": head_sha}, len(steps))
    except ExecutorError as exc:
        await runner.conclude("failed", {"reason": str(exc), "in_flight": None})
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised for the worker's own failure handling
        await runner.conclude("failed", {"reason": f"Internal executor error: {type(exc).__name__}",
                                         "in_flight": None})
        raise
    finally:
        await asyncio.to_thread(backend.release, copy)
    return await execution_state(factory, run_id)
