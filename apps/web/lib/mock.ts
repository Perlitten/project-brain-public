// Demo data. Shapes mirror the FastAPI dashboard so a live adapter can replace
// this module without touching a component. Every screen that renders this data
// says so in the masthead.
import type {
  AgentRun,
  CodeModule,
  Condition,
  ContextPack,
  Corpus,
  Decision,
  Identity,
  IndexRun,
  Insight,
  Job,
  LedgerEvent,
  McpTool,
  ModuleEdge,
  Principal,
  Report,
  Repository,
  RerankerStatus,
  Rule,
  SetupStep,
} from "./types";

export const repositories: Repository[] = [
  { id: 1, slug: "project-brain", name: "project-brain", path: "/srv/repos/project-brain", branch: "master", head: "d57f014", behind: 3,
    originPoll: { status: "ok", at: "02 Oct 2026, 03:25 UTC", remote: "https://***@github.com/you/project-brain.git", behind: 3 } },
  { id: 2, slug: "docs-site", name: "docs-site", path: "/srv/repos/docs-site", branch: "main", head: "b113aca", behind: 0 },
  { id: 3, slug: "mobile-app", name: "mobile-app", path: "/srv/repos/mobile-app", branch: "master", head: "bec759d", behind: 0 },
  { id: 4, slug: "billing-service", name: "billing-service", path: "/srv/repos/billing-service", branch: "main", head: "9f949e9", behind: 41,
    originPoll: { status: "error", at: "02 Oct 2026, 03:25 UTC", remote: "git@github.com:you/billing-service.git", error: "git ls-remote timed out after 30s", behind: null } },
];

export function findRepository(slug?: string | null): Repository {
  return repositories.find((r) => r.slug === slug) ?? repositories[0];
}

export function condition(repo: Repository): Condition {
  if ((repo.behind ?? 0) > 20) {
    return {
      tone: "bad",
      headline: `Brain is reading an old copy of your code`,
      detail:
        `Your code changed ${repo.behind} times since Brain last read it. AI agents asking Brain for help will get outdated answers until you refresh it.`,
      action: { label: "Refresh Brain’s copy", hint: "Re-reads the whole repository. Takes about 6 minutes." },
      tiles: [
        { value: String(repo.behind), label: "changes not yet read", tone: "bad" },
        { value: "2,960", label: "pieces not searchable yet", tone: "warn" },
        { value: "9", label: "context packs to rebuild", tone: "warn" },
        { value: "6d", label: "since Brain last read the code", tone: "bad" },
      ],
    };
  }
  if ((repo.behind ?? 0) > 0) {
    return {
      tone: "warn",
      headline: `Brain is a few changes behind your code`,
      detail:
        `${repo.behind} recent changes aren’t in Brain’s memory yet, so answers about them may be incomplete. A quick refresh fixes it.`,
      action: { label: "Refresh now", hint: "Only re-reads the 14 changed files. Under a minute." },
      tiles: [
        { value: String(repo.behind), label: "changes not yet read", tone: "bad" },
        { value: "412", label: "pieces not searchable yet", tone: "warn" },
        { value: "2", label: "context packs to rebuild", tone: "warn" },
        { value: "4h", label: "since Brain last read the code", tone: "ok" },
      ],
    };
  }
  return {
    tone: "ok",
    headline: "All good — Brain knows your latest code",
    detail: "Every file in the latest version is read and searchable. Agents get up-to-date answers. Nothing needs your attention.",
    tiles: [
      { value: "0", label: "changes not yet read", tone: "ok" },
      { value: "0", label: "pieces not searchable yet", tone: "ok" },
      { value: "0", label: "context packs to rebuild", tone: "ok" },
      { value: "38m", label: "since Brain last read the code", tone: "ok" },
    ],
  };
}

// Per-repository corpus sizes so each project in the demo shows plausible,
// distinct totals instead of one shared number.
const corpusStats: Record<string, { files: number; eligible: number; symbols: number; excluded: number }> = {
  "project-brain": { files: 1284, eligible: 18903, symbols: 9412, excluded: 1180 },
  "docs-site": { files: 342, eligible: 4150, symbols: 1860, excluded: 260 },
  "mobile-app": { files: 968, eligible: 12340, symbols: 6710, excluded: 890 },
  "billing-service": { files: 742, eligible: 9687, symbols: 5230, excluded: 640 },
};

export function corpus(repo: Repository): Corpus {
  const behind = repo.behind ?? 0;
  const stats = corpusStats[repo.slug] ?? corpusStats["project-brain"];
  const eligible = stats.eligible;
  const missing = behind > 20 ? 2960 : behind > 0 ? 412 : 0;
  const outdated = behind > 20 ? 4120 : behind > 0 ? 640 : 0;
  const excluded = stats.excluded;
  return {
    files: stats.files,
    chunks: eligible + excluded,
    symbols: stats.symbols,
    embeddings: eligible - missing,
    meters: [
      {
        label: "Searchable",
        value: `${(((eligible - missing) / eligible) * 100).toFixed(1)}%`,
        parts: [
          { tone: "filled", count: eligible - missing, text: `${(eligible - missing).toLocaleString("en-US")} searchable` },
          { tone: "missing", count: missing, text: `${missing.toLocaleString("en-US")} not yet` },
        ],
      },
      {
        label: "Up to date",
        value: `${(eligible - outdated).toLocaleString("en-US")}/${eligible.toLocaleString("en-US")}`,
        parts: [
          { tone: "filled", count: eligible - outdated, text: `${(eligible - outdated).toLocaleString("en-US")} current` },
          { tone: "missing", count: outdated, text: `${outdated.toLocaleString("en-US")} outdated` },
          { tone: "excluded", count: excluded, text: `${excluded.toLocaleString("en-US")} skipped on purpose (generated, vendored)` },
        ],
      },
    ],
  };
}

export const jobs: Job[] = [
  { id: "j-8841", kind: "index.incremental", status: "running", repo: "project-brain", startedAt: "14:02:11", detail: "14 / 31 files" },
  { id: "j-8840", kind: "embeddings.backfill", status: "queued", repo: "project-brain", startedAt: "14:02:11" },
  { id: "j-8839", kind: "context_pack.build", status: "completed", repo: "docs-site", startedAt: "13:58:40", duration: "4.2s" },
  { id: "j-8838", kind: "graph.sync", status: "retrying", repo: "mobile-app", startedAt: "13:51:02", detail: "neo4j timeout · attempt 2/5" },
  { id: "j-8836", kind: "eval.golden_tasks", status: "failed", repo: "project-brain", startedAt: "13:20:47", duration: "2m 14s", detail: "3 of 48 tasks regressed" },
];

export const agentRuns: AgentRun[] = [
  { id: "r-2291", agent: "claude-code", task: "Fix lease renewal in worker queue", status: "completed", tokens: 41230, packHit: true, startedAt: "13:41", duration: "6m 02s", repo: "billing-service" },
  { id: "r-2290", agent: "codex", task: "Split invoice PDF renderer per locale", status: "completed", tokens: 88410, packHit: true, startedAt: "12:58", duration: "14m 40s", repo: "billing-service" },
  { id: "r-2289", agent: "cursor", task: "Add audit CSV export endpoint", status: "failed", tokens: 23004, packHit: false, startedAt: "12:31", duration: "3m 11s", repo: "billing-service" },
  { id: "r-2288", agent: "claude-code", task: "Explain drift in sync engine", status: "completed", tokens: 12880, packHit: true, startedAt: "11:07", duration: "1m 48s", repo: "mobile-app" },
  { id: "r-2287", agent: "devin", task: "Offline queue replay ordering", status: "running", tokens: 30512, packHit: true, startedAt: "10:55", duration: "—", repo: "mobile-app" },
  { id: "r-2286", agent: "codex", task: "Versioned docs layout for v2", status: "timed_out", tokens: 61104, packHit: true, startedAt: "09:44", duration: "1h 00m", repo: "docs-site" },
];

export const events: LedgerEvent[] = [
  { at: "14:02:11", tone: "info", source: "indexer", text: "Incremental index started on master @ d57f014", ref: "run 412" },
  { at: "13:58:44", tone: "ok", source: "context", text: "Pack built for “Fix lease renewal in worker queue” — 9 files, 6.1k tokens", ref: "pk-7f2a" },
  { at: "13:51:02", tone: "warn", source: "graph", text: "Neo4j write timed out after 30s, job will retry", ref: "j-8838" },
  { at: "13:42:19", tone: "ok", source: "mcp", text: "brain.search served 212 calls in the last hour, p95 184ms" },
  { at: "13:20:47", tone: "bad", source: "eval", text: "Golden tasks regressed: 45/48 (was 48/48) after #106", ref: "eval 88" },
  { at: "12:58:03", tone: "ok", source: "memory", text: "Decision recorded: “Stripe webhooks handled out of the request path”", ref: "D-41" },
  { at: "12:31:40", tone: "warn", source: "agent", text: "cursor run ended without a context pack — answered from cold search", ref: "r-2289" },
  { at: "11:02:15", tone: "ok", source: "backup", text: "Nightly restore drill passed — 43,080 chunks restored and verified" },
  { at: "10:40:00", tone: "info", source: "auth", text: "Scoped credential issued to principal “ci-bridge” (core:read)" },
  { at: "09:12:33", tone: "ok", source: "indexer", text: "Index run 411 complete — 1,284 files, 0 errors", ref: "run 411" },
  { at: "03:00:02", tone: "info", source: "maintenance", text: "Nightly maintenance: 4 repositories checked, 1 reindexed" },
];

export const indexRuns: IndexRun[] = [
  { id: 412, revision: "d57f014", trigger: "push", status: "running", completeness: "partial", files: 31, changed: 14, chunks: 0, startedAt: "Oct 2 · 14:02", duration: "—" },
  { id: 411, revision: "5cb365c", trigger: "push", status: "completed", completeness: "complete", files: 1284, changed: 6, chunks: 18903, startedAt: "Oct 2 · 09:12", duration: "48s" },
  { id: 410, revision: "2f2e08e", trigger: "schedule", status: "completed", completeness: "complete", files: 1281, changed: 22, chunks: 18866, startedAt: "Oct 2 · 03:00", duration: "1m 12s" },
  { id: 409, revision: "d23a3da", trigger: "auto-heal", status: "degraded", completeness: "partial", files: 1279, changed: 140, chunks: 18120, startedAt: "Oct 1 · 22:40", duration: "6m 03s" },
  { id: 408, revision: "7fcb844", trigger: "manual", status: "failed", completeness: "aborted", files: 412, changed: 412, chunks: 0, startedAt: "Oct 1 · 18:15", duration: "2m 51s" },
  { id: 407, revision: "46f9c57", trigger: "push", status: "completed", completeness: "complete", files: 1262, changed: 9, chunks: 18640, startedAt: "Oct 1 · 11:30", duration: "51s" },
  { id: 406, revision: "d194224", trigger: "schedule", status: "completed", completeness: "complete", files: 1258, changed: 31, chunks: 18601, startedAt: "Oct 1 · 03:00", duration: "1m 04s" },
];

export const contextPacks: ContextPack[] = [
  { id: "1042", task: "Fix lease renewal in worker queue", budget: "medium", tokens: 6120, files: 9, createdAt: "13:58", stale: true, consumer: "claude-code", memory: { rules: 2, decisions: 2, learnings: 0 }, repo: "billing-service" },
  { id: "1041", task: "Split invoice PDF renderer per locale", budget: "large", tokens: 14880, files: 21, createdAt: "12:57", stale: true, consumer: "codex", memory: { rules: 1, decisions: 0, learnings: 0 }, repo: "billing-service" },
  { id: "1038", task: "Explain drift in sync engine", budget: "small", tokens: 2410, files: 4, createdAt: "11:06", stale: false, consumer: "claude-code", memory: { rules: 0, decisions: 0, learnings: 0 }, repo: "mobile-app" },
  { id: "1037", task: "Offline queue replay ordering", budget: "medium", tokens: 7330, files: 11, createdAt: "10:54", stale: false, consumer: "devin", memory: { rules: 0, decisions: 1, learnings: 0 }, repo: "mobile-app" },
  { id: "1031", task: "Versioned docs layout for v2", budget: "large", tokens: 15990, files: 26, createdAt: "Oct 1", stale: false, consumer: "codex", memory: { rules: 0, decisions: 1, learnings: 0 }, repo: "docs-site" },
  { id: "1029", task: "Retry policy capped at three attempts", budget: "small", tokens: 3102, files: 5, createdAt: "Oct 1", stale: false, consumer: "claude-code", memory: { rules: 0, decisions: 1, learnings: 0 }, repo: "billing-service" },
];

// Demo pack bodies, keyed by pack id above.
export const packContents: Record<string, string> = {
  "1042": `# Context pack — Fix lease renewal in worker queue\n\nDemo pack. Built for billing-service @ 9f949e9.\n\n## Files (9)\n\n- billing/workers/lease.py — lease token acquire/release\n- billing/workers/queue.py — job claim loop\n- billing/db/jobs.py — commit fencing\n- tests/test_lease.py — fencing regression tests\n- billing/payments/retry.py — uses the renewed lease\n- docs/runbooks/worker-ops.md — restart order\n- billing/config.py — lease TTL settings\n- migrations/0041_job_leases.sql — lease table\n- billing/workers/__init__.py — exports\n\n## Decisions carried\n\n- D-38 Invoice numbering moves to a DB sequence\n- D-41 Stripe webhooks handled out of the request path\n\n## Rules delivered\n\n- R-014 Payments code never touches the ORM session directly (block)\n- R-021 Every migration ships a matching down-revision (warn)\n\nBudget: medium · 6,120 tokens.`,
  "1041": `# Context pack — Split invoice PDF renderer per locale\n\nDemo pack. Built for billing-service @ 9f949e9.\n\n## Files (21)\n\nRenderer, locale tables, templates and tests for invoice PDFs.\n\n## Rules delivered\n\n- R-021 Every migration ships a matching down-revision (warn)\n\nBudget: large · 14,880 tokens.`,
  "1038": `# Context pack — Explain drift in sync engine\n\nDemo pack. Built for mobile-app @ bec759d.\n\n## Files (4)\n\n- mobile/sync/engine.kt\n- mobile/sync/conflict.kt\n- mobile/sync/queue.kt\n- docs/sync.md\n\nBudget: small · 2,410 tokens.`,
  "1037": `# Context pack — Offline queue replay ordering\n\nDemo pack. Built for mobile-app @ bec759d.\n\n## Files (11)\n\nOffline mutation queue, replay scheduler, conflict resolver and their tests.\n\n## Decisions carried\n\n- D-27 Offline queue replays mutations in order\n\nBudget: medium · 7,330 tokens.`,
  "1031": `# Context pack — Versioned docs layout for v2\n\nDemo pack. Built for docs-site @ b113aca.\n\n## Files (26)\n\nContent tree, locale overlays, nav builder and link checker.\n\n## Decisions carried\n\n- D-12 Docs versioning drops locale branches\n\nBudget: large · 15,990 tokens.`,
  "1029": `# Context pack — Retry policy capped at three attempts\n\nDemo pack. Built for billing-service @ 9f949e9.\n\n## Files (5)\n\n- billing/payments/retry.py\n- billing/payments/gateway.py\n- tests/test_retry.py\n- billing/config.py\n- docs/payments.md\n\n## Decisions carried\n\n- D-35 Retry policy capped at three attempts\n\nBudget: small · 3,102 tokens.`,
};

export const decisions: Decision[] = [
  { id: "D-41", title: "Stripe webhooks handled out of the request path", status: "accepted", scope: "billing-service", recordedAt: "Oct 2", summary: "Webhook events land in a queue and a worker applies them. A slow Stripe retry can no longer stall checkout requests.", repo: "billing-service" },
  { id: "D-38", title: "Invoice numbering moves to a DB sequence", status: "accepted", scope: "billing-service", recordedAt: "Sep 24", summary: "Replaces the app-side counter that produced duplicates under concurrent workers.", repo: "billing-service" },
  { id: "D-35", title: "Retry policy capped at three attempts", status: "proposed", scope: "billing-service", recordedAt: "Oct 2", summary: "Unbounded retries on declined payments were generating duplicate charge attempts during outages.", repo: "billing-service" },
  { id: "D-29", title: "Session tokens live in Keychain, not AsyncStorage", status: "accepted", scope: "mobile-app", recordedAt: "Sep 19", summary: "AsyncStorage is plain text on rooted devices; Keychain keeps tokens behind the OS keystore.", repo: "mobile-app" },
  { id: "D-27", title: "Offline queue replays mutations in order", status: "accepted", scope: "mobile-app", recordedAt: "Sep 12", summary: "Edits made offline replay oldest-first on reconnect so a rename never lands before its create.", repo: "mobile-app" },
  { id: "D-12", title: "Docs versioning drops locale branches", status: "superseded", scope: "docs-site", recordedAt: "Jul 30", summary: "Replaced by a single content tree with per-page locale overlays; per-locale git branches were unmergeable.", repo: "docs-site" },
];

export const rules: Rule[] = [
  { id: "R-014", rule: "Payments code never touches the ORM session directly", severity: "block", scope: "billing-service/payments/**", delivered30d: 18, caught30d: 2, repo: "billing-service" },
  { id: "R-021", rule: "Every migration ships a matching down-revision", severity: "warn", scope: "billing-service/migrations/**", delivered30d: 11, caught30d: 0, repo: "billing-service" },
  { id: "R-009", rule: "No network calls on the main thread", severity: "warn", scope: "mobile-app/**", delivered30d: 9, caught30d: 3, repo: "mobile-app" },
  { id: "R-030", rule: "Public pages keep headings in sentence case", severity: "advise", scope: "docs-site/content/**", delivered30d: 24, caught30d: 7, repo: "docs-site" },
];

export const insights: Insight[] = [
  { id: "in-71", tone: "bad", kind: "drift", title: "payments/retry.py bypasses the ORM session rule in a new place", evidence: "retry.py:88 — direct session.commit(), rule R-014 forbids it", module: "billing-service/payments", detectedAt: "13:21" },
  { id: "in-70", tone: "warn", kind: "coupling", title: "sync engine ↔ conflict resolver co-change rose to 0.71", evidence: "17 of 24 commits touching sync/engine.kt also touched sync/conflict.kt", module: "mobile-app/sync", detectedAt: "11:40" },
  { id: "in-69", tone: "warn", kind: "hotspot", title: "gateway.py is the top churn file this month", evidence: "38 commits, 2,140 lines changed, 3 authors", module: "billing-service/payments", detectedAt: "Oct 1" },
  { id: "in-67", tone: "warn", kind: "other", title: "Open PR touches payments and migrations in one diff", evidence: "PR #214 changes retry.py and adds migration 0042 — review flagged it as risky", module: "billing-service", detectedAt: "Oct 1" },
  { id: "in-68", tone: "info", kind: "freshness", title: "4 context packs reference files changed since build", evidence: "pk-1042, pk-1041, pk-1030, pk-1028", module: "billing-service", detectedAt: "Oct 1" },
];

export const moduleEdges: ModuleEdge[] = [
  { from: "apps/api", to: "brain/search", weight: 42, violation: false },
  { from: "apps/api", to: "brain/database", weight: 38, violation: false },
  { from: "apps/mcp_server", to: "brain/context", weight: 21, violation: false },
  { from: "brain/workers", to: "brain/indexers", weight: 19, violation: false },
  { from: "brain/workers", to: "brain/database", weight: 17, violation: false },
  { from: "brain/context", to: "brain/retrieval", weight: 15, violation: false },
  { from: "brain/retrieval", to: "brain/embeddings", weight: 12, violation: false },
  { from: "brain/insights", to: "apps/api", weight: 5, violation: true },
  { from: "brain/graph", to: "brain/database", weight: 9, violation: false },
];

// The same 13 tools apps/mcp_server/server.py registers. Names match one to
// one so the demo screen reads like the real server.
export const mcpTools: McpTool[] = [
  { name: "search_code", surface: "remote", calls24h: 4812, p95ms: 184, errorRate: 0.002, status: "ok" },
  { name: "ask_project", surface: "remote", calls24h: 2140, p95ms: 4120, errorRate: 0.008, status: "ok" },
  { name: "find_related_files", surface: "local", calls24h: 902, p95ms: 142, errorRate: 0.001, status: "ok" },
  { name: "prepare_task_context", surface: "remote", calls24h: 611, p95ms: 2310, errorRate: 0.011, status: "ok" },
  { name: "prepare_deep_context", surface: "remote", calls24h: 148, p95ms: 6840, errorRate: 0.020, status: "warn" },
  { name: "impact_analysis", surface: "remote", calls24h: 204, p95ms: 3920, errorRate: 0.034, status: "warn" },
  { name: "review_diff", surface: "local", calls24h: 73, p95ms: 5140, errorRate: 0.081, status: "bad" },
  { name: "record_decision", surface: "remote", calls24h: 38, p95ms: 96, errorRate: 0, status: "ok" },
  { name: "record_rule", surface: "remote", calls24h: 12, p95ms: 88, errorRate: 0, status: "ok" },
  { name: "record_task_lesson", surface: "remote", calls24h: 61, p95ms: 112, errorRate: 0, status: "ok" },
  { name: "record_task_observation", surface: "remote", calls24h: 55, p95ms: 104, errorRate: 0, status: "ok" },
  { name: "record_procedure_outcome", surface: "remote", calls24h: 9, p95ms: 130, errorRate: 0, status: "ok" },
  { name: "get_background_job", surface: "remote", calls24h: 264, p95ms: 74, errorRate: 0.004, status: "ok" },
];

export const reports: Report[] = [
  { id: "golden-task-eval-2026-10-02.md", title: "Golden task eval · 45/48", kind: "eval", createdAt: "Oct 2 · 13:20", size: "212 KB" },
  { id: "search-benchmark-2026-10-02.md", title: "Search benchmark · hit@5 87.4%", kind: "benchmark", createdAt: "Oct 2 · 03:00", size: "96 KB" },
  { id: "restore-drill-2026-10-02.md", title: "Restore drill", kind: "backup", createdAt: "Oct 2 · 11:02", size: "9 KB" },
];

// Demo report bodies, keyed by the report id above.
export const reportContents: Record<string, string> = {
  "golden-task-eval-2026-10-02.md": `# Golden task eval — 45/48\n\nDemo report. A fixed set of 48 real tasks with known right answers, re-run after each change.\n\n## Result\n\n- Passed: **45 of 48**\n- Regressed since last run: **3**\n\n## Regressions\n\n| Task | Expected | Got |\n|---|---|---|\n| Move retry policy into workers | brain/workers/tasks.py | brain/jobs (old layout) |\n| Where are rules enforced? | brain/memory/rule_store.py | apps/api/routers/web.py |\n| OAuth consent page | apps/api/mcp_oauth.py | missed |\n\nRun again after the index on commit \`5cb365c\` finishes.`,
  "search-benchmark-2026-10-02.md": `# Search benchmark — hit@5 87.4%\n\nDemo report. 96 questions with known right answers against the indexed projects.\n\n| Metric | Value |\n|---|---|\n| Right file first | 61.5% |\n| Right file in top 3 | 78.1% |\n| Right file in top 5 | 87.4% |\n| MRR | 0.704 |\n\nMeasured on build \`d57f014\`, corpus sha \`9f2c…e811\`.`,
  "restore-drill-2026-10-02.md": `# Restore drill\n\nDemo report. Nightly backup restored into a scratch database and verified.\n\n- Chunks restored and verified: **43,080**\n- Time to restore: **4m 12s**\n- Integrity check: **passed**\n\nNo action needed.`,
};

export const principals: Principal[] = [
  { id: "p-1", name: "admin", kind: "human", scopes: ["*"], lastSeen: "now", credentials: 1 },
  { id: "p-2", name: "claude-code", kind: "agent", scopes: ["core:read", "memory:write"], lastSeen: "13:41", credentials: 2 },
  { id: "p-3", name: "codex", kind: "agent", scopes: ["core:read"], lastSeen: "12:58", credentials: 1 },
  { id: "p-4", name: "ci-bridge", kind: "service", scopes: ["core:read"], lastSeen: "10:40", credentials: 1 },
  { id: "p-5", name: "telegram-bridge", kind: "service", scopes: ["core:read", "jobs:write"], lastSeen: "Sep 29", credentials: 1 },
];

export const identities: Identity[] = [
  {
    id: 1, name: "admin", kind: "human", disabled: false, createdAt: "Sep 24", activeKeys: 1, scopes: ["principals:write", "setup:write"], lastSeen: "13:58",
    keys: [
      { id: 1, scopes: ["principals:write", "setup:write"], status: "active", created: "Sep 24 · 09:12", expires: "never", lastUsed: "13:58", expiringSoon: false, neverUsed: false },
    ],
  },
  {
    id: 2, name: "claude-code", kind: "agent", disabled: false, createdAt: "Sep 24", activeKeys: 2, scopes: ["core:write", "jobs:read"], lastSeen: "13:41",
    keys: [
      { id: 7, scopes: ["core:write", "jobs:read"], status: "active", created: "Oct 1 · 18:20", expires: "Dec 30 · in 89d", lastUsed: "never", expiringSoon: false, neverUsed: true },
      { id: 3, scopes: ["core:write", "jobs:read"], status: "active", created: "Sep 24 · 09:30", expires: "Oct 9 · in 7d", lastUsed: "13:41", expiringSoon: true, neverUsed: false },
    ],
  },
  {
    id: 3, name: "codex", kind: "agent", disabled: false, createdAt: "Sep 25", activeKeys: 1, scopes: ["core:read"], lastSeen: "12:58",
    keys: [
      { id: 6, scopes: ["core:read"], status: "active", created: "Sep 28 · 11:04", expires: "never", lastUsed: "12:58", expiringSoon: false, neverUsed: false },
      { id: 4, scopes: ["core:read"], status: "revoked", created: "Sep 25 · 16:40", expires: "never", lastUsed: "Sep 28", expiringSoon: false, neverUsed: false },
    ],
  },
  {
    id: 4, name: "ci-bridge", kind: "service", disabled: false, createdAt: "Sep 26", activeKeys: 1, scopes: ["core:read", "jobs:write"], lastSeen: "10:40",
    keys: [
      { id: 5, scopes: ["core:read", "jobs:write"], status: "active", created: "Sep 26 · 10:02", expires: "Sep 26, 2027 · in 359d", lastUsed: "10:40", expiringSoon: false, neverUsed: false },
    ],
  },
  {
    id: 5, name: "old-laptop", kind: "agent", disabled: true, createdAt: "Aug 30", activeKeys: 0, scopes: [], lastSeen: "Sep 12",
    keys: [{ id: 2, scopes: ["core:write", "jobs:read"], status: "expired", created: "Aug 30 · 08:15", expires: "Sep 29", lastUsed: "Sep 12", expiringSoon: false, neverUsed: false }],
  },
];

export const setupSteps: SetupStep[] = [
  { id: "doctor", title: "Run the install preflight", detail: "Checks Postgres, pgvector, Redis, Neo4j and the worker in one pass.", done: true, command: "brain doctor" },
  { id: "repo", title: "Register a repository", detail: "Point Brain at a checkout. It is indexed in place; nothing is copied.", done: true, command: "brain repo add ~/code/project-brain" },
  { id: "index", title: "Build the first index", detail: "Files, symbols and chunks. Embeddings backfill in the background.", done: true, command: "brain index --repo project-brain" },
  { id: "mcp", title: "Connect an agent over MCP", detail: "Writes a project .mcp.json with a scoped, revocable credential.", done: false, command: "brain mcp install --agent claude-code" },
  { id: "pack", title: "Ask for a context pack", detail: "Describe a task; Brain returns the files and rules an agent needs.", done: false, command: "brain pack \"fix lease renewal\"" },
];

export const reranker: RerankerStatus = {
  model: "LFM2-ColBERT-350M",
  state: "ok",
  stateText: "Serving",
  coverage: {
    label: "Ready for precise ranking",
    value: "93.4%",
    parts: [
      { tone: "filled", count: 17655, text: "17,655 encoded" },
      { tone: "missing", count: 1248, text: "1,248 pending" },
    ],
  },
  p50ms: 41,
  p95ms: 118,
  queries24h: 3920,
};

// The memory map: the repository as modules, each split into the chunks Brain
// stores. State per chunk is derived from the repository's condition so the map
// and the verdict always agree.
const moduleSizes: [string, number][] = [
  ["brain/search", 1420], ["brain/indexers", 1310], ["brain/context", 1180], ["brain/database", 1240],
  ["brain/graph", 1650], ["brain/insights", 1890], ["brain/workers", 980], ["brain/embeddings", 760],
  ["brain/retrieval", 890], ["brain/memory", 1020], ["brain/late_interaction", 640], ["brain/lab", 520],
  ["apps/api", 2210], ["apps/mcp_server", 610], ["apps/cli", 430], ["tests", 2140], ["scripts", 380],
  ["docs", 820], ["deploy", 210], ["rules", 120],
];

export function codeModules(repo: Repository): CodeModule[] {
  const severity = (repo.behind ?? 0) > 20 ? 3 : (repo.behind ?? 0) > 0 ? 1 : 0;
  const touched = new Set(
    severity === 3
      ? ["apps/api", "brain/workers", "brain/insights", "brain/graph", "tests", "brain/search", "brain/context"]
      : severity === 1
        ? ["apps/api", "brain/workers", "tests"]
        : [],
  );
  return moduleSizes.map(([name, chunks], i) => {
    const excluded = name === "docs" ? Math.round(chunks * 0.6) : name === "scripts" ? Math.round(chunks * 0.3) : 0;
    const hot = touched.has(name);
    const outdated = hot ? Math.round(chunks * (severity === 3 ? 0.34 : 0.12)) : 0;
    const missing = hot ? Math.round(chunks * (severity === 3 ? 0.22 : 0.06)) + (i % 3) * 4 : 0;
    return { name, chunks, current: chunks - excluded - outdated - missing, outdated, missing, excluded };
  });
}
