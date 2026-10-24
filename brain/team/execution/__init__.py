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

from brain.team.execution._common import (EVENT_PREFIX as EVENT_PREFIX, TERMINAL as TERMINAL, SessionFactory as SessionFactory, ExecutionNotFound as ExecutionNotFound, ExecutionDenied as ExecutionDenied, _event as _event, _events as _events, _run_and_row as _run_and_row)
from brain.team.execution.policy import (EXECUTION_ROLES as EXECUTION_ROLES, BUDGET_KEYS as BUDGET_KEYS, BACKENDS as BACKENDS, _ENVIRONMENT_KEYS as _ENVIRONMENT_KEYS, MAX_STEPS_PER_REQUEST as MAX_STEPS_PER_REQUEST, MAX_DELEGATION_DEPTH as MAX_DELEGATION_DEPTH, _NAME as _NAME, _REF as _REF, _HOST as _HOST, ExecutionPolicy as ExecutionPolicy, operator_policy as operator_policy, _parse_environments as _parse_environments, _container_settings as _container_settings, default_backend as default_backend, execution_config_health as execution_config_health, _llm_health as _llm_health, Capabilities as Capabilities, _normalize_request as _normalize_request, _ceiling as _ceiling, _within as _within, authorize_execution as authorize_execution, _check_steps as _check_steps)
from brain.team.execution.artifacts import (ARTIFACT_KINDS as ARTIFACT_KINDS, _REDACTION_MARGIN as _REDACTION_MARGIN, _ARTIFACT_COLUMNS as _ARTIFACT_COLUMNS, ArtifactDraft as ArtifactDraft, artifact_caps as artifact_caps, redact_bytes as redact_bytes, _artifact_meta as _artifact_meta, _artifact_list as _artifact_list, _store_artifacts as _store_artifacts, _report_type as _report_type, step_artifacts as step_artifacts, _patch_artifact as _patch_artifact, list_artifacts as list_artifacts, get_artifact as get_artifact)
from brain.team.execution.state import (derive_state as derive_state, execution_dict as execution_dict, request_execution as request_execution, get_execution as get_execution, cancel_execution as cancel_execution, execution_state as execution_state)
from brain.team.execution.grants import (grant_dict as grant_dict, set_grant as set_grant, revoke_grant as revoke_grant, _PRINCIPAL_ACTOR as _PRINCIPAL_ACTOR, _record_grant_event as _record_grant_event, list_grants as list_grants, grant_execution as grant_execution, revoke_execution as revoke_execution, list_execution_grants as list_execution_grants, list_llm_consents as list_llm_consents, consent_llm as consent_llm, revoke_llm_consent as revoke_llm_consent, llm_budget as llm_budget, set_llm_budget as set_llm_budget, clear_llm_budget as clear_llm_budget)
from brain.team.execution.lease import (worker_owner as worker_owner, Lease as Lease, acquire_lease as acquire_lease, _fence as _fence, recover_orphaned_runs as recover_orphaned_runs)
from brain.team.execution.runner import (ExecutionRunner as ExecutionRunner, _changed_paths as _changed_paths, _failure as _failure, execute_run as execute_run)

from brain.execution.bounded import ToolSpec as ToolSpec
