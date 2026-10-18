// The team Board's model (M5): columns, the shapes mapped from raw API rows and
// which controls to show. Pure and safe in client components; loading lives in
// lib/board-view.ts (server only). Brain decides what each caller may see and
// do; the action flags below only hide controls Brain would refuse anyway, and
// Brain still checks every call.
import type { MeProject } from "./team-activity-view";

export type BoardColumn = "blocked" | "ready" | "running" | "awaiting" | "needs_rebase" | "stale_instructions" | "integrated";

export const BOARD_COLUMNS: { id: BoardColumn; label: string; hint: string }[] = [
  { id: "blocked", label: "Blocked by dependencies", hint: "Waiting for other subtasks, or a dependency was cancelled" },
  { id: "ready", label: "Ready", hint: "Dependencies done; a run can be requested" },
  { id: "running", label: "Running", hint: "Has a run that is not in the queue yet" },
  { id: "awaiting", label: "Awaiting integration", hint: "Queued; a reviewer integrates in order" },
  { id: "needs_rebase", label: "Needs rebase", hint: "The run's base is not the current base" },
  { id: "stale_instructions", label: "Stale instructions", hint: "A decision or rule changed since the run started" },
  { id: "integrated", label: "Integrated", hint: "Integrated, or completed" },
];

export type QueueStatus = "queued" | "needs_rebase" | "stale_instructions" | "integrated" | "rejected";
const QUEUE_STATUSES: readonly QueueStatus[] = ["queued", "needs_rebase", "stale_instructions", "integrated", "rejected"];
const PENDING: ReadonlySet<QueueStatus> = new Set(["queued", "needs_rebase", "stale_instructions"]);
/** Harness task statuses that mean work is under way (brain/memory/harness_store.py RUNNING_TASK_STATUSES). */
const RUNNING_STATUSES = new Set(["running", "in_progress", "started", "indexing", "processing", "claimed", "artifacted", "memory_updated", "validating", "acceptance_pending"]);

export interface QueueEntry {
  id: number;
  repositoryId: number | null;
  taskId: string;
  runId: string;
  baseSha: string;
  headSha: string;
  status: QueueStatus;
  reason: string | null;
  checkedBaseSha: string | null;
  checkedAt: string | null;
  enqueuedBy: number | null;
  enqueuedAt: string | null;
  initiatorId: number | null;
  requestedBy: number | null;
  version: number;
}

export interface Lease {
  id: number;
  repositoryId: number | null;
  resource: string;
  holderId: number | null;
  taskId: string | null;
  fencingToken: number;
  state: "held" | "expired" | "released";
  expiresAt: string | null;
}

export interface Overlap {
  kind: "task" | "lease";
  repositoryId: number | null;
  taskId: string;
  otherTaskId: string | null;
  otherTitle: string | null;
  leaseId: number | null;
  resource: string | null;
  holderId: number | null;
  expiresAt: string | null;
  /** Pairs of overlapping paths: [this task's path, the other side's path]. */
  paths: [string, string][];
}

export interface SupersededInstruction {
  kind: "decision" | "rule";
  id: number;
  version: number | null;
  currentVersion: number | null;
  reason: "edited" | "retired" | "deleted" | string;
}

export interface RunInstructions {
  runId: string;
  status: "unknown" | "current" | "superseded";
  superseded: SupersededInstruction[];
}

export interface BoardTask {
  id: string;
  title: string;
  status: string;
  repositoryId: number | null;
  branch: string;
  parentId: string | null;
  parentTitle: string | null;
  /** Derived by Brain's plan view for subtasks (done, cancelled, blocked, running, ready, waiting); null for top-level tasks. */
  state: string | null;
  dependsOn: string[];
  /** Dependencies that are not done yet. */
  waitingOn: string[];
  scopePaths: string[];
  runs: string[];
  /** The task's most recent queue entry, pending or final. */
  entry: QueueEntry | null;
  column: BoardColumn | null;
}

export interface ProjectBoard {
  id: string;
  name: string;
  role: string;
  permissions: string[];
  repositories: { id: number; name: string }[];
  tasks: BoardTask[];
  leases: Lease[];
  overlaps: Overlap[];
  /** Pending queue entries in integration order (oldest first). */
  queue: QueueEntry[];
  instructions: Record<string, RunInstructions>;
  /** More tasks exist than the board loaded. */
  truncated: boolean;
  /** Parts Brain didn't return (shown as a note, the rest of the board still renders). */
  missing: string[];
}

export type BoardView =
  | { kind: "ok"; demo: boolean; selfId: number | null; projects: ProjectBoard[] }
  | { kind: "solo" }
  | { kind: "unavailable" };

// ---------------------------------------------------------------------------
// Mapping raw API rows (pure; unit-tested)

type Raw = Record<string, unknown>;
export const obj = (v: unknown): Raw | null => (v && typeof v === "object" && !Array.isArray(v) ? (v as Raw) : null);
export const str = (v: unknown) => (typeof v === "string" ? v : "");
const text = (v: unknown) => (typeof v === "string" && v ? v : null);
const int = (v: unknown) => (typeof v === "number" && Number.isInteger(v) ? v : null);
const strings = (v: unknown) => (Array.isArray(v) ? v.filter((s): s is string => typeof s === "string") : []);
export const list = (data: unknown, key: string): unknown[] => {
  const d = obj(data);
  return d && Array.isArray(d[key]) ? (d[key] as unknown[]) : [];
};

export function toEntry(raw: unknown): QueueEntry | null {
  const e = obj(raw);
  const id = int(e?.id);
  const version = int(e?.version);
  const status = QUEUE_STATUSES.find((s) => s === e?.status);
  if (!e || id === null || version === null || !status || !text(e.task_id) || !text(e.run_id)) return null;
  return {
    id, version, status,
    repositoryId: int(e.repository_id),
    taskId: str(e.task_id),
    runId: str(e.run_id),
    baseSha: str(e.base_sha),
    headSha: str(e.head_sha),
    reason: text(e.reason),
    checkedBaseSha: text(e.checked_base_sha),
    checkedAt: text(e.checked_at),
    enqueuedBy: int(e.enqueued_by),
    enqueuedAt: text(e.enqueued_at),
    initiatorId: int(e.initiator_id),
    requestedBy: int(e.requested_by),
  };
}

export function toLease(raw: unknown): Lease | null {
  const l = obj(raw);
  const id = int(l?.id);
  const token = int(l?.fencing_token);
  const state = l?.state === "held" || l?.state === "expired" || l?.state === "released" ? l.state : null;
  if (!l || id === null || token === null || !state || !text(l.resource)) return null;
  return {
    id, state, fencingToken: token,
    repositoryId: int(l.repository_id),
    resource: str(l.resource),
    holderId: int(l.holder_id),
    taskId: text(l.task_id),
    expiresAt: text(l.expires_at),
  };
}

export function toOverlap(raw: unknown): Overlap | null {
  const o = obj(raw);
  if (!o || (o.kind !== "task" && o.kind !== "lease") || !text(o.task_id)) return null;
  const paths = (Array.isArray(o.paths) ? o.paths : [])
    .filter((p): p is unknown[] => Array.isArray(p) && p.length === 2 && p.every((s) => typeof s === "string"))
    .map((p) => [p[0], p[1]] as [string, string]);
  return {
    kind: o.kind,
    repositoryId: int(o.repository_id),
    taskId: str(o.task_id),
    otherTaskId: text(o.other_task_id),
    otherTitle: text(o.other_title),
    leaseId: int(o.lease_id),
    resource: text(o.resource),
    holderId: int(o.holder_id),
    expiresAt: text(o.expires_at),
    paths,
  };
}

export function toInstructions(raw: unknown): RunInstructions | null {
  const r = obj(raw);
  if (!r || !text(r.run_id)) return null;
  const status = r.status === "current" || r.status === "superseded" ? r.status : "unknown";
  const superseded = (Array.isArray(r.superseded) ? r.superseded : [])
    .map(obj)
    .filter((s): s is Raw => s !== null && (s.kind === "decision" || s.kind === "rule") && int(s.id) !== null)
    .map((s) => ({ kind: s.kind as "decision" | "rule", id: int(s.id) as number, version: int(s.version), currentVersion: int(s.current_version), reason: str(s.reason) || "edited" }));
  return { runId: str(r.run_id), status, superseded };
}

/** Where a task sits: its latest queue entry wins, else Brain's plan state, else the task status. Null hides it (cancelled). */
export function columnFor(state: string | null, status: string, entry: QueueEntry | null): BoardColumn | null {
  switch (entry?.status) {
    case "integrated": return "integrated";
    case "needs_rebase": return "needs_rebase";
    case "stale_instructions": return "stale_instructions";
    case "queued": return "awaiting";
    default: break; // none, or rejected: back to the plan
  }
  switch (state) {
    case "done": return "integrated";
    case "cancelled": return null;
    case "blocked":
    case "waiting": return "blocked";
    case "running": return "running";
    case "ready": return "ready";
    default: break;
  }
  if (status === "completed") return "integrated";
  if (status === "cancelled" || status === "failed") return null;
  return RUNNING_STATUSES.has(status) ? "running" : "ready";
}

/** The latest entry per task (highest id), across pending and final entries. */
export function latestEntries(entries: QueueEntry[]): Map<string, QueueEntry> {
  const latest = new Map<string, QueueEntry>();
  for (const e of entries) {
    const seen = latest.get(e.taskId);
    if (!seen || e.id > seen.id) latest.set(e.taskId, e);
  }
  return latest;
}

export interface RawProjectData {
  tasks: unknown[];
  /** Plan views by task id (GET .../tasks/{id}/plan). */
  plans: Record<string, unknown>;
  leases: unknown[];
  overlaps: unknown[];
  /** Pending and final entries; duplicates are merged by id. */
  entries: unknown[];
  instructions: unknown[];
  truncated?: boolean;
  missing?: string[];
}

export function assembleProject(project: Pick<MeProject, "id" | "name" | "role" | "effectivePermissions" | "repositories">, raw: RawProjectData): ProjectBoard {
  const byId = new Map<number, QueueEntry>();
  for (const e of raw.entries.map(toEntry)) if (e) byId.set(e.id, e);
  const entries = [...byId.values()].sort((a, b) => a.id - b.id);
  const latest = latestEntries(entries);

  // Subtask facts come from the parent's plan view; a task's own plan says who its parent is.
  const sub = new Map<string, { parentId: string; parentTitle: string | null; state: string | null; waitingOn: string[]; runs: string[] }>();
  const ownPlan = new Map<string, { parentId: string | null; dependsOn: string[]; scopePaths: string[] }>();
  for (const [taskId, view] of Object.entries(raw.plans)) {
    const v = obj(view);
    if (!v) continue;
    const plan = obj(v.plan);
    if (plan) ownPlan.set(taskId, { parentId: text(plan.parent_task_id), dependsOn: strings(plan.depends_on), scopePaths: strings(plan.scope_paths) });
    const parentTitle = text(obj(v.task)?.title);
    for (const s of Array.isArray(v.subtasks) ? v.subtasks : []) {
      const row = obj(s);
      if (!row || !text(row.id)) continue;
      sub.set(str(row.id), { parentId: taskId, parentTitle, state: text(row.state), waitingOn: strings(row.waiting_on), runs: strings(row.runs) });
      const p = obj(row.plan);
      if (p && !ownPlan.has(str(row.id))) ownPlan.set(str(row.id), { parentId: taskId, dependsOn: strings(p.depends_on), scopePaths: strings(p.scope_paths) });
    }
  }

  const tasks: BoardTask[] = [];
  for (const rawTask of raw.tasks) {
    const t = obj(rawTask);
    if (!t || !text(t.id)) continue;
    const id = str(t.id);
    const facts = sub.get(id);
    const plan = ownPlan.get(id);
    const entry = latest.get(id) ?? null;
    const status = str(t.status);
    const column = columnFor(facts?.state ?? null, status, entry);
    tasks.push({
      id,
      title: str(t.title) || id,
      status,
      repositoryId: int(t.repository_id),
      branch: str(t.branch),
      parentId: facts?.parentId ?? plan?.parentId ?? null,
      parentTitle: facts?.parentTitle ?? null,
      state: facts?.state ?? null,
      dependsOn: plan?.dependsOn ?? [],
      waitingOn: facts?.waitingOn ?? [],
      scopePaths: plan?.scopePaths ?? [],
      runs: facts?.runs ?? [],
      entry,
      column,
    });
  }

  const instructions: Record<string, RunInstructions> = {};
  for (const i of raw.instructions.map(toInstructions)) if (i) instructions[i.runId] = i;

  return {
    id: project.id,
    name: project.name,
    role: project.role,
    permissions: project.effectivePermissions,
    repositories: project.repositories.map((r) => ({ id: r.id, name: r.name || r.path || `repository ${r.id}` })),
    tasks,
    leases: raw.leases.map(toLease).filter((l): l is Lease => l !== null && l.state === "held"),
    overlaps: raw.overlaps.map(toOverlap).filter((o): o is Overlap => o !== null),
    queue: entries.filter((e) => PENDING.has(e.status)),
    instructions,
    truncated: raw.truncated === true,
    missing: raw.missing ?? [],
  };
}

// ---------------------------------------------------------------------------
// Which controls to show (Brain still decides every call)

export interface EntryActions {
  /** Re-validate and integrate: a Reviewer (task.accept) who did not queue it; Brain also refuses the run's initiator. */
  integrate: boolean;
  /** Take it out of the queue: a Reviewer, or the member who queued it (withdraw). */
  reject: boolean;
  withdraw: boolean;
  /** Request a fresh run of the task: resolves the current instructions. */
  rerun: boolean;
}

export function entryActions(entry: QueueEntry, permissions: string[], selfId: number | null): EntryActions {
  const reviewer = permissions.includes("task.accept");
  const mine = selfId !== null && entry.enqueuedBy === selfId;
  const independent = selfId !== null && entry.initiatorId !== selfId && entry.requestedBy !== selfId;
  const pending = PENDING.has(entry.status);
  return {
    integrate: pending && reviewer && independent,
    reject: pending && reviewer,
    withdraw: pending && !reviewer && mine,
    rerun: entry.status !== "integrated" && permissions.includes("run.request"),
  };
}

/** Blocking position within the repository's queued entries; non-queued verdicts do not block. */
export function queuePosition(entries: QueueEntry[], target: QueueEntry): number | null {
  if (target.status !== "queued") return null;
  return entries.filter((entry) => entry.status === "queued"
    && entry.repositoryId === target.repositoryId && entry.id <= target.id).length;
}

export interface LeaseActions { release: boolean; renew: boolean }

export function leaseActions(lease: Lease, permissions: string[], selfId: number | null): LeaseActions {
  const mine = selfId !== null && lease.holderId === selfId && lease.state === "held";
  return { release: mine && permissions.includes("read"), renew: mine && permissions.includes("run.request") };
}

/** A link to a superseded decision or rule on the Memory page (its search matches ids). */
export function instructionHref(item: Pick<SupersededInstruction, "kind" | "id">): string {
  return `/memory?view=${item.kind === "rule" ? "rules" : "decisions"}&q=${item.id}`;
}
