// Demo data for the Board when no Brain API is configured. Raw API shapes, so
// the demo goes through the same mapping as live data (lib/board-model.ts).

const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const PROJECT = "11111111-1111-4111-8111-111111111111";
const PARENT = id(1);
const [S1, S2, S3, S4, S5, T2] = [id(2), id(3), id(4), id(5), id(6), id(7)];
const RUN = (n: number) => `22222222-2222-4222-8222-${String(n).padStart(12, "0")}`;
const SHA = (c: string) => c.repeat(40);

const task = (taskId: string, title: string, status: string, branch: string) => ({
  id: taskId, project_id: PROJECT, repository_id: 1, author_id: 7, title, goal: title, status, version: 1, branch,
  declared_base_sha: SHA("a"),
});
const plan = (taskId: string, parent: string | null, depends: string[], scope: string[]) => ({
  task_id: taskId, parent_task_id: parent, depends_on: depends, scope_paths: scope, version: 1,
});
const subtask = (taskId: string, title: string, state: string, depends: string[], scope: string[], runs: string[], waiting: string[] = []) => ({
  ...task(taskId, title, "created", `retries/${taskId.slice(-2)}`), plan: plan(taskId, PARENT, depends, scope), state, waiting_on: waiting, runs,
});
const entry = (entryId: number, taskId: string, run: string, status: string, enqueuedBy: number, extra: Record<string, unknown> = {}) => ({
  id: entryId, project_id: PROJECT, repository_id: 1, task_id: taskId, run_id: run, base_sha: SHA("a"), head_sha: SHA(String(entryId)),
  status, reason: null, checked_base_sha: null, checked_at: null, enqueued_by: enqueuedBy, enqueued_at: "2026-10-18T08:0" + entryId + ":00+00:00",
  decided_by: null, decided_at: null, version: 1, ...extra,
});

const subtasks = [
  subtask(S1, "Retry policy module", "done", [], ["brain/payments/retry.py"], [RUN(1)]),
  subtask(S2, "Wire retries into the checkout API", "running", [S1], ["apps/api/checkout/**"], [RUN(2)]),
  subtask(S3, "Retry metrics on the dashboard", "waiting", [S2], ["apps/web/app/payments/**"], [], [S2]),
  subtask(S4, "Idempotency keys for refunds", "ready", [S1], ["apps/api/checkout/refunds.py", "db/migrations/**"], []),
  subtask(S5, "Document the retry policy", "running", [S1], ["docs/PAYMENTS.md"], [RUN(5)]),
];

export const DEMO_BOARD = {
  selfId: 4,
  project: {
    id: PROJECT,
    name: "Checkout",
    role: "reviewer",
    effectivePermissions: ["read", "task.create", "run.request", "knowledge.propose", "knowledge.approve", "task.accept"],
    repositories: [{ id: 1, name: "checkout-service", path: "/srv/checkout-service" }],
  },
  raw: {
    tasks: [
      task(PARENT, "Payment retries", "running", "retries"),
      ...subtasks.map((s) => task(s.id, s.title, s.id === S1 ? "completed" : "created", s.branch)),
      task(T2, "Upgrade the HTTP client", "running", "http-client"),
    ],
    plans: {
      [PARENT]: { task: task(PARENT, "Payment retries", "running", "retries"), plan: plan(PARENT, null, [], ["apps/api/checkout/**", "brain/payments/**"]), subtasks, ready: [S4] },
      [T2]: { task: task(T2, "Upgrade the HTTP client", "running", "http-client"), plan: plan(T2, null, [], ["brain/http/**", "apps/api/checkout/client.py"]), subtasks: [], ready: [] },
    },
    leases: [
      { id: 11, project_id: PROJECT, repository_id: 1, resource: "apps/api/checkout/handlers.py", holder_id: 4, task_id: S2, fencing_token: 3, state: "held",
        acquired_at: "2026-10-18T09:00:00+00:00", expires_at: "2026-10-18T09:05:00+00:00", released_at: null, released_reason: null },
      { id: 12, project_id: PROJECT, repository_id: 1, resource: "db/migrations", holder_id: 7, task_id: null, fencing_token: 8, state: "held",
        acquired_at: "2026-10-18T09:01:00+00:00", expires_at: "2026-10-18T09:11:00+00:00", released_at: null, released_reason: null },
    ],
    overlaps: [
      { kind: "task", repository_id: 1, task_id: S2, other_task_id: T2, other_title: "Upgrade the HTTP client", paths: [["apps/api/checkout/**", "apps/api/checkout/client.py"]] },
      { kind: "lease", repository_id: 1, task_id: S4, lease_id: 12, resource: "db/migrations", holder_id: 7, expires_at: "2026-10-18T09:11:00+00:00", paths: [["db/migrations/**", "db/migrations"]] },
    ],
    entries: [
      entry(1, S1, RUN(1), "integrated", 7, { decided_by: 4, decided_at: "2026-10-18T08:30:00+00:00", version: 2 }),
      entry(2, T2, RUN(7), "needs_rebase", 9, { reason: `Run base ${"a".repeat(12)} is not the current base ${"1".repeat(12)}`, checked_base_sha: SHA("1"), checked_at: "2026-10-18T08:40:00+00:00", version: 2 }),
      entry(3, S5, RUN(5), "stale_instructions", 4, { reason: "Instructions changed since the run started: decision 42 (edited), rule 7 (retired)", checked_base_sha: SHA("a"), checked_at: "2026-10-18T08:45:00+00:00", version: 2 }),
      entry(4, S2, RUN(2), "queued", 7),
    ],
    instructions: [
      { run_id: RUN(5), task_id: S5, status: "superseded", resolved_at: "2026-10-18T07:50:00+00:00", resolved: [],
        superseded: [{ kind: "decision", id: 42, version: 3, current_version: 4, reason: "edited" }, { kind: "rule", id: 7, version: 2, current_version: 2, reason: "retired" }] },
    ],
  },
};
