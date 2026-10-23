// The team Board's model (M5): columns, the shapes mapped from raw API rows and
// which controls to show. Pure and safe in client components; loading lives in
// lib/board-view.ts (server only). Brain decides what each caller may see and
// do; the action flags below only hide controls Brain would refuse anyway, and
// Brain still checks every call.
import type { MeProject } from "./team-activity-view";
import type { Segment } from "./atlas";

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
  decidedAt: string | null;
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
  /** "dependency": the scopes share no path but one imports the other (weaker than "task"). */
  kind: "task" | "lease" | "dependency";
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
    decidedAt: text(e.decided_at),
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
  if (!o || (o.kind !== "task" && o.kind !== "lease" && o.kind !== "dependency") || !text(o.task_id)) return null;
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

/**
 * The base the board can vouch for when integrating `entry`: the newest integrated head in its
 * repository (what Brain defaults to), else the base Brain last checked the entry against.
 */
export function knownBase(board: Pick<ProjectBoard, "tasks" | "queue">, entry: QueueEntry): { sha: string; source: "integrated" | "checked" } | null {
  const integrated = [...board.tasks.map((t) => t.entry), ...board.queue]
    .filter((e): e is QueueEntry => Boolean(e) && e!.status === "integrated" && e!.repositoryId === entry.repositoryId && Boolean(e!.headSha))
    // Match Brain's authoritative ordering: decision time first, then ID.
    .sort((a, b) => (Date.parse(b.decidedAt ?? "") || 0) - (Date.parse(a.decidedAt ?? "") || 0) || b.id - a.id)[0];
  if (integrated) return { sha: integrated.headSha, source: "integrated" };
  return entry.checkedBaseSha ? { sha: entry.checkedBaseSha, source: "checked" } : null;
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

// ---------------------------------------------------------------------------
// What the Board says first, what needs the member, and who claims which paths

/** A path without its trailing glob: "db/migrations/**" and "db/migrations" are one place. */
export const normPath = (p: string) => p.replace(/\/\*\*?$/, "").replace(/\/+$/, "") || p;

const inFlight = (t: BoardTask) => t.column !== null && t.column !== "integrated";

/** The opening sentence: what can't land, what is next in line, where work overlaps. Mentions are task ids. */
export function boardVerdict(board: ProjectBoard, selfId: number | null): Segment[] {
  const titles = new Map(board.tasks.map((t) => [t.id, t.title]));
  const mention = (id: string): Segment => ({ ref: id, label: titles.get(id) ?? `task ${id.slice(0, 8)}` });
  const verdict = (e: QueueEntry) => (e.status === "needs_rebase" ? " needs a rebase" : " has stale instructions");
  const out: Segment[] = [];
  const say = (...parts: Segment[]) => { if (out.length) out.push(" "); out.push(...parts); };

  const held = board.queue.filter((e) => e.status !== "queued");
  if (held.length === 1) say(mention(held[0].taskId), verdict(held[0]), " and can’t land yet.");
  else if (held.length > 1) {
    say({ num: String(held.length), tone: "warn" }, " runs can’t land yet: ");
    held.slice(0, 2).forEach((e, i) => { if (i) out.push(" and "); out.push(mention(e.taskId), verdict(e)); });
    out.push(held.length > 2 ? `, and ${held.length - 2} more.` : ".");
  }
  for (const e of board.queue.filter((q) => queuePosition(board.queue, q) === 1)) {
    say(mention(e.taskId), entryActions(e, board.permissions, selfId).integrate ? " is next in line and waits for your review." : " is next in line to integrate.");
  }
  const overlaps = board.overlaps.length;
  if (overlaps) say("Work overlaps in ", { num: String(overlaps), tone: "warn" }, overlaps === 1 ? " place." : " places.");
  if (!out.length) {
    const active = board.tasks.filter(inFlight).length;
    say(active ? `Nothing on ${board.name} needs a person right now; ${active} ${active === 1 ? "task is" : "tasks are"} in flight.` : `Nothing is in flight on ${board.name}.`);
  }
  return out;
}

export type Attention =
  /** A run Brain refused to integrate as it is: it needs a rebase or a fresh run. */
  | { kind: "held"; taskId: string; entry: QueueEntry }
  /** The first entry in a repository's queue, when the member may integrate it. */
  | { kind: "integrate"; taskId: string; entry: QueueEntry }
  /** A lease the member holds: renew it or let others in. */
  | { kind: "lease"; taskId: string | null; lease: Lease }
  | { kind: "overlap"; taskId: string; overlap: Overlap };

/** What asks for a person, most blocking first. */
export function attention(board: ProjectBoard, selfId: number | null): Attention[] {
  const out: Attention[] = [];
  for (const e of board.queue) if (e.status !== "queued") out.push({ kind: "held", taskId: e.taskId, entry: e });
  for (const e of board.queue) {
    if (queuePosition(board.queue, e) === 1 && entryActions(e, board.permissions, selfId).integrate) out.push({ kind: "integrate", taskId: e.taskId, entry: e });
  }
  for (const l of board.leases) if (leaseActions(l, board.permissions, selfId).release) out.push({ kind: "lease", taskId: l.taskId, lease: l });
  for (const o of board.overlaps) out.push({ kind: "overlap", taskId: o.taskId, overlap: o });
  return out;
}

export interface Claim {
  path: string;
  /** The path below its nearest claimed ancestor (the whole path at the top level). */
  label: string;
  depth: number;
  /** Tasks in flight that declared the path; tasks that only group subtasks claim through them. */
  tasks: string[];
  leases: Lease[];
  /** Brain reported an overlap on this path. */
  overlap: boolean;
}

/** Every path claimed by a task in flight or a live lease, as a tree in path order. */
export function claimMap(board: ProjectBoard): Claim[] {
  const groups = new Set(board.tasks.map((t) => t.parentId).filter((id): id is string => id !== null));
  const rows = new Map<string, { tasks: Set<string>; leases: Lease[] }>();
  const at = (p: string) => {
    const key = normPath(p);
    let row = rows.get(key);
    if (!row) rows.set(key, (row = { tasks: new Set(), leases: [] }));
    return row;
  };
  for (const t of board.tasks) if (inFlight(t) && !groups.has(t.id)) for (const p of t.scopePaths) at(p).tasks.add(t.id);
  for (const l of board.leases) at(l.resource).leases.push(l);
  const hot = new Set(board.overlaps.flatMap((o) => o.paths.flat().map(normPath)));
  // Sorting with "/" lowest keeps every path right after its ancestors ("a/b", "a/b/c", then "a/b-c").
  const order = (p: string) => p.replace(/\//g, "\u0000");
  const paths = [...rows.keys()].sort((a, b) => (order(a) < order(b) ? -1 : order(a) > order(b) ? 1 : 0));
  const depth = new Map<string, number>();
  return paths.map((path) => {
    const parent = paths.filter((p) => path.startsWith(`${p}/`)).sort((a, b) => b.length - a.length)[0];
    const d = parent ? (depth.get(parent) ?? 0) + 1 : 0;
    depth.set(path, d);
    const row = rows.get(path)!;
    return { path, label: parent ? path.slice(parent.length + 1) : path, depth: d, tasks: [...row.tasks], leases: row.leases, overlap: hot.has(path) };
  });
}
