// Where the screens get their data. With BRAIN_API_URL and BRAIN_API_KEY set
// on the server, every getter reads the Brain API (lib/api.ts, keyed,
// server-only) and maps the answer to lib/types.ts. Without them the screens
// show demo data. In live mode an unavailable endpoint yields an honest empty
// value — never demo numbers. Components never import mock.ts directly.
import { connection } from "next/server";
import { cache } from "react";
import { apiConfigured, brainFetch } from "./api";
import * as mock from "./mock";
import { getListQuery, type ListPaging, type ListQuery, pagingFrom } from "./list-query";
import type {
  AccessKey,
  AccessView,
  AgentRun,
  CodeModule,
  Condition,
  ConditionTile,
  ContextPack,
  Corpus,
  Decision,
  Identity,
  IndexRun,
  Insight,
  Job,
  JobStatus,
  LedgerEvent,
  McpTool,
  Meter,
  MeterTone,
  ModuleEdge,
  Principal,
  Report,
  Repository,
  RerankerStatus,
  Rule,
  SetupStep,
  Tone,
} from "./types";

export type DataSource = "demo" | "live";

export const dataSource: DataSource = apiConfigured ? "live" : "demo";

// ---------------------------------------------------------------------------
// Live plumbing

// Opting into dynamic rendering before any request: a live page is never
// prerendered at build time (no API call during `next build`). It must stay
// outside the try below, or the prerender bail-out would be swallowed.
async function live<T>(empty: T, load: () => Promise<T | null>): Promise<T> {
  await connection();
  try {
    return (await load()) ?? empty;
  } catch (err) {
    console.error("[brain-data] mapping failed:", err);
    return empty;
  }
}

type Json = Record<string, unknown>;
const obj = (v: unknown): Json => (v && typeof v === "object" && !Array.isArray(v) ? (v as Json) : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown, fallback = ""): string => (typeof v === "string" ? v : typeof v === "number" ? String(v) : fallback);
const num = (v: unknown): number | undefined => (typeof v === "number" && Number.isFinite(v) ? v : undefined);
const int = (v: unknown, fallback = 0): number => num(v) ?? fallback;
/** A list either bare or wrapped as `{ [key]: [...] }`. */
const list = (v: unknown, key: string): Json[] => (Array.isArray(v) ? v : arr(obj(v)[key])).map(obj);

// ---------------------------------------------------------------------------
// Formatting (plain words, same shapes as the demo strings)

const tz = process.env.BRAIN_TIMEZONE || "UTC";
const dayFmt = new Intl.DateTimeFormat("en-US", { timeZone: tz, month: "short", day: "numeric" });
const clockFmt = new Intl.DateTimeFormat("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const hmFmt = new Intl.DateTimeFormat("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false });

function parseDate(v: unknown): Date | null {
  if (typeof v !== "string" && typeof v !== "number") return null;
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? null : d;
}
const isToday = (d: Date) => dayFmt.format(d) === dayFmt.format(new Date());
/** "14:02:11" today, "Oct 1 · 14:02" before. */
const fmtClock = (v: unknown) => {
  const d = parseDate(v);
  if (!d) return "—";
  return isToday(d) ? clockFmt.format(d) : `${dayFmt.format(d)} · ${hmFmt.format(d)}`;
};
/** "13:58" today, "Oct 1" before. */
const fmtShort = (v: unknown) => {
  const d = parseDate(v);
  if (!d) return "—";
  return isToday(d) ? hmFmt.format(d) : dayFmt.format(d);
};
/** "Oct 2 · 14:02". */
const fmtWhen = (v: unknown) => {
  const d = parseDate(v);
  return d ? `${dayFmt.format(d)} · ${hmFmt.format(d)}` : "—";
};
function fmtDuration(seconds: number | undefined): string {
  if (seconds === undefined || seconds < 0) return "—";
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1).replace(/\.0$/, "") : Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m ${String(Math.round(seconds % 60)).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}
const durationOf = (v: unknown) => (typeof v === "string" && v ? v : fmtDuration(num(v)));
function between(start: unknown, end: unknown): string {
  const a = parseDate(start);
  const b = parseDate(end);
  return a && b ? fmtDuration((b.getTime() - a.getTime()) / 1000) : "—";
}
function fmtAge(seconds: number): string {
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}
function fmtBytes(bytes: number | undefined): string {
  if (bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
const fmtN = (n: number) => n.toLocaleString("en-US");
const sentence = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);
const words = (s: string) => s.replace(/[._]+/g, " ").trim();

function jobStatus(v: unknown): JobStatus {
  const s = str(v).toLowerCase();
  if (["completed", "complete", "success", "succeeded", "passed", "done", "ok"].includes(s)) return "completed";
  if (["failed", "failure", "fail"].includes(s)) return "failed";
  if (["error", "errored"].includes(s)) return "error";
  if (["timed_out", "timeout", "expired"].includes(s)) return "timed_out";
  if (["running", "in_progress", "started", "indexing", "processing", "claimed", "artifacted", "memory_updated", "validating", "acceptance_pending"].includes(s))
    return "running";
  if (["queued", "pending", "created", "routed", "scheduled", "waiting"].includes(s)) return "queued";
  if (["cancelled", "canceled", "aborted"].includes(s)) return "cancelled";
  if (["retrying", "retry"].includes(s)) return "retrying";
  if (s === "skipped") return "skipped";
  return "degraded";
}

function rawTaskStatus(v: string): string {
  if (v === "completed") return "passed";
  if (v === "running") return "running";
  if (v === "queued") return "queued";
  return v;
}

function severityTone(v: unknown): Tone {
  const s = str(v).toLowerCase();
  if (["critical", "high", "error", "bad", "block", "blocker"].includes(s)) return "bad";
  if (["medium", "warn", "warning", "moderate"].includes(s)) return "warn";
  if (["ok", "good", "success"].includes(s)) return "ok";
  return "info";
}

// ---------------------------------------------------------------------------
// Repositories

type RawFreshness = {
  branch?: string | null;
  indexed_commit?: string | null;
  commits_behind?: number | null;
  status?: string | null;
  age_seconds?: number | null;
  note?: string | null;
};
type RawRepo = {
  id: number;
  name?: string;
  path?: string;
  indexing_status?: string | null;
  last_indexed_commit?: string | null;
  last_indexed_at?: string | null;
  freshness?: RawFreshness | null;
};

const fetchRepos = cache(async (): Promise<RawRepo[] | null> => {
  const data = await brainFetch<{ repositories?: RawRepo[] }>("/repositories");
  if (!data) return null;
  return list(data, "repositories").filter((r) => num(r.id) !== undefined) as unknown as RawRepo[];
});

function toRepository(r: RawRepo): Repository {
  const f = obj(r.freshness);
  const commit = str(f.indexed_commit) || str(r.last_indexed_commit);
  return {
    id: r.id,
    slug: String(r.id),
    name: str(r.name) || `repository ${r.id}`,
    path: str(r.path),
    branch: str(f.branch),
    head: commit.startsWith("snapshot:") ? commit.slice(9, 16) : commit.slice(0, 7),
    behind: num(f.commits_behind) ?? null,
    freshness: str(f.status) || "unknown",
  };
}

const noRepository: Repository = { id: 0, slug: "", name: "No repository", path: "", branch: "", head: "", behind: null, freshness: "unindexed" };

const pickRepo = (repos: RawRepo[] | null, slug?: string | null) =>
  slug ? repos?.find((r) => String(r.id) === slug) ?? null : repos?.[0] ?? null;

export type Paged<T> = { items: T[]; paging: ListPaging };
const queryString = (q: ListQuery) => {
  const p = new URLSearchParams({ page: String(q.page), page_size: String(q.size) });
  if (q.q) p.set("q", q.q);
  if (q.status) p.set("status", q.status);
  return p.toString();
};
const pageResult = <T>(data: Json, key: string, q: ListQuery, map: (v: Json) => T): Paged<T> => ({
  items: list(data, key).map(map),
  paging: pagingFrom(data, q),
});

// ---------------------------------------------------------------------------
// Overview (new /api/web router; may be missing on older APIs)

type RawOverview = Json;
const fetchOverview = cache(
  (repositoryId?: number): Promise<RawOverview | null> =>
    brainFetch<RawOverview>(`/api/web/overview${repositoryId ? `?repository_id=${repositoryId}` : ""}`, { fresh: true }),
);

function toJob(j: Json, repoName: string): Job {
  return {
    id: str(j.id, "—"),
    kind: str(j.kind, "job"),
    status: jobStatus(j.status),
    repo: str(j.repo) || str(j.repository) || repoName,
    startedAt: fmtClock(j.started_at),
    duration: j.duration == null && j.duration_seconds == null ? undefined : durationOf(j.duration ?? j.duration_seconds),
    detail: str(j.detail) || undefined,
  };
}

async function loadJobs(repositoryId?: number, repoName = ""): Promise<Job[] | null> {
  const o = await fetchOverview(repositoryId);
  if (!o) return null;
  const name = repoName || str(obj(o.repository).name);
  return list(o.jobs ?? o.recent_jobs, "jobs").map((j) => toJob(j, name));
}

const meterTones: MeterTone[] = ["filled", "missing", "excluded"];
function toMeter(m: Json): Meter | null {
  const parts = arr(m.parts)
    .map(obj)
    .map((p) => ({
      tone: (meterTones as string[]).includes(str(p.tone)) ? (p.tone as MeterTone) : "missing",
      count: int(p.count),
      text: str(p.text),
    }));
  const label = str(m.label);
  return label && parts.length ? { label, value: str(m.value), parts } : null;
}

/** Coverage either as ready-made meters or as raw counts. */
function coverageMeters(o: Json): Meter[] {
  const ready = arr(o.meters ?? (Array.isArray(o.coverage) ? o.coverage : undefined))
    .map(obj)
    .map(toMeter)
    .filter((m): m is Meter => m !== null);
  if (ready.length) return ready;
  // /api/web/overview: {eligible_chunks, vector:{populated}, retrieval:{current},
  // missing, stale, incompatible, excluded}
  const c = obj(o.coverage);
  const eligible = num(c.eligible_chunks) ?? num(c.eligible);
  if (!eligible) return [];
  const populated = num(obj(c.vector).populated) ?? num(c.searchable);
  const missing = num(c.missing) ?? (populated !== undefined ? Math.max(0, eligible - populated) : undefined);
  const current = num(obj(c.retrieval).current);
  const stale = num(c.stale);
  const incompatible = num(c.incompatible);
  const outdated =
    current !== undefined
      ? Math.max(0, eligible - current - (missing ?? 0))
      : stale !== undefined || incompatible !== undefined
        ? (stale ?? 0) + (incompatible ?? 0)
        : undefined;
  const excluded = int(c.excluded);
  const meters: Meter[] = [];
  if (missing !== undefined) {
    const have = Math.max(0, eligible - missing);
    meters.push({
      label: "Searchable",
      value: `${((have / eligible) * 100).toFixed(1)}%`,
      parts: [
        { tone: "filled", count: have, text: `${fmtN(have)} searchable` },
        { tone: "missing", count: missing, text: `${fmtN(missing)} not yet` },
      ],
    });
  }
  if (outdated !== undefined) {
    const upToDate = current ?? Math.max(0, eligible - outdated - (missing ?? 0));
    const notCurrent = Math.max(0, eligible - upToDate);
    meters.push({
      label: "Up to date",
      value: `${fmtN(upToDate)}/${fmtN(eligible)}`,
      parts: [
        { tone: "filled", count: upToDate, text: `${fmtN(upToDate)} current` },
        { tone: "missing", count: notCurrent, text: `${fmtN(notCurrent)} outdated or not read` },
        ...(excluded ? [{ tone: "excluded" as const, count: excluded, text: `${fmtN(excluded)} skipped on purpose (generated, vendored)` }] : []),
      ],
    });
  }
  return meters;
}

// ---------------------------------------------------------------------------
// Condition: the verdict, derived from real signals in plain words

function deriveCondition(
  repo: RawRepo | null,
  reposAvailable: boolean,
  jobs: Job[] | null,
  insights: Insight[] | null,
): Condition {
  const dash = (label: string): ConditionTile => ({ value: "—", label, tone: "idle" });
  if (!reposAvailable) {
    return {
      tone: "idle",
      headline: "Brain didn’t answer",
      detail:
        "The dashboard couldn’t get data from the Brain API, so there is nothing to show yet. Check that the API is running and that BRAIN_API_URL and BRAIN_API_KEY are correct.",
      tiles: [dash("changes not yet read"), dash("since Brain last read the code"), dash("background jobs failed"), dash("serious problems found")],
    };
  }
  if (!repo) {
    return {
      tone: "info",
      headline: "No repository connected yet",
      detail: "Brain hasn’t been pointed at any code. Register a repository and run a first index to see its condition here.",
      tiles: [dash("changes not yet read"), dash("since Brain last read the code"), dash("background jobs failed"), dash("serious problems found")],
    };
  }

  const f = obj(repo.freshness);
  const status = str(f.status);
  const behind = num(f.commits_behind);
  let age = num(f.age_seconds);
  if (age === undefined) {
    const at = parseDate(repo.last_indexed_at);
    if (at) age = Math.max(0, (Date.now() - at.getTime()) / 1000);
  }
  const failed = jobs?.filter((j) => j.status === "failed" || j.status === "error").length;
  const serious = insights?.filter((i) => i.tone === "bad").length;

  const tiles: ConditionTile[] = [
    behind === undefined
      ? dash("changes not yet read")
      : { value: fmtN(behind), label: "changes not yet read", tone: behind > 0 ? "bad" : "ok" },
    age === undefined
      ? dash("since Brain last read the code")
      : { value: fmtAge(age), label: "since Brain last read the code", tone: age < 86400 ? "ok" : age < 7 * 86400 ? "warn" : "bad" },
    failed === undefined
      ? dash("background jobs failed")
      : { value: String(failed), label: "background jobs failed", tone: failed ? "bad" : "ok" },
    serious === undefined
      ? dash("serious problems found")
      : { value: String(serious), label: "serious problems found", tone: serious ? "bad" : "ok" },
  ];

  const base = { tiles };
  if (status === "unindexed") {
    return {
      ...base,
      tone: "bad",
      headline: "Brain hasn’t read this repository yet",
      detail: "There is no finished index for this code, so AI agents asking Brain about it get no answers. Run a first index to fix it.",
    };
  }
  if (status === "source_missing") {
    return {
      ...base,
      tone: "warn",
      headline: "Brain can’t see this repository’s files",
      detail: "The checkout Brain reads from is missing on the server, so it can’t tell whether its copy is current. Answers come from the last copy it read.",
    };
  }
  if ((behind ?? 0) > 20 || (status === "stale" && (behind ?? 0) > 0)) {
    return {
      ...base,
      tone: "bad",
      headline: "Brain is reading an old copy of your code",
      detail: `Your code changed ${fmtN(behind ?? 0)} times since Brain last read it. AI agents asking Brain for help will get outdated answers until it is refreshed.`,
    };
  }
  if ((behind ?? 0) > 0 || status === "behind") {
    return {
      ...base,
      tone: "warn",
      headline: "Brain is a few changes behind your code",
      detail: `${behind ? `${fmtN(behind)} recent change${behind > 1 ? "s aren’t" : " isn’t"}` : "Recent changes aren’t"} in Brain’s memory yet, so answers about them may be incomplete. A refresh fixes it.`,
    };
  }
  if (status === "stale") {
    return {
      ...base,
      tone: "warn",
      headline: "Brain’s copy of your code is getting old",
      detail: "Brain hasn’t re-read this repository in a while. Answers are probably fine, but a refresh makes sure they are current.",
    };
  }
  if (failed) {
    return {
      ...base,
      tone: "warn",
      headline: "Some background work failed",
      detail: `${failed} background job${failed > 1 ? "s" : ""} ended with an error recently. Brain still answers, but part of its memory may not have been updated. See Activity for details.`,
    };
  }
  if (serious) {
    return {
      ...base,
      tone: "warn",
      headline: "Brain is up to date, but found problems worth a look",
      detail: `${serious} finding${serious > 1 ? "s break" : " breaks"} a rule your team set. See Findings for what changed and where.`,
    };
  }
  if (status !== "current") {
    return {
      ...base,
      tone: "info",
      headline: "Brain can’t tell if its copy is current",
      detail: str(f.note) || "Brain couldn’t compare its copy with the latest code. Answers come from the last version it read.",
    };
  }
  return {
    ...base,
    tone: "ok",
    headline: "All good — Brain knows your latest code",
    detail: "Every change is read and searchable. Agents get up-to-date answers. Nothing needs your attention.",
  };
}

// ---------------------------------------------------------------------------
// Insights

const insightKind = (t: string): Insight["kind"] => {
  const s = t.toLowerCase();
  if (s.includes("drift")) return "drift";
  if (s.includes("coupl") || s.includes("cycle")) return "coupling";
  if (s.includes("hotspot") || s.includes("churn")) return "hotspot";
  if (s.includes("fresh") || s.includes("stale")) return "freshness";
  return "other";
};

function evidenceText(e: unknown): string {
  if (typeof e === "string") return e;
  const o = obj(e);
  const where = [str(o.file) || str(o.path), str(o.line)].filter(Boolean).join(":");
  const what = str(o.detail) || str(o.summary) || str(o.message) || str(o.text);
  return [where, what].filter(Boolean).join(" — ");
}

async function loadInsights(): Promise<Insight[] | null> {
  const data = await brainFetch<Json>("/insights?limit=100");
  if (!data) return null;
  return list(data, "insights")
    .filter((i) => !["resolved", "dismissed", "closed", "archived"].includes(str(i.status).toLowerCase()))
    .map((i) => {
      const evidence = arr(i.evidence);
      const first = obj(evidence[0]);
      return {
        id: str(i.id),
        tone: severityTone(i.severity),
        kind: insightKind(str(i.insight_type)),
        title: str(i.title, "Untitled finding"),
        evidence: evidenceText(evidence[0]) || str(i.summary),
        module: str(first.module) || str(first.file) || str(first.path) || str(i.source) || "—",
        detectedAt: fmtShort(i.last_seen_at ?? i.created_at),
      };
    });
}

// ---------------------------------------------------------------------------
// Ledger events (the API pages oldest-first; we want the newest)

function eventTone(e: Json): Tone {
  const s = `${str(e.event_type)} ${str(e.action)} ${str(e.new_state)}`.toLowerCase();
  if (/(fail|error|reject|denied|violation|broken|invalid)/.test(s)) return "bad";
  if (/(warn|retry|degrad|stale|timeout|timed_out|revok|disabl|cancel)/.test(s)) return "warn";
  if (/(complet|pass|success|succeed|approv|accept|verified|restor|built|recorded)/.test(s)) return "ok";
  return "info";
}

function toEvent(e: Json): LedgerEvent {
  const type = str(e.event_type);
  const what = sentence(words(str(e.action) || type || "event"));
  const subject = [words(str(e.entity_type)), str(e.entity_id)].filter(Boolean).join(" ");
  const transition = str(e.previous_state) && str(e.new_state) ? ` (${str(e.previous_state)} → ${str(e.new_state)})` : "";
  const reason = str(e.reason);
  const ref = str(e.entity_id);
  return {
    at: fmtClock(e.timestamp_utc ?? e.at ?? e.created_at),
    tone: eventTone(e),
    source: type.split(".")[0] || words(str(e.entity_type)) || str(e.actor_type) || "brain",
    text: `${what}${subject && !str(e.action) ? "" : subject ? ` — ${subject}` : ""}${transition}${reason ? `: ${reason}` : ""}`,
    ref: ref ? (ref.length > 18 ? `${ref.slice(0, 16)}…` : ref) : undefined,
  };
}

async function loadEvents(limit = 50, repositoryPath?: string): Promise<LedgerEvent[] | null> {
  const scope = repositoryPath ? `&repository_path=${encodeURIComponent(repositoryPath)}` : "";
  const head = await brainFetch<Json>(`/ledger/events?limit=1${scope}`, { fresh: true });
  if (!head) return null;
  const total = int(head.total);
  if (total <= 1) return list(head, "events").map(toEvent);
  const offset = Math.max(0, total - limit);
  const page = await brainFetch<Json>(`/ledger/events?limit=${limit}&offset=${offset}${scope}`, { fresh: true });
  if (!page) return null;
  return list(page, "events").reverse().map(toEvent);
}

// ---------------------------------------------------------------------------
// Getters (same signatures as before; demo branch unchanged)

export const getRepositories = async (): Promise<Repository[]> => {
  if (!apiConfigured) return mock.repositories;
  return live<Repository[]>([], async () => (await fetchRepos())?.map(toRepository) ?? null);
};

export const getRepository = async (slug?: string | null): Promise<Repository> => {
  if (!apiConfigured) return mock.findRepository(slug);
  return live(noRepository, async () => {
    const r = pickRepo(await fetchRepos(), slug);
    return r ? toRepository(r) : null;
  });
};

export const getCondition = async (slug?: string | null): Promise<Condition> => {
  if (!apiConfigured) return mock.condition(mock.findRepository(slug));
  return live(deriveCondition(null, false, null, null), async () => {
    const repos = await fetchRepos();
    const repo = pickRepo(repos, slug);
    const [jobs, insights] = await Promise.all([
      repo ? loadJobs(repo.id, str(repo.name)) : Promise.resolve(null),
      Promise.resolve(null),
    ]);
    return deriveCondition(repo, repos !== null, jobs, insights);
  });
};

export const getCorpus = async (slug?: string | null): Promise<Corpus> => {
  if (!apiConfigured) return mock.corpus(mock.findRepository(slug));
  const empty: Corpus = { files: 0, chunks: 0, symbols: 0, embeddings: 0, meters: [] };
  return live(empty, async () => {
    const repo = pickRepo(await fetchRepos(), slug);
    const overview = repo ? await fetchOverview(repo.id) : null;
    if (!repo) return null;
    let counts = obj(overview?.counts);
    if (!Object.keys(counts).length) {
      // Older APIs: global counts only, no coverage meters.
      counts = obj((await brainFetch<Json>("/api/status/indexing"))?.counts);
    }
    return {
      files: int(counts.files),
      chunks: int(counts.chunks),
      symbols: int(counts.symbols),
      embeddings: int(counts.embeddings),
      meters: overview ? coverageMeters(overview) : [],
    };
  });
};

export const getJobs = async (slug?: string | null): Promise<Job[]> => {
  if (!apiConfigured) return mock.jobs;
  return live<Job[]>([], async () => {
    const repo = pickRepo(await fetchRepos(), slug);
    if (slug && !repo) return [];
    return loadJobs(repo?.id, str(repo?.name));
  });
};

export const getAgentRuns = async (slug?: string | null): Promise<AgentRun[]> => {
  if (!apiConfigured) return mock.agentRuns;
  return live<AgentRun[]>([], async () => {
    const repo = pickRepo(await fetchRepos(), slug);
    if (slug && !repo) return [];
    const params = new URLSearchParams({ page: "1", page_size: "100" });
    if (repo?.path) params.set("repo_path", repo.path);
    const data = await brainFetch<Json>(`/harness/tasks?${params}`, { fresh: true });
    if (!data) return null;
    return list(data, "tasks").map((t) => {
      const status = jobStatus(t.status);
      const finished = !["running", "queued", "retrying"].includes(status);
      return {
        id: str(t.id).slice(0, 8),
        agent: str(t.target_agent) || str(t.owner_agent) || "agent",
        task: str(t.title) || str(t.goal) || "Untitled task",
        status,
        tokens: num(t.tokens),
        packHit: typeof t.pack_hit === "boolean" ? t.pack_hit : undefined,
        startedAt: fmtShort(t.created_at),
        duration: finished ? between(t.created_at, t.updated_at) : "—",
      };
    });
  });
};

export const getEvents = async (limit = 50, slug?: string | null): Promise<LedgerEvent[]> => {
  if (!apiConfigured) return mock.events;
  return live<LedgerEvent[]>([], async () => {
    const repo = pickRepo(await fetchRepos(), slug);
    if (slug && !repo) return [];
    return loadEvents(limit, repo?.path);
  });
};

export const getPagedAgentRuns = async (slug: string | null | undefined, input: Partial<ListQuery> = {}): Promise<Paged<AgentRun>> => {
  const q = getListQuery(input);
  return live({ items: [], paging: { ...q, total: 0, facets: {} } }, async () => {
    const repo = slug ? pickRepo(await fetchRepos(), slug) : null;
    if (slug && !repo) return null;
    const params = new URLSearchParams({ page: String(q.page), page_size: String(q.size) });
    if (repo?.path) params.set("repo_path", repo.path);
    if (q.q) params.set("q", q.q);
    if (q.status) params.set("status", rawTaskStatus(q.status));
    const data = await brainFetch<Json>(`/harness/tasks?${params}`, { fresh: true });
    if (!data) return null;
    const result = pageResult(data, "tasks", q, (t) => {
      const status = jobStatus(t.status), finished = !["running", "queued", "retrying"].includes(status);
      return { id: str(t.id), agent: str(t.target_agent) || str(t.owner_agent) || "agent", task: str(t.title) || str(t.goal) || "Untitled task", status, tokens: num(t.tokens), packHit: typeof t.pack_hit === "boolean" ? t.pack_hit : undefined, startedAt: fmtShort(t.created_at), duration: finished ? between(t.created_at, t.updated_at) : "—" };
    });
    result.paging.facets = Object.entries(result.paging.facets).reduce<Record<string, number>>((acc, [key, value]) => {
      const normalized = jobStatus(key);
      acc[normalized] = (acc[normalized] || 0) + Number(value);
      return acc;
    }, {});
    return result;
  });
};

export const getPagedEvents = async (slug: string | null | undefined, input: Partial<ListQuery> = {}): Promise<Paged<LedgerEvent>> => {
  const q = getListQuery(input);
  return live({ items: [], paging: { ...q, total: 0, facets: {} } }, async () => {
    const repo = slug ? pickRepo(await fetchRepos(), slug) : null;
    if (slug && !repo) return null;
    const base = new URLSearchParams({ limit: String(q.size), offset: String((q.page - 1) * q.size), descending: "true" });
    if (repo?.path) base.set("repository_path", repo.path);
    if (q.q) base.set("q", q.q);
    const data = await brainFetch<Json>(`/ledger/events?${base}`, { fresh: true });
    return data ? { items: list(data, "events").map(toEvent), paging: pagingFrom(data, q) } : null;
  });
};

const triggers: IndexRun["trigger"][] = ["push", "schedule", "manual", "auto-heal"];
function indexTrigger(v: unknown): IndexRun["trigger"] {
  const s = str(v).toLowerCase().replace(/_/g, "-");
  if (!s) return "unknown";
  if ((triggers as string[]).includes(s)) return s as IndexRun["trigger"];
  if (/(sched|nightly|cron|maint)/.test(s)) return "schedule";
  if (/(push|commit|webhook|hook)/.test(s)) return "push";
  if (/heal/.test(s)) return "auto-heal";
  if (/(manual|cli|hand|user)/.test(s)) return "manual";
  return "unknown";
}
function completeness(v: unknown, status: JobStatus): IndexRun["completeness"] {
  const s = str(v).toLowerCase();
  if (s === "complete" || s === "partial" || s === "aborted") return s;
  if (status === "failed" || status === "error" || status === "cancelled") return "aborted";
  // The API reports completeness as the share of eligible files read (0..1).
  const ratio = num(v);
  if (ratio !== undefined) return ratio >= 1 ? "complete" : "partial";
  return status === "completed" ? "complete" : "partial";
}

export const getIndexRuns = async (slug?: string | null): Promise<IndexRun[]> => {
  if (!apiConfigured) return mock.indexRuns;
  return live<IndexRun[]>([], async () => {
    const repo = pickRepo(await fetchRepos(), slug);
    if (!repo) return null;
    const data = await brainFetch<Json>(`/api/web/index-runs?limit=200&repository_id=${repo.id}`, { fresh: true });
    if (!data) return null;
    return list(data, "runs").map((r) => {
      const status = jobStatus(r.status);
      const rev = str(r.revision);
      return {
        id: int(r.id),
        revision: rev.startsWith("snapshot:") ? rev.slice(9, 16) : rev.slice(0, 7),
        trigger: indexTrigger(r.trigger),
        status,
        completeness: completeness(r.completeness, status),
        files: int(r.files),
        changed: int(r.changed),
        chunks: int(r.chunks),
        startedAt: fmtWhen(r.started_at),
        duration: status === "running" || status === "queued" ? "—" : durationOf(r.duration_seconds ?? r.duration),
      };
    });
  });
};

export const getContextPacks = async (): Promise<ContextPack[]> => {
  if (!apiConfigured) return mock.contextPacks;
  return live<ContextPack[]>([], async () => {
    const data = await brainFetch<Json>("/api/web/context-packs?limit=200");
    if (!data) return null;
    return list(data, "packs").map((p) => {
      const budget = str(p.budget).toLowerCase();
      return {
        id: str(p.id),
        task: str(p.task) || "Untitled task",
        budget: budget === "small" || budget === "large" ? budget : "medium",
        tokens: int(p.tokens),
        files: int(p.files),
        createdAt: fmtShort(p.created_at),
        stale: typeof p.stale === "boolean" ? p.stale : null,
        consumer: str(p.consumer) || "an agent",
      };
    });
  });
};

function decisionStatus(v: unknown): Decision["status"] {
  const s = str(v).toLowerCase();
  if (["proposed", "draft", "pending"].includes(s)) return "proposed";
  if (["superseded", "deprecated", "historical", "replaced", "inactive", "rejected"].includes(s)) return "superseded";
  return "accepted";
}

export const getDecisions = async (): Promise<Decision[]> => {
  if (!apiConfigured) return mock.decisions;
  return live<Decision[]>([], async () => {
    const data = await brainFetch<unknown>("/decisions");
    if (!data) return null;
    return list(data, "decisions").map((d) => {
      const modules = arr(d.affected_modules).map((m) => str(m)).filter(Boolean);
      return {
        id: `D-${str(d.id)}`,
        title: str(d.title) || "Untitled decision",
        status: decisionStatus(d.status),
        scope: modules[0] ?? (str(d.repo_path).split(/[\\/]/).filter(Boolean).pop() || "whole project"),
        recordedAt: fmtShort(d.date ?? d.created_at),
        summary: str(d.description) || str(d.reason) || "",
      };
    });
  });
};

function ruleSeverity(v: unknown): Rule["severity"] {
  const s = str(v).toLowerCase();
  if (["critical", "high", "block", "blocker", "error"].includes(s)) return "block";
  if (["medium", "warn", "warning"].includes(s)) return "warn";
  return "advise";
}
function ruleScope(applies: unknown, type: string): string {
  if (typeof applies === "string") return applies;
  if (Array.isArray(applies)) return applies.map((a) => str(a)).filter(Boolean).join(", ") || type || "everywhere";
  const o = obj(applies);
  const first = Object.values(o)
    .flatMap((v) => (Array.isArray(v) ? v : [v]))
    .map((v) => str(v))
    .filter(Boolean);
  return first.slice(0, 2).join(", ") || type || "everywhere";
}

export const getRules = async (): Promise<Rule[]> => {
  if (!apiConfigured) return mock.rules;
  return live<Rule[]>([], async () => {
    const data = await brainFetch<unknown>("/rules");
    if (!data) return null;
    return list(data, "rules")
      .filter((r) => !["disabled", "inactive", "deprecated"].includes(str(r.status).toLowerCase()))
      .map((r) => ({
        id: str(r.id),
        rule: str(r.description) || str(r.name) || str(r.id),
        severity: ruleSeverity(r.severity),
        scope: ruleScope(r.applies_to, str(r.type)),
        hits30d: num(r.hits_30d),
      }));
  });
};

export const getInsights = async (): Promise<Insight[]> => {
  if (!apiConfigured) return mock.insights;
  return live<Insight[]>([], () => loadInsights());
};

export const getModuleEdges = async (slug?: string | null): Promise<ModuleEdge[]> => {
  if (!apiConfigured) return mock.moduleEdges;
  return live<ModuleEdge[]>([], async () => {
    const repo = pickRepo(await fetchRepos(), slug);
    if (!repo) return null;
    const data = await brainFetch<Json>(`/api/web/module-graph?repository_id=${repo.id}`);
    if (!data) return null;
    return list(data, "edges")
      .filter((e) => str(e.from) && str(e.to))
      .map((e) => ({ from: str(e.from), to: str(e.to), weight: int(e.weight, 1), violation: e.violation === true }));
  });
};

export const getMcpTools = async (): Promise<McpTool[]> => {
  if (!apiConfigured) return mock.mcpTools;
  return live<McpTool[]>([], async () => {
    const data = await brainFetch<Json>("/api/status/mcp");
    if (!data) return null;
    const online = data.available === true;
    return list(data, "tools")
      .filter((t) => str(t.name))
      .map((t) => {
        const errorRate = num(t.error_rate);
        return {
          name: str(t.name),
          surface: str(t.surface) === "local" ? "local" : "remote",
          calls24h: num(t.calls_24h),
          p95ms: num(t.p95_ms),
          errorRate,
          // Without call counts a tool is only known to exist, not to be healthy.
          status: !online
            ? "bad"
            : errorRate === undefined && num(t.calls_24h) === undefined
              ? "info"
              : errorRate !== undefined && errorRate > 0.05
                ? "bad"
                : errorRate !== undefined && errorRate > 0.02
                  ? "warn"
                  : "ok",
        } satisfies McpTool;
      });
  });
};

export const getReports = async (): Promise<Report[]> => {
  if (!apiConfigured) return mock.reports;
  return live<Report[]>([], async () => {
    const data = await brainFetch<Json>("/api/web/reports?limit=200");
    if (!data) return null;
    return list(data, "reports").map((r) => ({
      id: str(r.id),
      title: str(r.title) || str(r.id) || "Untitled report",
      kind: str(r.kind) || "report",
      createdAt: fmtWhen(r.created_at),
      size: fmtBytes(num(r.size_bytes)),
    }));
  });
};

/** Server-paged collections used by the history screens. The legacy getters above
 * remain available for compact dashboard widgets. */
export const getPagedIndexRuns = async (slug: string | null | undefined, input: Partial<ListQuery> = {}): Promise<Paged<IndexRun>> => {
  const q = getListQuery(input);
  if (!apiConfigured) return { items: mock.indexRuns.slice((q.page - 1) * q.size, q.page * q.size), paging: { ...q, total: mock.indexRuns.length, facets: {} } };
  return live({ items: [], paging: { ...q, total: 0, facets: {} } }, async () => {
    const repo = pickRepo(await fetchRepos(), slug);
    if (!repo) return null;
    const data = await brainFetch<Json>(`/api/web/index-runs?repository_id=${repo.id}&${queryString(q)}`, { fresh: true });
    return data ? pageResult(data, "runs", q, (r) => {
      const status = jobStatus(r.status), rev = str(r.revision);
      return { id: int(r.id), revision: rev.startsWith("snapshot:") ? rev.slice(9, 16) : rev.slice(0, 7), trigger: indexTrigger(r.trigger), status, completeness: completeness(r.completeness, status), files: int(r.files), changed: int(r.changed), chunks: int(r.chunks), startedAt: fmtWhen(r.started_at), duration: status === "running" || status === "queued" ? "—" : durationOf(r.duration_seconds ?? r.duration) };
    }) : null;
  });
};

export const getPagedContextPacks = async (input: Partial<ListQuery> = {}, slug?: string | null): Promise<Paged<ContextPack>> => {
  const q = getListQuery(input);
  return live({ items: [], paging: { ...q, total: 0, facets: {} } }, async () => {
    const repo = slug ? pickRepo(await fetchRepos(), slug) : null;
    if (slug && !repo) return null;
    const data = await brainFetch<Json>(`/api/web/context-packs?${queryString(q)}${repo ? `&repository_id=${repo.id}` : ""}`);
    return data ? pageResult(data, "packs", q, (p) => { const budget = str(p.budget).toLowerCase(); return { id: str(p.id), task: str(p.task) || "Untitled task", budget: budget === "small" || budget === "large" ? budget : "medium", tokens: int(p.tokens), files: int(p.files), createdAt: fmtShort(p.created_at), stale: typeof p.stale === "boolean" ? p.stale : null, consumer: str(p.consumer) || "an agent" }; }) : null;
  });
};

export const getPagedReports = async (input: Partial<ListQuery> = {}): Promise<Paged<Report>> => {
  const q = getListQuery(input);
  return live({ items: [], paging: { ...q, total: 0, facets: {} } }, async () => {
    const data = await brainFetch<Json>(`/api/web/reports?${queryString(q)}`);
    return data ? pageResult(data, "reports", q, (r) => ({ id: str(r.id), title: str(r.title) || str(r.id) || "Untitled report", kind: str(r.kind) || "report", createdAt: fmtWhen(r.created_at), size: fmtBytes(num(r.size_bytes)) })) : null;
  });
};

export const getPagedInsights = async (slug: string | null | undefined, input: Partial<ListQuery> = {}): Promise<Paged<Insight>> => {
  const q = getListQuery(input);
  return live({ items: [], paging: { ...q, total: 0, facets: {} } }, async () => {
    const params = new URLSearchParams(queryString({ ...q, status: "" }));
    if (q.status) params.set("severity", q.status);
    const data = await brainFetch<Json>(`/insights?${params}`);
    return data ? pageResult(data, "insights", q, (i) => {
      const evidence = arr(i.evidence), first = obj(evidence[0]);
      return { id: str(i.id), tone: severityTone(i.severity), kind: insightKind(str(i.insight_type)), title: str(i.title, "Untitled finding"), evidence: evidenceText(evidence[0]) || str(i.summary), module: str(first.module) || str(first.file) || str(first.path) || str(i.source) || "—", detectedAt: fmtShort(i.last_seen_at ?? i.created_at) };
    }) : null;
  });
};

export const getPagedDecisions = async (slug: string | null | undefined, input: Partial<ListQuery> = {}): Promise<Paged<Decision>> => {
  const q = getListQuery(input);
  return live({ items: [], paging: { ...q, total: 0, facets: {} } }, async () => {
    const repo = slug ? pickRepo(await fetchRepos(), slug) : null; if (slug && !repo) return null;
    const scope = repo ? `&repository_id=${repo.id}` : "";
    const data = await brainFetch<Json>(`/api/web/decisions?page=${q.page}&page_size=${q.size}${q.q ? `&q=${encodeURIComponent(q.q)}` : ""}${q.status ? `&status=${q.status}` : ""}${scope}`);
    return data ? pageResult(data, "decisions", q, (d) => ({ id: `D-${str(d.id)}`, title: str(d.title) || "Untitled decision", status: decisionStatus(d.status), scope: str(d.repo_path).split(/[\\/]/).filter(Boolean).pop() || "whole project", recordedAt: fmtShort(d.date ?? d.created_at), summary: str(d.description) || str(d.reason) || "" })) : null;
  });
};

export const getPagedRules = async (slug: string | null | undefined, input: Partial<ListQuery> = {}): Promise<Paged<Rule>> => {
  const q = getListQuery(input);
  return live({ items: [], paging: { ...q, total: 0, facets: {} } }, async () => {
    const repo = slug ? pickRepo(await fetchRepos(), slug) : null; if (slug && !repo) return null;
    const scope = repo ? `&repository_id=${repo.id}` : "";
    const severity = q.status && ["block", "warn", "advise"].includes(q.status) ? `&severity=${q.status}` : "";
    const data = await brainFetch<Json>(`/api/web/rules?page=${q.page}&page_size=${q.size}${q.q ? `&q=${encodeURIComponent(q.q)}` : ""}${scope}${severity}`);
    return data ? pageResult(data, "rules", q, (r) => ({ id: str(r.id), rule: str(r.description) || str(r.name) || str(r.id), severity: ruleSeverity(r.severity), scope: ruleScope(r.applies_to, str(r.type)), hits30d: num(r.hits_30d) })) : null;
  });
};

export const getPrincipals = async (): Promise<Principal[]> => {
  if (!apiConfigured) return mock.principals;
  return live<Principal[]>([], async () => {
    const data = await brainFetch<Json>("/admin/principals");
    if (!data) return null;
    const now = Date.now();
    return list(data, "principals")
      .filter((p) => !p.disabled_at)
      .map((p) => {
        const creds = arr(p.credentials)
          .map(obj)
          .filter((c) => !c.revoked_at && !((parseDate(c.expires_at)?.getTime() ?? Infinity) < now));
        const scopes = [...new Set(creds.flatMap((c) => arr(c.scopes).map((s) => str(s))).filter(Boolean))];
        const seen = creds
          .map((c) => parseDate(c.last_used_at))
          .filter((d): d is Date => d !== null)
          .sort((a, b) => b.getTime() - a.getTime())[0];
        const kind = str(p.kind).toLowerCase();
        return {
          id: `p-${str(p.id)}`,
          name: str(p.name) || `principal ${str(p.id)}`,
          kind: kind === "human" ? "human" : kind === "agent" ? "agent" : "service",
          scopes,
          lastSeen: seen ? fmtShort(seen.toISOString()) : "never",
          credentials: creds.length,
        } satisfies Principal;
      });
  });
};

const fetchSetup = cache(() => brainFetch<Json>("/api/setup/status", { fresh: true }));

export const getClientConfigs = async (): Promise<{ name: string; where: string; config: string; cli: string }[]> => {
  if (!apiConfigured) return [];
  return live([], async () => {
    const data = await fetchSetup();
    return Object.entries(obj(data?.client_configs)).map(([name, raw]) => {
      const entry = obj(raw);
      return { name, where: str(entry.where), config: str(entry.config), cli: str(entry.cli) };
    }).filter(entry => entry.config);
  });
};

// Access: every principal, disabled ones included, with each key's whole life
// (created, expires, last used, revoked) — what /admin needs to manage them.
const SOON_MS = 14 * 86400_000;
const yearFmt = new Intl.DateTimeFormat("en-US", { timeZone: tz, month: "short", day: "numeric", year: "numeric" });
/** "Oct 30", or "Oct 30, 2027" outside this year. */
const fmtDay = (d: Date) => (d.getUTCFullYear() === new Date().getUTCFullYear() ? dayFmt.format(d) : yearFmt.format(d));

function summarize(identities: Identity[]): AccessView["summary"] {
  const live = identities.filter((i) => !i.disabled).flatMap((i) => i.keys.filter((k) => k.status === "active"));
  return {
    identities: identities.length,
    disabled: identities.filter((i) => i.disabled).length,
    activeKeys: live.length,
    expiringSoon: live.filter((k) => k.expiringSoon).length,
    neverUsed: live.filter((k) => k.neverUsed).length,
  };
}

function toAccessKey(c: Json, now: number): AccessKey & { at: number; seen: Date | null } {
  const expires = parseDate(c.expires_at);
  const seen = parseDate(c.last_used_at);
  const status: AccessKey["status"] = c.revoked_at ? "revoked" : expires && expires.getTime() < now ? "expired" : "active";
  const left = expires ? expires.getTime() - now : Infinity;
  return {
    id: int(c.id),
    scopes: [...new Set(arr(c.scopes).map((s) => str(s)).filter(Boolean))].sort(),
    status,
    created: fmtWhen(c.created_at),
    expires: !expires ? "never" : status === "active" ? `${fmtDay(expires)} · in ${fmtAge(left / 1000)}` : fmtDay(expires),
    lastUsed: seen ? fmtShort(seen.toISOString()) : "never",
    expiringSoon: status === "active" && left < SOON_MS,
    neverUsed: status === "active" && !seen,
    at: parseDate(c.created_at)?.getTime() ?? int(c.id),
    seen,
  };
}

export const getAccessView = async (): Promise<AccessView> => {
  if (!apiConfigured) return { readable: true, identities: mock.identities, summary: summarize(mock.identities) };
  const empty: AccessView = { readable: false, identities: [], summary: summarize([]) };
  return live<AccessView>(empty, async () => {
    const data = await brainFetch<Json>("/admin/principals", { fresh: true });
    if (!data) return null;
    const now = Date.now();
    const identities = list(data, "principals").map((p): Identity => {
      const keys = arr(p.credentials)
        .map((c) => toAccessKey(obj(c), now))
        .sort((a, b) => b.at - a.at);
      const active = keys.filter((k) => k.status === "active");
      const seen = keys
        .map((k) => k.seen)
        .filter((d): d is Date => d !== null)
        .sort((a, b) => b.getTime() - a.getTime())[0];
      const kind = str(p.kind).toLowerCase();
      const created = parseDate(p.created_at);
      return {
        id: int(p.id),
        name: str(p.name) || `principal ${str(p.id)}`,
        kind: kind === "human" ? "human" : kind === "agent" ? "agent" : "service",
        disabled: Boolean(p.disabled_at),
        createdAt: created ? fmtDay(created) : "—",
        activeKeys: active.length,
        scopes: [...new Set(active.flatMap((k) => k.scopes))].sort(),
        lastSeen: seen ? fmtShort(seen.toISOString()) : "never",
        keys: keys.map(({ at: _at, seen: _seen, ...k }) => k),
      };
    });
    return { readable: true, identities, summary: summarize(identities) };
  });
};

export const getSetupSteps = async (): Promise<SetupStep[]> => {
  if (!apiConfigured) return mock.setupSteps;
  return live<SetupStep[]>([], async () => {
    const data = await fetchSetup();
    if (!data) return null;
    return list(data, "steps").map((s) => {
      const detail = sentence(str(s.detail));
      const hint = str(s.hint);
      return {
        id: str(s.id),
        title: str(s.title) || str(s.id),
        detail: hint ? `${detail}${detail ? ". " : ""}${sentence(hint)}.` : detail ? `${detail}.` : "",
        done: str(s.status) === "done",
        command: str(s.command) || undefined,
      };
    });
  });
};

export const getReranker = async (): Promise<RerankerStatus> => {
  if (!apiConfigured) return mock.reranker;
  const empty: RerankerStatus = {
    model: "—",
    state: "idle",
    stateText: "No data",
    coverage: { label: "Ready for precise ranking", value: "—", parts: [] },
    p50ms: 0,
    p95ms: 0,
    ndcgLift: "—",
    queries24h: 0,
    available: false,
  };
  return live(empty, async () => {
    const repo = pickRepo(await fetchRepos());
    if (!repo?.path) return null;
    const data = await brainFetch<Json>(`/late-interaction/status?repo_path=${encodeURIComponent(repo.path)}`);
    if (!data) return null;
    const inv = obj(data.inventory);
    const provider = obj(data.provider);
    const metrics = obj(data.metrics);
    const traffic = obj(data.traffic);
    const approval = obj(data.approval);
    const pStatus = str(provider.status).toLowerCase();
    const serviceUp = ["healthy", "ok", "ready"].includes(pStatus);
    const on = traffic.enabled === true && traffic.rerank === true && int(traffic.canary_percent) > 0;
    const [state, stateText]: [Tone, string] =
      traffic.enabled === undefined && !pStatus
        ? ["idle", "Unknown"]
        : !on
          ? ["idle", "Turned off"]
          : serviceUp
            ? ["ok", "Serving"]
            : ["bad", "Not responding"];

    // Newer servers count encoded documents; older ones counted chunks.
    const docs = int(inv.document_count);
    const total = int(inv.total_chunks);
    const current = int(inv.current_chunks);
    const pending = Math.max(0, total - current);
    const pct = num(inv.coverage_pct) ?? (total ? (current / total) * 100 : 0);
    const revision = str(inv.index_revision);
    const approvedRevision = str(approval.approved_index_revision);
    const approvedDocs = int(approval.approved_document_count);

    const offReasons: string[] = [];
    if (!on) {
      if (traffic.enabled !== true) offReasons.push("Switched off in the server settings (LATE_INTERACTION_ENABLED).");
      else if (traffic.rerank !== true) offReasons.push("Re-ranking itself is switched off (LATE_INTERACTION_RERANK_ENABLED).");
      if (int(traffic.canary_percent) <= 0) offReasons.push("0% of searches are routed to it (LATE_INTERACTION_CANARY_PERCENT).");
      if (revision && approvedRevision && revision !== approvedRevision)
        offReasons.push(
          `The encoded index (${revision}, ${fmtN(docs)} documents) is newer than the one approved for live use (${approvedRevision}, ${fmtN(approvedDocs)}). It has to be checked and approved again before it can serve.`,
        );
      if (approval.production_gates_passed === false) offReasons.push("Its quality checks for live traffic haven’t passed yet.");
    }
    if (!serviceUp && pStatus) offReasons.push(`The model service reports “${pStatus}”${str(provider.reason) ? `: ${str(provider.reason)}` : ""}.`);

    return {
      model: str(inv.model) || str(provider.model_revision).slice(0, 12) || "—",
      state,
      stateText,
      coverage: {
        label: "Ready for precise ranking",
        value: total ? `${pct.toFixed(1)}%` : docs ? fmtN(docs) : "—",
        parts: total
          ? [
              { tone: "filled", count: current, text: `${fmtN(current)} encoded` },
              { tone: "missing", count: pending, text: `${fmtN(pending)} pending` },
            ]
          : docs
            ? [{ tone: "filled", count: docs, text: `${fmtN(docs)} documents encoded` }]
            : [],
      },
      p50ms: Math.round(int(metrics.rerank_latency_ms_p50)),
      p95ms: Math.round(int(metrics.rerank_latency_ms_p95)),
      ndcgLift: "—",
      queries24h: int(metrics.rerank_requests),
      queriesWindow: "since the API last restarted",
      service: pStatus ? (serviceUp ? { tone: "ok", text: "Model service ready" } : { tone: "bad", text: `Model service ${pStatus}` }) : undefined,
      offReasons,
      index: revision
        ? { revision, documents: docs, updated: inv.last_update ? fmtShort(inv.last_update) : undefined, approvedRevision: approvedRevision || undefined, approvedDocuments: approvedDocs || undefined }
        : undefined,
      available: true,
    };
  });
};

export const getCodeModules = async (slug?: string | null): Promise<CodeModule[]> => {
  if (!apiConfigured) return mock.codeModules(mock.findRepository(slug));
  return live<CodeModule[]>([], async () => {
    const repo = pickRepo(await fetchRepos(), slug);
    if (!repo) return null;
    const data = await brainFetch<Json>(`/api/web/modules?repository_id=${repo.id}`);
    if (!data) return null;
    return list(data, "modules")
      .filter((m) => str(m.name))
      .map((m) => {
        const current = int(m.current);
        const outdated = int(m.outdated);
        const missing = int(m.missing);
        const excluded = int(m.excluded);
        return {
          name: str(m.name),
          chunks: num(m.chunks) ?? current + outdated + missing + excluded,
          current,
          outdated,
          missing,
          excluded,
        };
      });
  });
};
