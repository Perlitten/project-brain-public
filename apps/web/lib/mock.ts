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
  { id: 1, slug: "project-brain", name: "project-brain", path: "/srv/repos/project-brain", branch: "master", head: "d57f014", behind: 3 },
  { id: 2, slug: "mycelium", name: "MYCELIUM", path: "/srv/repos/MYCELIUM", branch: "main", head: "b113aca", behind: 0 },
  { id: 3, slug: "forex-bot", name: "forex-bot", path: "/srv/repos/forex-bot", branch: "master", head: "bec759d", behind: 0 },
  { id: 4, slug: "billing-service", name: "billing-service", path: "/srv/repos/billing-service", branch: "main", head: "9f949e9", behind: 41 },
];

export function findRepository(slug?: string | null): Repository {
  return repositories.find((r) => r.slug === slug) ?? repositories[0];
}

export function condition(repo: Repository): Condition {
  if (repo.behind > 20) {
    return {
      tone: "bad",
      headline: `Brain is reading an old copy of your code`,
      detail:
        `Your code changed ${repo.behind} times since Brain last read it. AI agents asking Brain for help will get outdated answers until you refresh it.`,
      action: { label: "Refresh Brain’s copy", hint: "Re-reads the whole repository. Takes about 6 minutes." },
      tiles: [
        { value: String(repo.behind), label: "changes not yet read", tone: "bad" },
        { value: "2,960", label: "pieces not searchable yet", tone: "warn" },
        { value: "9", label: "agent briefings to rebuild", tone: "warn" },
        { value: "6d", label: "since Brain last read the code", tone: "bad" },
      ],
    };
  }
  if (repo.behind > 0) {
    return {
      tone: "warn",
      headline: `Brain is a few changes behind your code`,
      detail:
        `${repo.behind} recent changes aren’t in Brain’s memory yet, so answers about them may be incomplete. A quick refresh fixes it.`,
      action: { label: "Refresh now", hint: "Only re-reads the 14 changed files. Under a minute." },
      tiles: [
        { value: String(repo.behind), label: "changes not yet read", tone: "bad" },
        { value: "412", label: "pieces not searchable yet", tone: "warn" },
        { value: "2", label: "agent briefings to rebuild", tone: "warn" },
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
      { value: "0", label: "agent briefings to rebuild", tone: "ok" },
      { value: "38m", label: "since Brain last read the code", tone: "ok" },
    ],
  };
}

export function corpus(repo: Repository): Corpus {
  const behind = repo.behind;
  const eligible = 18903;
  const missing = behind > 20 ? 2960 : behind > 0 ? 412 : 0;
  const outdated = behind > 20 ? 4120 : behind > 0 ? 640 : 0;
  const excluded = 1180;
  return {
    files: 1284,
    chunks: eligible + excluded,
    symbols: 9412,
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
  { id: "j-8839", kind: "context_pack.build", status: "completed", repo: "MYCELIUM", startedAt: "13:58:40", duration: "4.2s" },
  { id: "j-8838", kind: "graph.sync", status: "retrying", repo: "forex-bot", startedAt: "13:51:02", detail: "neo4j timeout · attempt 2/5" },
  { id: "j-8836", kind: "eval.golden_tasks", status: "failed", repo: "project-brain", startedAt: "13:20:47", duration: "2m 14s", detail: "3 of 48 tasks regressed" },
];

export const agentRuns: AgentRun[] = [
  { id: "r-2291", agent: "claude-code", task: "Fix lease renewal in worker queue", status: "completed", tokens: 41230, packHit: true, startedAt: "13:41", duration: "6m 02s" },
  { id: "r-2290", agent: "codex", task: "Split dashboard shell.js per feature", status: "completed", tokens: 88410, packHit: true, startedAt: "12:58", duration: "14m 40s" },
  { id: "r-2289", agent: "cursor", task: "Add audit CSV export endpoint", status: "failed", tokens: 23004, packHit: false, startedAt: "12:31", duration: "3m 11s" },
  { id: "r-2288", agent: "claude-code", task: "Explain drift in brain/graph", status: "completed", tokens: 12880, packHit: true, startedAt: "11:07", duration: "1m 48s" },
  { id: "r-2287", agent: "devin", task: "Pin Flask corpus for eval", status: "running", tokens: 30512, packHit: true, startedAt: "10:55", duration: "—" },
];

export const events: LedgerEvent[] = [
  { at: "14:02:11", tone: "info", source: "indexer", text: "Incremental index started on master @ d57f014", ref: "run 412" },
  { at: "13:58:44", tone: "ok", source: "context", text: "Pack built for “Fix lease renewal in worker queue” — 9 files, 6.1k tokens", ref: "pk-7f2a" },
  { at: "13:51:02", tone: "warn", source: "graph", text: "Neo4j write timed out after 30s, job will retry", ref: "j-8838" },
  { at: "13:42:19", tone: "ok", source: "mcp", text: "brain.search served 212 calls in the last hour, p95 184ms" },
  { at: "13:20:47", tone: "bad", source: "eval", text: "Golden tasks regressed: 45/48 (was 48/48) after #106", ref: "eval 88" },
  { at: "12:58:03", tone: "ok", source: "memory", text: "Decision recorded: “Dashboard shell split per feature module”", ref: "ADR-0011" },
  { at: "12:31:40", tone: "warn", source: "agent", text: "cursor run ended without a context pack — answered from cold search", ref: "r-2289" },
  { at: "11:02:15", tone: "ok", source: "backup", text: "Nightly restore drill passed — 18,903 chunks restored and verified" },
  { at: "10:40:00", tone: "info", source: "auth", text: "Scoped credential issued to principal “n8n-bridge” (core:read)" },
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
  { id: "pk-7f2a", task: "Fix lease renewal in worker queue", budget: "medium", tokens: 6120, files: 9, createdAt: "13:58", stale: true, consumer: "claude-code" },
  { id: "pk-7f1c", task: "Split dashboard shell.js per feature", budget: "large", tokens: 14880, files: 21, createdAt: "12:57", stale: true, consumer: "codex" },
  { id: "pk-7e90", task: "Explain drift in brain/graph", budget: "small", tokens: 2410, files: 4, createdAt: "11:06", stale: false, consumer: "claude-code" },
  { id: "pk-7e41", task: "Pin Flask corpus for eval", budget: "medium", tokens: 7330, files: 11, createdAt: "10:54", stale: false, consumer: "devin" },
  { id: "pk-7d02", task: "Principal and credential lifecycle endpoints", budget: "large", tokens: 15990, files: 26, createdAt: "Oct 1", stale: false, consumer: "codex" },
  { id: "pk-7c77", task: "Backup restore drill via psql", budget: "small", tokens: 3102, files: 5, createdAt: "Oct 1", stale: false, consumer: "claude-code" },
];

export const decisions: Decision[] = [
  { id: "ADR-0011", title: "Dashboard shell split per feature module", status: "accepted", scope: "apps/api/static", recordedAt: "Oct 2", summary: "One module per shell feature; no cross-module globals. Keeps each file under 300 lines and independently cacheable." },
  { id: "ADR-0010", title: "Scoped, revocable credentials for every principal", status: "accepted", scope: "apps/api/auth", recordedAt: "Sep 24", summary: "API keys become principals with domain scopes. The single shared key is retired after 0.9." },
  { id: "ADR-0009", title: "Postgres fencing for job commits", status: "accepted", scope: "brain/workers", recordedAt: "Sep 19", summary: "A job commits only while it holds the lease token. Stale workers cannot overwrite a newer result." },
  { id: "ADR-0008", title: "Frontend on Vercel behind a server-side proxy", status: "proposed", scope: "apps/web", recordedAt: "Oct 2", summary: "Next.js app talks to the VPS API only from route handlers; the API key never reaches the browser." },
  { id: "ADR-0003", title: "Graph schema v2 identity", status: "superseded", scope: "brain/graph", recordedAt: "Jul 30", summary: "Replaced by v3 identity: stable symbol ids across renames." },
];

export const rules: Rule[] = [
  { id: "R-014", rule: "brain/ must not import apps/", severity: "block", scope: "brain/**", hits30d: 2 },
  { id: "R-021", rule: "Tests raise settings before importing app modules", severity: "warn", scope: "tests/**", hits30d: 0 },
  { id: "R-009", rule: "rules/golden_tasks.yaml changes with every module move", severity: "warn", scope: "brain/**", hits30d: 3 },
  { id: "R-030", rule: "No raw colour or length literals in templates", severity: "advise", scope: "apps/api/templates/**", hits30d: 7 },
  { id: "R-031", rule: "Secrets never enter a field value", severity: "block", scope: "apps/**", hits30d: 0 },
];

export const insights: Insight[] = [
  { id: "in-71", tone: "bad", kind: "drift", title: "brain/insights imports apps.api.helpers in a new place", evidence: "proactive.py:212 — 5th lazy import, rule R-014 allows 4", module: "brain/insights", detectedAt: "13:21" },
  { id: "in-70", tone: "warn", kind: "coupling", title: "workers ↔ database co-change rose to 0.71", evidence: "17 of 24 commits touching workers/tasks.py also touched database/", module: "brain/workers", detectedAt: "11:40" },
  { id: "in-69", tone: "warn", kind: "hotspot", title: "routers/dashboard.py is the top churn file this month", evidence: "38 commits, 2,140 lines changed, 3 authors", module: "apps/api/routers", detectedAt: "Oct 1" },
  { id: "in-68", tone: "info", kind: "freshness", title: "4 context packs reference files changed since build", evidence: "pk-7f2a, pk-7f1c, pk-7b10, pk-7a44", module: "brain/context", detectedAt: "Oct 1" },
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

export const mcpTools: McpTool[] = [
  { name: "brain.search", surface: "remote", calls24h: 4812, p95ms: 184, errorRate: 0.002, status: "ok" },
  { name: "brain.prepare_task_context", surface: "remote", calls24h: 611, p95ms: 2310, errorRate: 0.011, status: "ok" },
  { name: "brain.impact_analysis", surface: "remote", calls24h: 204, p95ms: 3920, errorRate: 0.034, status: "warn" },
  { name: "brain.record_decision", surface: "remote", calls24h: 38, p95ms: 96, errorRate: 0, status: "ok" },
  { name: "brain.review_diff", surface: "local", calls24h: 73, p95ms: 5140, errorRate: 0.081, status: "bad" },
  { name: "brain.find_related_files", surface: "local", calls24h: 902, p95ms: 142, errorRate: 0.001, status: "ok" },
];

export const reports: Report[] = [
  { id: "rp-88", title: "Golden task eval · 45/48", kind: "eval", createdAt: "Oct 2 · 13:20", size: "212 KB" },
  { id: "rp-87", title: "Token economy A/B · 50-task corpus", kind: "benchmark", createdAt: "Sep 30", size: "1.4 MB" },
  { id: "rp-86", title: "Nightly maintenance", kind: "maintenance", createdAt: "Oct 2 · 03:00", size: "18 KB" },
  { id: "rp-85", title: "Restore drill", kind: "backup", createdAt: "Oct 2 · 11:02", size: "9 KB" },
  { id: "rp-84", title: "Architecture drift · master vs HEAD", kind: "drift", createdAt: "Oct 1", size: "64 KB" },
];

export const principals: Principal[] = [
  { id: "p-1", name: "andrei", kind: "human", scopes: ["*"], lastSeen: "now", credentials: 1 },
  { id: "p-2", name: "claude-code", kind: "agent", scopes: ["core:read", "memory:write"], lastSeen: "13:41", credentials: 2 },
  { id: "p-3", name: "codex", kind: "agent", scopes: ["core:read"], lastSeen: "12:58", credentials: 1 },
  { id: "p-4", name: "n8n-bridge", kind: "service", scopes: ["core:read"], lastSeen: "10:40", credentials: 1 },
  { id: "p-5", name: "telegram-bridge", kind: "service", scopes: ["core:read", "jobs:write"], lastSeen: "Sep 29", credentials: 1 },
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
  ndcgLift: "+0.071",
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
  const severity = repo.behind > 20 ? 3 : repo.behind > 0 ? 1 : 0;
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
