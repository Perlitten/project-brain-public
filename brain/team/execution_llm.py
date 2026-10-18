"""LLM provider consent and budget for bounded execution (M2; docs/operations/TEAM_EXECUTION.md).

An execution that may send repository content to an external LLM provider
declares it: ``llm: {"provider", "model", "max_tokens", "max_cost_cents"?}``.
The provider must be configured by the operator (``BRAIN_EXECUTION_LLM_PROVIDERS``:
its hosts and, optionally, the models it allows). Egress to a host that belongs
to an LLM provider (a configured provider's host or a well-known LLM API host)
is denied unless the execution declares that provider.

In team state a declared provider also needs

- a live consent a team administrator recorded for the project, the provider
  and the model (or ``*``), revocable at any time (the next checkpoint of a
  running execution then revokes it), and
- a project LLM budget (fail closed: no budget, no LLM use). Brain cannot meter
  tokens inside the step's TLS connection, so the declared ``max_tokens`` /
  ``max_cost_cents`` are reserved in full against the budget when the
  execution is requested, per project and optionally per member.

In solo state (a team of one) the owner consents implicitly; a budget is
enforced only if the owner set one.

This module raises ``LlmDenied`` (a ``TeamDenied``); ``brain.team.execution``
turns it into an ``ExecutionDenied`` whose message callers see.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.team.activity import record_event
from brain.team.authorization import TeamConflict, TeamDenied, fresh, require_team_storage, team_state
from brain.team.models import AgentRun, AgentRunExecution, TeamLlmBudget, TeamLlmConsent, TeamProject, TeamTaskBinding

# Public LLM API hosts. Egress to one of them is treated as sending content to an LLM provider even when no
# provider names it, so an egress allowlist cannot be used to bypass consent.
KNOWN_LLM_HOSTS = frozenset({
    "api.openai.com", "api.anthropic.com", "generativelanguage.googleapis.com", "aiplatform.googleapis.com",
    "api.mistral.ai", "api.groq.com", "api.together.xyz", "openrouter.ai", "api.deepseek.com", "api.cohere.com",
    "api.cohere.ai", "integrate.api.nvidia.com", "api.x.ai", "api.fireworks.ai", "api.perplexity.ai",
})
KNOWN_LLM_HOST_SUFFIXES = (".openai.azure.com", "-aiplatform.googleapis.com")
PERIODS = ("month", "total")
ANY_MODEL = "*"
LLM_KEYS = frozenset({"provider", "model", "max_tokens", "max_cost_cents"})
_PROVIDER = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,127}$")
_HOST = re.compile(r"^[a-z0-9.-]{1,253}$")
_PRINCIPAL_ACTOR = re.compile(r"^principal:(\d+)$")
MAX_LIMIT = 2**31 - 1


class LlmDenied(TeamDenied):
    """An LLM consent or budget gate refused; the message is safe to show to the requester."""


@dataclass(frozen=True)
class LlmProvider:
    name: str
    hosts: frozenset[str]
    models: frozenset[str]  # empty: any model


@dataclass(frozen=True)
class LlmCeiling:
    """What a run's parents allow: unbounded for a top-level run, else the narrowed parent declaration."""
    bounded: bool = False
    cap: Mapping | None = None  # None while bounded: a parent declared no LLM use, so the child may not


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def host_name(entry: str) -> str:
    """``host[:port]`` -> ``host`` (lowercase)."""
    return entry.strip().lower().rsplit(":", 1)[0] if ":" in entry else entry.strip().lower()


def parse_providers(raw: str | None) -> dict[str, LlmProvider]:
    """``BRAIN_EXECUTION_LLM_PROVIDERS``: JSON name -> {"hosts": [...], "models": [...] (optional)}."""
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        raise ValueError("BRAIN_EXECUTION_LLM_PROVIDERS must be a JSON object") from None
    if not isinstance(data, dict):
        raise ValueError("BRAIN_EXECUTION_LLM_PROVIDERS must be a JSON object")
    providers: dict[str, LlmProvider] = {}
    for name, spec in data.items():
        if not _PROVIDER.match(str(name)) or not isinstance(spec, dict) or set(spec) - {"hosts", "models"}:
            raise ValueError(f"invalid LLM provider: {name}")
        hosts, models = spec.get("hosts"), spec.get("models") or []
        if (not isinstance(hosts, list) or not hosts
                or not all(isinstance(host, str) and _HOST.match(host.strip().lower()) for host in hosts)):
            raise ValueError(f"LLM provider {name} needs a list of host names")
        if not isinstance(models, list) or not all(isinstance(m, str) and _MODEL.match(m) for m in models):
            raise ValueError(f"LLM provider {name} has an invalid model list")
        providers[name] = LlmProvider(name, frozenset(host.strip().lower() for host in hosts), frozenset(models))
    return providers


def is_known_llm_host(host: str) -> bool:
    return host in KNOWN_LLM_HOSTS or host.endswith(KNOWN_LLM_HOST_SUFFIXES)


def llm_hosts(egress: Iterable[str], providers: Mapping[str, LlmProvider]) -> dict[str, set[str]]:
    """Egress entries that reach an LLM provider -> the configured providers owning that host (may be empty)."""
    found: dict[str, set[str]] = {}
    for entry in egress:
        host = host_name(entry)
        owners = {provider.name for provider in providers.values() if host in provider.hosts}
        if owners or is_known_llm_host(host):
            found[entry] = owners
    return found


def normalize_llm(value: object) -> dict | None:
    """Shape check of a request's ``llm`` declaration; raises ``ValueError`` with a caller-facing message."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - LLM_KEYS:
        raise ValueError(f"The LLM declaration keys are {', '.join(sorted(LLM_KEYS))}")
    provider, model = value.get("provider"), value.get("model")
    if not isinstance(provider, str) or not _PROVIDER.match(provider):
        raise ValueError("The LLM provider is a lowercase name")
    if not isinstance(model, str) or not _MODEL.match(model) or model == ANY_MODEL:
        raise ValueError("The LLM declaration names one model")
    out: dict = {"provider": provider, "model": model}
    tokens = value.get("max_tokens")
    if not isinstance(tokens, int) or isinstance(tokens, bool) or not 0 < tokens <= MAX_LIMIT:
        raise ValueError("The LLM declaration needs a positive max_tokens")
    out["max_tokens"] = tokens
    cost = value.get("max_cost_cents")
    if cost is not None:
        if not isinstance(cost, int) or isinstance(cost, bool) or not 0 <= cost <= MAX_LIMIT:
            raise ValueError("max_cost_cents must be a non-negative integer")
        out["max_cost_cents"] = cost
    return out


def narrow(ceiling: LlmCeiling, parent: Mapping | None) -> LlmCeiling:
    """Fold one parent's declaration into the ceiling (walking from the immediate parent up)."""
    if parent is None or (ceiling.bounded and ceiling.cap is None):
        return LlmCeiling(True, None)
    if not ceiling.bounded or ceiling.cap is None:
        return LlmCeiling(True, dict(parent))
    cap = dict(ceiling.cap)
    if (cap["provider"], cap["model"]) != (parent.get("provider"), parent.get("model")):
        return LlmCeiling(True, None)
    cap["max_tokens"] = min(int(cap["max_tokens"]), int(parent["max_tokens"]))
    costs = [int(c) for c in (cap.get("max_cost_cents"), parent.get("max_cost_cents")) if c is not None]
    if costs:
        cap["max_cost_cents"] = min(costs)
    return LlmCeiling(True, cap)


def _period_start(period: str, now: datetime) -> datetime | None:
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc) if period == "month" else None


async def usage(session: AsyncSession, project_id: uuid.UUID, period: str, *, member_id: int | None = None,
                exclude_run: uuid.UUID | None = None, now: datetime | None = None) -> dict:
    """Declared LLM ceilings reserved by the project's executions in the period (optionally one member's).

    Executions cancelled before they started reserve nothing."""
    since = _period_start(period, now or _now())
    query = (select(AgentRunExecution.run_id, AgentRunExecution.llm, AgentRunExecution.started_at,
                    AgentRunExecution.released_at, AgentRunExecution.created_at, AgentRun.initiator_id)
             .join(AgentRun, AgentRun.id == AgentRunExecution.run_id)
             .join(TeamTaskBinding, TeamTaskBinding.task_id == AgentRun.task_id)
             .where(TeamTaskBinding.project_id == project_id, AgentRunExecution.llm.is_not(None)))
    if since is not None:
        query = query.where(AgentRunExecution.created_at >= since)
    if member_id is not None:
        query = query.where(AgentRun.initiator_id == member_id)
    tokens = cost = count = 0
    for run_id, llm, started_at, released_at, _created, _initiator in (await session.execute(query)).all():
        if not isinstance(llm, dict) or run_id == exclude_run or (released_at is not None and started_at is None):
            continue
        count += 1
        tokens += int(llm.get("max_tokens") or 0)
        cost += int(llm.get("max_cost_cents") or 0)
    return {"period": period, "since": _iso(since), "tokens": tokens, "cost_cents": cost, "executions": count}


async def check(session: AsyncSession, *, project: TeamProject, run: AgentRun, llm: Mapping | None,
                egress: Iterable[str], ceiling: LlmCeiling, providers: Mapping[str, LlmProvider]) -> None:
    """The LLM gate of ``authorize_execution``; raises ``LlmDenied``."""
    reached = llm_hosts(egress, providers)
    if llm is None:
        if reached:
            raise LlmDenied(f"Egress to an LLM provider needs a declared LLM provider: {sorted(reached)[0]}")
        return
    provider = providers.get(str(llm["provider"]))
    if provider is None:
        raise LlmDenied(f"LLM provider not configured on this Brain: {llm['provider']}")
    if provider.models and llm["model"] not in provider.models:
        raise LlmDenied(f"LLM model not allowed for provider {provider.name}: {llm['model']}")
    foreign = sorted(entry for entry, owners in reached.items() if provider.name not in owners)
    if foreign:
        raise LlmDenied(f"Egress destination belongs to another LLM provider: {foreign[0]}")
    if ceiling.bounded:
        cap = ceiling.cap
        if cap is None or (cap["provider"], cap["model"]) != (llm["provider"], llm["model"]):
            raise LlmDenied("LLM use not allowed for this run: a parent run declares no such LLM use")
        if int(llm["max_tokens"]) > int(cap["max_tokens"]):
            raise LlmDenied("LLM max_tokens exceeds the parent run's declaration")
        if cap.get("max_cost_cents") is not None and (
                llm.get("max_cost_cents") is None or int(llm["max_cost_cents"]) > int(cap["max_cost_cents"])):
            raise LlmDenied("LLM max_cost_cents exceeds the parent run's declaration")
    solo = await team_state(session, project.team_id) == "solo"
    if not solo:
        consent = (await session.execute(select(TeamLlmConsent).where(
            TeamLlmConsent.project_id == project.id, TeamLlmConsent.provider == provider.name,
            TeamLlmConsent.model.in_([str(llm["model"]), ANY_MODEL]), TeamLlmConsent.revoked_at.is_(None))
            .with_for_update(read=True))).scalars().first()
        if consent is None:
            raise LlmDenied(f"No team administrator consent for sending repository content to {provider.name} "
                            f"({llm['model']}) in this project")
    budget = await fresh(session, TeamLlmBudget, TeamLlmBudget.project_id == project.id)
    if budget is None:
        if solo:
            return  # solo owner without a budget: implicit consent, no limit
        raise LlmDenied("This project has no LLM budget; a team administrator sets one before executions may use "
                        "an LLM provider")
    await _check_budget(session, budget, project.id, run, llm)


async def _check_budget(session: AsyncSession, budget: TeamLlmBudget, project_id: uuid.UUID, run: AgentRun,
                        llm: Mapping) -> None:
    tokens, cost = int(llm["max_tokens"]), llm.get("max_cost_cents")
    if (budget.max_cost_cents is not None or budget.per_member_max_cost_cents is not None) and cost is None:
        raise LlmDenied("This project's LLM budget limits cost; declare max_cost_cents")
    cost = int(cost or 0)
    scopes: list[tuple[str, int | None, int | None, int | None]] = [
        ("project", None, budget.max_tokens, budget.max_cost_cents)]
    if budget.per_member_max_tokens is not None or budget.per_member_max_cost_cents is not None:
        scopes.append(("member", run.initiator_id, budget.per_member_max_tokens, budget.per_member_max_cost_cents))
    for scope, member_id, max_tokens, max_cost in scopes:
        if max_tokens is None and max_cost is None:
            continue
        used = await usage(session, project_id, budget.period, member_id=member_id, exclude_run=run.id)
        window = "this month" if budget.period == "month" else "in total"
        if max_tokens is not None and used["tokens"] + tokens > max_tokens:
            raise LlmDenied(f"LLM token budget exceeded for this {scope}: {used['tokens']} of {max_tokens} tokens "
                            f"already reserved {window}, this execution declares {tokens}")
        if max_cost is not None and used["cost_cents"] + cost > max_cost:
            raise LlmDenied(f"LLM cost budget exceeded for this {scope}: {used['cost_cents']} of {max_cost} cents "
                            f"already reserved {window}, this execution declares {cost}")


# --------------------------------------------------------------------------- consent and budget administration


def consent_dict(row: TeamLlmConsent) -> dict:
    return {"project_id": str(row.project_id), "provider": row.provider, "model": row.model,
            "consented_by": row.consented_by, "consented_at": _iso(row.consented_at),
            "revoked_at": _iso(row.revoked_at), "revoked_by": row.revoked_by, "version": row.version,
            "active": row.revoked_at is None}


def budget_dict(row: TeamLlmBudget | None) -> dict | None:
    if row is None:
        return None
    return {"project_id": str(row.project_id), "period": row.period, "max_tokens": row.max_tokens,
            "max_cost_cents": row.max_cost_cents, "per_member_max_tokens": row.per_member_max_tokens,
            "per_member_max_cost_cents": row.per_member_max_cost_cents, "set_by": row.set_by,
            "set_at": _iso(row.set_at), "version": row.version}


async def _project(session: AsyncSession, project_id: uuid.UUID) -> TeamProject:
    from brain.team.provisioning import TeamNotFound

    project = await fresh(session, TeamProject, TeamProject.id == project_id)
    if project is None:
        raise TeamNotFound("Project not found")
    return project


async def _activity(session: AsyncSession, kind: str, summary: str, project_id: uuid.UUID, by: str,
                    details: dict) -> None:
    match = _PRINCIPAL_ACTOR.match(by)
    actor_id = int(match.group(1)) if match else None
    await record_event(session, kind=kind, summary=summary, audience="admin", actor_principal_id=actor_id,
                       actor_label=None if actor_id else "operator", project_id=project_id, details=details)


async def set_consent(session: AsyncSession, project_id: uuid.UUID, provider: str, model: str, by: str, *,
                      providers: Mapping[str, LlmProvider]) -> TeamLlmConsent:
    """Record (or renew) consent for one operator-configured provider and model (``*``: any allowed model)."""
    require_team_storage()
    await _project(session, project_id)
    configured = providers.get(provider)
    if configured is None:
        raise TeamConflict(f"LLM provider not configured on this Brain: {provider}")
    if model != ANY_MODEL and (not _MODEL.match(model) or (configured.models and model not in configured.models)):
        raise TeamConflict(f"LLM model not allowed for provider {provider}: {model}")
    row = (await session.execute(select(TeamLlmConsent).where(
        TeamLlmConsent.project_id == project_id, TeamLlmConsent.provider == provider, TeamLlmConsent.model == model)
        .with_for_update())).scalar_one_or_none()
    if row is None:
        row = TeamLlmConsent(project_id=project_id, provider=provider, model=model, consented_by=by[:128],
                             consented_at=_now(), version=1)
        session.add(row)
    elif row.revoked_at is not None:
        row.revoked_at, row.revoked_by = None, None
        row.consented_by, row.consented_at = by[:128], _now()
        row.version += 1
    else:
        return row
    await session.flush()
    await _activity(session, "execution.llm_consented", f"LLM consent recorded for {provider} ({model})",
                    project_id, by, {"provider": provider, "model": model, "version": row.version})
    return row


async def revoke_consent(session: AsyncSession, project_id: uuid.UUID, provider: str, model: str,
                         by: str) -> TeamLlmConsent:
    from brain.team.provisioning import TeamNotFound

    require_team_storage()
    row = (await session.execute(select(TeamLlmConsent).where(
        TeamLlmConsent.project_id == project_id, TeamLlmConsent.provider == provider, TeamLlmConsent.model == model)
        .with_for_update())).scalar_one_or_none()
    if row is None:
        raise TeamNotFound("LLM consent not found")
    if row.revoked_at is None:
        row.revoked_at, row.revoked_by = _now(), by[:128]
        row.version += 1
        await session.flush()
        await _activity(session, "execution.llm_consent_revoked", f"LLM consent revoked for {provider} ({model})",
                        project_id, by, {"provider": provider, "model": model, "version": row.version})
    return row


async def list_consents(session: AsyncSession, project_id: uuid.UUID) -> list[dict]:
    rows = (await session.execute(select(TeamLlmConsent).where(TeamLlmConsent.project_id == project_id)
                                  .order_by(TeamLlmConsent.provider, TeamLlmConsent.model))).scalars().all()
    return [consent_dict(row) for row in rows]


def _limit(value: object, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_LIMIT:
        raise TeamConflict(f"{name} must be a non-negative integer")
    return value


async def set_budget(session: AsyncSession, project_id: uuid.UUID, by: str, *, period: str = "month",
                     max_tokens: int | None = None, max_cost_cents: int | None = None,
                     per_member_max_tokens: int | None = None,
                     per_member_max_cost_cents: int | None = None) -> TeamLlmBudget:
    """Set the project's LLM budget (``None`` limits are unlimited)."""
    require_team_storage()
    await _project(session, project_id)
    if period not in PERIODS:
        raise TeamConflict(f"The budget period is one of {', '.join(PERIODS)}")
    limits = {"max_tokens": _limit(max_tokens, "max_tokens"),
              "max_cost_cents": _limit(max_cost_cents, "max_cost_cents"),
              "per_member_max_tokens": _limit(per_member_max_tokens, "per_member_max_tokens"),
              "per_member_max_cost_cents": _limit(per_member_max_cost_cents, "per_member_max_cost_cents")}
    row = await fresh(session, TeamLlmBudget, TeamLlmBudget.project_id == project_id, exclusive=True)
    if row is None:
        row = TeamLlmBudget(project_id=project_id, period=period, set_by=by[:128], set_at=_now(), version=1,
                            **limits)
        session.add(row)
    else:
        row.period, row.set_by, row.set_at = period, by[:128], _now()
        for key, value in limits.items():
            setattr(row, key, value)
        row.version += 1
    await session.flush()
    await _activity(session, "execution.llm_budget_set", "LLM budget set", project_id, by,
                    {"period": period, **limits, "version": row.version})
    return row


async def clear_budget(session: AsyncSession, project_id: uuid.UUID, by: str) -> bool:
    """Remove the budget. In team state that stops new LLM use in the project (fail closed)."""
    require_team_storage()
    row = await fresh(session, TeamLlmBudget, TeamLlmBudget.project_id == project_id, exclusive=True)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    await _activity(session, "execution.llm_budget_cleared", "LLM budget cleared", project_id, by, {})
    return True


async def budget_report(session: AsyncSession, project_id: uuid.UUID) -> dict:
    project = await _project(session, project_id)
    row = await fresh(session, TeamLlmBudget, TeamLlmBudget.project_id == project_id)
    state = await team_state(session, project.team_id)
    return {"project_id": str(project_id), "state": state, "budget": budget_dict(row),
            "usage": await usage(session, project_id, row.period if row else "month"),
            "consent_required": state != "solo", "budget_required": state != "solo"}


async def lock_budget(session: AsyncSession, project_id: uuid.UUID) -> None:
    """Serialise concurrent reservations against one project's budget (held until the transaction ends)."""
    await fresh(session, TeamLlmBudget, TeamLlmBudget.project_id == project_id, exclusive=True)
