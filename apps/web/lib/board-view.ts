// Data for the team Board (M5): per project, every task and subtask in a column by
// state, live leases, overlap warnings and the integration queue in order.
// Composed from the existing M3 coordination routes, read with the signed-in
// member's own key (lib/api.ts) and fresh on every render. The pure model (and
// the controls each member sees) is lib/board-model.ts.
import "server-only";
import { apiConfigured, brainRead } from "./api";
import { DEMO_BOARD } from "./board-demo";
import { assembleProject, latestEntries, list, obj, str, toEntry, type BoardView, type ProjectBoard, type QueueEntry } from "./board-model";
import { getMeView, type MeProject } from "./team-activity-view";

export * from "./board-model";

// ---------------------------------------------------------------------------
// Loading

/** Board limits: one page of tasks (Brain returns at most 100), one plan read per task, a few instruction reads. */
const MAX_PROJECTS = 10;
const MAX_PLANS = 60;
const MAX_INSTRUCTION_READS = 20;
const CONCURRENCY = 6;

async function pool<T, R>(items: T[], limit: number, fn: (item: T) => Promise<R>): Promise<R[]> {
  const out: R[] = new Array(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const index = next++;
      out[index] = await fn(items[index]);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

const read = (path: string) => brainRead<unknown>(path, { fresh: true, timeoutMs: 6000 });

export async function loadProject(project: MeProject): Promise<ProjectBoard> {
  const base = `/team/projects/${encodeURIComponent(project.id)}`;
  const missing: string[] = [];
  const [tasksRes, leasesRes, overlapsRes, pendingRes, doneRes] = await Promise.all([
    read(`${base}/tasks`),
    read(`${base}/leases`),
    read(`${base}/overlaps`),
    read(`${base}/integration`),
    read(`${base}/integration?include_done=true`),
  ]);
  const tasks = tasksRes.ok && Array.isArray(tasksRes.data) ? (tasksRes.data as unknown[]) : [];
  if (!tasksRes.ok) missing.push("tasks");
  if (!leasesRes.ok) missing.push("leases");
  if (!overlapsRes.ok) missing.push("overlap warnings");
  if (!pendingRes.ok) missing.push("integration queue");

  const ids = tasks.map((t) => str(obj(t)?.id)).filter(Boolean);
  const planIds = ids.slice(0, MAX_PLANS);
  const planAnswers = await pool(planIds, CONCURRENCY, (id) => read(`${base}/tasks/${encodeURIComponent(id)}/plan`));
  const plans: Record<string, unknown> = {};
  planAnswers.forEach((answer, index) => {
    if (answer.ok) plans[planIds[index]] = answer.data;
  });
  if (planAnswers.some((a) => !a.ok)) missing.push("some task plans");

  const entries = [...list(pendingRes.ok ? pendingRes.data : null, "entries"), ...list(doneRes.ok ? doneRes.data : null, "entries")];
  const latest = latestEntries(entries.map(toEntry).filter((e): e is QueueEntry => e !== null));
  const stale = [...latest.values()].filter((e) => e.status === "stale_instructions").slice(0, MAX_INSTRUCTION_READS);
  const instructionAnswers = await pool(stale, CONCURRENCY, (e) =>
    read(`${base}/tasks/${encodeURIComponent(e.taskId)}/runs/${encodeURIComponent(e.runId)}/instructions`));

  return assembleProject(project, {
    tasks,
    plans,
    leases: list(leasesRes.ok ? leasesRes.data : null, "leases"),
    overlaps: list(overlapsRes.ok ? overlapsRes.data : null, "overlaps"),
    entries,
    instructions: instructionAnswers.filter((a) => a.ok).map((a) => (a.ok ? a.data : null)),
    truncated: tasks.length >= 100 || ids.length > MAX_PLANS,
    missing,
  });
}

/**
 * The board for every project the member belongs to. Coordination is
 * authorized by project membership, so a team admin sees the projects they are
 * a member of, like everyone else.
 */
export async function getBoardView(): Promise<BoardView> {
  if (!apiConfigured) {
    return { kind: "ok", demo: true, selfId: DEMO_BOARD.selfId, projects: [assembleProject(DEMO_BOARD.project, DEMO_BOARD.raw)] };
  }
  const me = await getMeView();
  if (me.kind !== "ok") return { kind: "unavailable" };
  if (me.teamState !== "team") return { kind: "solo" };
  const projects = me.projects.filter((p) => !p.disabled).slice(0, MAX_PROJECTS);
  return { kind: "ok", demo: false, selfId: me.principal.id, projects: await Promise.all(projects.map(loadProject)) };
}
