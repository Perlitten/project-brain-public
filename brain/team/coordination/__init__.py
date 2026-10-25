"""Team coordination (M3; ADR docs/adr/team-collaboration.md, "Revocation, concurrency and approval").

Five pieces, all on top of the M1 task/run model and the central ``authorize()``:

* **Subtasks and a coordinator.** A subtask is an ordinary team task whose
  ``team_task_plans`` row names its parent. ``depends_on`` lists sibling
  subtasks; the dependency graph is kept acyclic on every write, and
  ``brain.team.store.request_run`` refuses a run while a dependency is not done
  (``require_dependencies_done``). The bounded executor checks again when the
  execution is requested and when it actually starts (``brain.team.execution``). ``plan_view`` derives each subtask's state
  and the ready set in topological order; ``dispatch`` requests runs for it.
* **Overlap detection.** Declared scope paths of active tasks in one
  repository, and live path leases, are compared by path prefix (a glob counts
  from its static prefix). An overlap is a warning in the response and a
  ``coordination.overlap`` row in the activity feed. When a bounded execution
  completes, the files it actually changed are compared the same way
  (``record_change_overlaps``). Declared scopes that share no path but are
  linked by an import edge in the repository graph give a weaker
  ``dependency`` warning; when the graph is missing or stale the check
  degrades to paths only and the response says so (``dependency_graph``).
  It flags risk only: same files can be independent and separate files can
  still conflict.
* **Instruction resolution.** Each new run records the canonical, active
  decision and rule versions that applied to its repository
  (``team_run_resolutions``). Integration flags a run whose instructions were
  edited, superseded or retired since.
* **Critical leases.** Exclusive, expiring leases on repository paths or named
  resources with fencing tokens (``team_resource_leases``), the same
  compare-and-swap pattern as the M2 execution lease. Revoking a member
  releases their leases (``brain.team.provisioning.revoke_principal_access``);
  a team administrator can force-release a lease with a recorded reason
  (``force_release_lease``), which also bumps the fencing token.
* **Integration queue.** Completed runs (a recorded head revision) are queued
  per repository. ``integrate`` re-validates the next entry against the
  current base revision: a different base gives ``needs_rebase`` unless the
  current base is the head of entries integrated on the run's own base, in
  which case the run's patch is applied after theirs in a temporary index
  (``brain.team.combined_patch``): a conflict rejects it with the paths, a
  clean result goes on to the other checks; superseded instructions give
  ``stale_instructions``, otherwise an independent reviewer marks it
  ``integrated``. No git merge or push happens here: the combined patch is
  computed and reported, and the integrator merges. An integrated entry
  completes its task (``agent_tasks.status = "completed"`` plus a
  ``status_change`` event); a rejected one is recorded on the task ledger and
  leaves the task open for another run.
"""
from __future__ import annotations

from brain.team.coordination._common import (MAX_SCOPE_PATHS as MAX_SCOPE_PATHS, MAX_DEPENDENCIES as MAX_DEPENDENCIES, MAX_SUBTASKS as MAX_SUBTASKS, MAX_RESOLVED as MAX_RESOLVED, MIN_LEASE_SECONDS as MIN_LEASE_SECONDS, MAX_LEASE_SECONDS as MAX_LEASE_SECONDS, MAX_CHANGED_PATHS as MAX_CHANGED_PATHS, MAX_OVERLAP_PAIRS as MAX_OVERLAP_PAIRS, MAX_REASON as MAX_REASON, MAX_DEPENDENCY_PATHS as MAX_DEPENDENCY_PATHS, MAX_DEPENDENCY_NEIGHBORS as MAX_DEPENDENCY_NEIGHBORS, DEPENDENCY_TIMEOUT_SECONDS as DEPENDENCY_TIMEOUT_SECONDS, INACTIVE_TASK_STATUSES as INACTIVE_TASK_STATUSES, PENDING as PENDING, FINAL as FINAL, _GLOB as _GLOB, _DRIVE as _DRIVE, _RESOURCE as _RESOURCE, _uuid as _uuid, normalize_path as normalize_path, normalize_scope as normalize_scope, normalize_changed_path as normalize_changed_path, normalize_resource as normalize_resource, _static_prefix as _static_prefix, paths_overlap as paths_overlap, overlapping as overlapping, _done as _done, _related as _related)
from brain.team.coordination.overlaps import (_active_plans as _active_plans, _live_leases as _live_leases, _conflicts as _conflicts, DependencyGraph as DependencyGraph, DependencyProvider as DependencyProvider, _graph_result as _graph_result, _is_file_path as _is_file_path, graph_dependencies as graph_dependencies, dependency_provider as dependency_provider, graph_report as graph_report, _repository_graph as _repository_graph, dependency_pairs as dependency_pairs, _dependency_conflicts as _dependency_conflicts, _record_overlaps as _record_overlaps, detect_overlaps as detect_overlaps, changed_scope as changed_scope, record_change_overlaps as record_change_overlaps, overlaps_view as overlaps_view, overlaps_report as overlaps_report, list_overlaps as list_overlaps)
from brain.team.coordination.plans import (plan_dict as plan_dict, _binding as _binding, _siblings as _siblings, find_cycle as find_cycle, _validate_dependencies as _validate_dependencies, require_dependencies_done as require_dependencies_done, _topological as _topological, plan_view as plan_view, get_plan as get_plan, create_subtask as create_subtask, update_plan as update_plan, dispatch as dispatch)
from brain.team.coordination.instructions import (resolve_instructions as resolve_instructions, snapshot_instructions as snapshot_instructions, superseded_instructions as superseded_instructions, get_run_instructions as get_run_instructions)
from brain.team.coordination.leases import (lease_dict as lease_dict, _ttl as _ttl, acquire_task_lease as acquire_task_lease, acquire_lease as acquire_lease, _held_lease as _held_lease, renew_lease as renew_lease, release_lease as release_lease, force_release_lease as force_release_lease, operator_force_release_lease as operator_force_release_lease, _force_release as _force_release, leases_view as leases_view, list_leases as list_leases)
from brain.team.coordination.queue import (entry_dict as entry_dict, _executed_base as _executed_base, _coordination_event as _coordination_event, enqueue as enqueue, _settle_task as _settle_task, _entry as _entry, _run_copy as _run_copy, _combined_patch as _combined_patch, integrate as integrate, reject as reject, queue_view as queue_view, list_queue as list_queue)

__all__ = ["acquire_task_lease", "acquire_lease", "changed_scope", "create_subtask", "dispatch", "enqueue", "find_cycle",
           "force_release_lease", "get_plan", "get_run_instructions",
           "integrate", "leases_view", "list_leases", "list_overlaps", "list_queue", "normalize_path",
           "operator_force_release_lease",
           "normalize_resource", "overlaps_report", "overlaps_view", "paths_overlap", "plan_view", "queue_view", "record_change_overlaps",
           "reject", "release_lease", "renew_lease", "require_dependencies_done",
           "resolve_instructions", "snapshot_instructions", "superseded_instructions", "update_plan"]
