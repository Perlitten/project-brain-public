"""Operator policy and request authorization: tools/secrets/egress/budget ceilings."""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping
from sqlalchemy.ext.asyncio import AsyncSession
from brain.auth.principals import ResolvedPrincipal
from brain.config.settings import settings
from brain.execution.bounded import (ExecutorBackend, ExecutorError, SubprocessBackend, ToolSpec, validate_args, valid_report_path)
from brain.execution.container import (ContainerBackend, ContainerSettings, Environment, default_user, valid_cpus, valid_image, valid_memory, valid_network, valid_user)
from brain.team import execution_llm
from brain.team.authorization import Authority, Permission, authorize, fresh, permissions, require_team_storage
from brain.team.execution_llm import LlmCeiling, LlmDenied, LlmProvider
from brain.team.models import AgentRun, AgentRunExecution, ProjectMember, TeamExecutionGrant, TeamProject
from brain.team.execution._common import ExecutionDenied


EXECUTION_ROLES = frozenset({"contributor", "reviewer"})


BUDGET_KEYS = ("wall_seconds", "steps", "step_timeout_seconds", "output_bytes")


BACKENDS = ("subprocess", "container")


_ENVIRONMENT_KEYS = frozenset({"image", "tools", "cpus", "memory", "pids"})


MAX_STEPS_PER_REQUEST = 100


MAX_DELEGATION_DEPTH = 32


_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


_REF = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


_HOST = re.compile(r"^[a-z0-9.-]{1,253}(:[0-9]{1,5})?$")


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
    llm_providers: dict[str, LlmProvider] = field(default_factory=dict)

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
            report = spec.get("report")
            if report is not None and not valid_report_path(report):
                raise ValueError("invalid tool report path")
            tools[name] = ToolSpec(name=name, argv=tuple(argv), allow_args=bool(spec.get("allow_args", False)),
                                   report=report)
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
    llm_providers: dict[str, LlmProvider] = {}
    if error is None:
        try:
            if backend not in BACKENDS:
                raise ValueError(f"BRAIN_EXECUTION_BACKEND must be one of {', '.join(BACKENDS)}")
            environments = _parse_environments(settings.BRAIN_EXECUTION_ENVIRONMENTS, tools)
            container = _container_settings()
            llm_providers = execution_llm.parse_providers(settings.BRAIN_EXECUTION_LLM_PROVIDERS)
        except ValueError as exc:
            error, environments, llm_providers = f"Execution is misconfigured: {exc}", {}, {}
    return ExecutionPolicy(
        enabled=bool(settings.BRAIN_EXECUTION_ENABLED), root=root, tools=tools, secrets=secrets, egress=egress,
        max_wall_seconds=max(1, int(settings.BRAIN_EXECUTION_MAX_WALL_SECONDS)),
        max_steps=max(1, int(settings.BRAIN_EXECUTION_MAX_STEPS)),
        step_timeout_seconds=max(1, int(settings.BRAIN_EXECUTION_STEP_TIMEOUT_SECONDS)),
        max_output_bytes=max(1024, int(settings.BRAIN_EXECUTION_MAX_OUTPUT_BYTES)),
        lease_seconds=max(1, int(settings.BRAIN_EXECUTION_LEASE_SECONDS)), error=error,
        backend=backend if backend in BACKENDS else "subprocess", environments=environments, container=container,
        llm_providers=llm_providers)


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
    _llm_health(policy, add)
    missing = sorted(ref for ref, variable in policy.secrets.items() if not environ.get(variable))
    add("secrets", "warn" if missing else "ok",
        f"Secret references without a server value: {', '.join(missing)}" if missing
        else f"{len(policy.secrets)} secret reference(s) configured")
    order = {"ok": 0, "warn": 1, "error": 2}
    status = max((check["status"] for check in checks), key=order.__getitem__, default="ok")
    return {"status": status, "enabled": True, "backend": policy.backend,
            "environments": sorted(policy.environments), "egress_enforced": enforced, "checks": checks}


def _llm_health(policy: ExecutionPolicy, add: Callable[[str, str, str], None]) -> None:
    providers = policy.llm_providers
    reached = execution_llm.llm_hosts(policy.egress, providers)
    unowned = sorted(entry for entry, owners in reached.items() if not owners)
    egress_hosts = {execution_llm.host_name(entry) for entry in policy.egress}
    unreachable = sorted(f"{p.name}: {host}" for p in providers.values() for host in p.hosts
                         if host not in egress_hosts)
    if unowned:
        add("llm", "warn", "BRAIN_EXECUTION_EGRESS allows LLM API hosts no configured provider declares (requests "
                           f"naming them are denied): {', '.join(unowned)}")
    elif unreachable:
        add("llm", "warn", f"LLM provider hosts missing from BRAIN_EXECUTION_EGRESS: {', '.join(unreachable)}")
    elif providers:
        add("llm", "ok", f"{len(providers)} LLM provider(s): {', '.join(sorted(providers))}; team runs need a team "
                         "administrator's consent and a project LLM budget")
    else:
        add("llm", "ok", "No LLM providers configured; egress to LLM API hosts is denied")


@dataclass(frozen=True)
class Capabilities:
    tools: frozenset[str]
    secret_refs: frozenset[str]
    egress: frozenset[str]
    budget: dict[str, int] = field(default_factory=dict)
    environment: str | None = None
    llm: dict | None = None

    @classmethod
    def from_row(cls, row: AgentRunExecution) -> Capabilities:
        return cls(frozenset(row.tools), frozenset(row.secret_refs), frozenset(row.egress), dict(row.budget),
                   row.environment, dict(row.llm) if row.llm else None)

    def as_dict(self) -> dict:
        return {"tools": sorted(self.tools), "secret_refs": sorted(self.secret_refs), "egress": sorted(self.egress),
                "budget": {key: self.budget[key] for key in BUDGET_KEYS if key in self.budget},
                "environment": self.environment, "llm": self.llm}


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
    try:
        llm = execution_llm.normalize_llm(payload.get("llm"))
    except ValueError as exc:
        raise ExecutionDenied(str(exc)) from None
    canonical = {"steps": steps, "tools": sorted(tools), "secret_refs": sorted(refs), "egress": sorted(egress),
                 "budget": dict(sorted(budget.items()))}
    if environment is not None:
        canonical["environment"] = environment  # absent when unset, so earlier request digests stay stable
    if llm is not None:
        canonical["llm"] = llm  # likewise
    return steps, Capabilities(tools, refs, egress, budget, environment, llm), canonical


async def _ceiling(session: AsyncSession, run: AgentRun,
                   policy: ExecutionPolicy) -> tuple[Capabilities, LlmCeiling]:
    """The operator ceiling narrowed by every parent run's executor capabilities (and LLM declarations)."""
    tools, refs, egress, budget = set(policy.tools), set(policy.secrets), set(policy.egress), policy.budget()
    environment: str | None = None
    llm = LlmCeiling()
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
        llm = execution_llm.narrow(llm, granted.llm)
        parent_id = parent.parent_id
    return Capabilities(frozenset(tools), frozenset(refs), frozenset(egress), budget, environment), llm


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
    return Capabilities(requested.tools, requested.secret_refs, requested.egress, budget, requested.environment,
                        requested.llm)


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
    ceiling, llm_ceiling = await _ceiling(session, run, policy)
    effective = _within(capabilities, ceiling, backend, policy)
    try:
        await execution_llm.check(session, project=project, run=run, llm=effective.llm, egress=effective.egress,
                                  ceiling=llm_ceiling, providers=policy.llm_providers)
    except LlmDenied as exc:
        raise ExecutionDenied(str(exc)) from None
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
