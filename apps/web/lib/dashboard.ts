import { connection } from "next/server";
import { apiConfigured, brainFetch } from "./api";

export type DashboardPeriod = "24h" | "7d" | "30d";
export function dashboardPeriod(value?: string): DashboardPeriod {
  return value === "24h" || value === "30d" ? value : "7d";
}

// Request telemetry is independent of index coverage and offline evaluations.
export interface RequestTelemetry {
  collection_started_at: string | null;
  observed_at: string;
  period: DashboardPeriod;
  partial: boolean;
  total: number;
  failed: number;
  clients: number;
  context_total: number;
  context_success: number;
  requests: { unattributed: number; partial: number; empty: number };
  latency: { search: Latency; context: Latency };
  buckets: RequestBucket[];
  recent_requests: RequestObservation[];
}
export interface Latency { samples: number; p50_ms: number | null; p95_ms: number | null }
export interface RequestBucket { at: string; total: number | null; failed: number | null; p95_ms: number | null; partial?: boolean }
export interface RequestObservation { operation: string; outcome: string; latency_ms: number; recorded_at: string; repository_id: number | null; request_id: string | null; identity: string; surface: string }

// Demo-mode payloads: shown only when no API is configured. The shapes are
// the same the live endpoints return, so the UI renders identically.
const demoBuckets = (period: DashboardPeriod): RequestBucket[] => {
  const days = period === "24h" ? 1 : period === "30d" ? 7 : 7;
  const totals = period === "24h" ? [212, 184, 236, 198, 261, 244, 207, 289] : [1820, 2104, 1972, 2388, 2190, 1844, 2412];
  return totals.slice(0, days === 1 ? 8 : days).map((total, i) => ({
    at: `Oct ${period === "30d" ? 26 + i : 1 + i}`,
    total,
    failed: Math.round(total * (i === 3 ? 0.03 : 0.008)),
    p95_ms: 168 + ((i * 37) % 60),
  }));
};

const demoTelemetry = (period: DashboardPeriod): RequestTelemetry => ({
  collection_started_at: "2026-09-24T09:12:00Z",
  observed_at: "2026-10-02T14:02:00Z",
  period,
  partial: false,
  total: period === "24h" ? 1847 : period === "30d" ? 52411 : 14730,
  failed: 61,
  clients: 4,
  context_total: 611,
  context_success: 598,
  requests: { unattributed: 88, partial: 0, empty: 12 },
  latency: { search: { samples: 14119, p50_ms: 96, p95_ms: 184 }, context: { samples: 611, p50_ms: 1820, p95_ms: 2310 } },
  buckets: demoBuckets(period),
  recent_requests: [
    { operation: "prepare_task_context", outcome: "success", latency_ms: 2140, recorded_at: "2026-10-02T13:58:00Z", repository_id: 4, request_id: "req-991", identity: "claude-code", surface: "mcp" },
    { operation: "search_code", outcome: "success", latency_ms: 142, recorded_at: "2026-10-02T13:57:00Z", repository_id: 3, request_id: "req-990", identity: "codex", surface: "mcp" },
    { operation: "search_code", outcome: "success", latency_ms: 171, recorded_at: "2026-10-02T13:55:00Z", repository_id: 4, request_id: "req-989", identity: "claude-code", surface: "http" },
    { operation: "prepare_deep_context", outcome: "timeout", latency_ms: 30000, recorded_at: "2026-10-02T13:41:00Z", repository_id: 1, request_id: "req-988", identity: "devin", surface: "mcp" },
    { operation: "search_code", outcome: "success", latency_ms: 118, recorded_at: "2026-10-02T13:40:00Z", repository_id: 2, request_id: "req-987", identity: "cursor", surface: "mcp" },
  ],
});

/** An unknown repository never becomes an all-project query. */
export async function getRequestTelemetry(period: DashboardPeriod, repositoryId?: number): Promise<RequestTelemetry | null> {
  await connection();
  if (!apiConfigured) return demoTelemetry(period);
  if ((repositoryId !== undefined && (!Number.isSafeInteger(repositoryId) || repositoryId <= 0))) return null;
  const query = new URLSearchParams({ period });
  if (repositoryId !== undefined) query.set("repository_id", String(repositoryId));
  const response = await brainFetch<RequestTelemetry & { unavailable?: boolean }>(`/api/web/dashboard?${query}`, { fresh: true });
  return response?.unavailable ? null : response;
}

export function measuredNumber(value: number | null | undefined, suffix = ""): string {
  return value == null || !Number.isFinite(value) ? "—" : `${value.toLocaleString("en-US", { maximumFractionDigits: 1 })}${suffix}`;
}

export interface QualityEvidence {
  historical: boolean;
  /** Measured by the maintainers on Brain's own source and shipped with it. */
  reference_run?: boolean;
  /** Demo-mode result: labelled as an example, not a real measurement. */
  demo?: boolean;
  generated_at: string | null;
  source: { build_sha: string; corpus_sha256: string; search_sample_count: number; context_fixture_count: number; provenance: { artifact: string; measured_build_sha: string; sha256: string }[] };
  search: { hit1_any_pct: number; hit3_any_pct: number; hit5_any_pct: number; mrr_any: number };
  context: { normal_count: number; mean_recall: number; mean_mrr: number; mean_fixture_noise_ratio: number; adversarial_status: string };
  quality_limits: { native_tasks_passed: number; native_task_count: number; native_precision: number; native_recall: number };
}

/** No benchmark has been saved on this install yet. */
export interface QualityNotRun {
  available: false;
  reason: "not_run";
}
export const qualityNotRun = (e: QualityEvidence | QualityNotRun | null): e is QualityNotRun =>
  Boolean(e && (e as QualityNotRun).available === false);

export async function getQualityEvidence(): Promise<QualityEvidence | QualityNotRun | null> {
  await connection();
  if (!apiConfigured) {
    return {
      historical: false,
      demo: true,
      generated_at: "2026-10-02T03:00:00Z",
      source: { build_sha: "d57f0142c9a1", corpus_sha256: "9f2c41a8e811", search_sample_count: 96, context_fixture_count: 48, provenance: [{ artifact: "retrieval-eval", measured_build_sha: "d57f0142c9a1", sha256: "b7e10c92aa04" }] },
      search: { hit1_any_pct: 61.5, hit3_any_pct: 78.1, hit5_any_pct: 87.4, mrr_any: 0.704 },
      context: { normal_count: 45, mean_recall: 0.82, mean_mrr: 0.66, mean_fixture_noise_ratio: 0.11, adversarial_status: "declined correctly" },
      quality_limits: { native_tasks_passed: 45, native_task_count: 48, native_precision: 0.88, native_recall: 0.84 },
    };
  }
  return brainFetch<QualityEvidence | QualityNotRun>("/api/web/quality", { fresh: true });
}

// Whether Brain is worth having: adoption per agent, pack economy, and
// whether recorded memory reaches the work. Every count here can legitimately
// be null — packs built before composition accounting have no counts, and a
// null must never be rendered as a zero.
export interface AgentValue {
  period: DashboardPeriod;
  unavailable: boolean;
  window: { days: number; started_at: string | null; until: string | null };
  packs: AgentValuePacks | null;
  agents: AgentValueAgent[];
  rules: AgentValueRules | null;
}
export interface Spread { p50: number | null; p95: number | null; sample_count: number }
export interface AgentValuePacks {
  total: number;
  measured: number;
  unmeasured: number;
  measurement_started_at: string | null;
  partial: boolean;
  tokens: Spread;
  files: Spread;
  rules_per_pack: Spread;
  decisions_per_pack: Spread;
  learnings_per_pack: Spread;
  /** Measured packs carrying at least one rule, decision or learning. */
  with_memory: number | null;
  /** That count as a share of measured packs; null until something was measured. */
  memory_reach: number | null;
  carrying: { rules: number | null; decisions: number | null; learnings: number | null };
  attributed: number;
  unattributed: number;
  by_budget: { name: string; count: number }[];
}
export interface AgentValueAgent {
  agent: string;
  requests: number;
  context_requests: number;
  search_requests: number;
  successes: number;
  failures: number;
  surfaces: string[];
  packs: number;
  packs_with_memory: number | null;
  last_seen_at: string | null;
  success_rate: number | null;
  /** Durable packs per context request — not a task count. */
  pack_rate: number | null;
}
export interface AgentValueRules {
  active: number;
  measurement_started_at: string | null;
  delivered_events: number;
  flagged_events: number;
  /** Null until recording began: before that every rule would look unused. */
  delivered_rules: number | null;
  flagged_rules: number | null;
  unused: number | null;
  top: { rule_id: string; delivered: number; flagged: number }[];
}

const demoAgentValue = (period: DashboardPeriod): AgentValue => ({
  period,
  unavailable: false,
  window: { days: period === "24h" ? 1 : period === "30d" ? 30 : 7, started_at: "2026-09-25T00:00:00Z", until: "2026-10-02T14:02:00Z" },
  packs: {
    total: 42,
    measured: 39,
    unmeasured: 3,
    measurement_started_at: "2026-09-24T09:12:00Z",
    partial: false,
    tokens: { p50: 6120, p95: 15990, sample_count: 39 },
    files: { p50: 9, p95: 26, sample_count: 39 },
    rules_per_pack: { p50: 2, p95: 4, sample_count: 39 },
    decisions_per_pack: { p50: 1, p95: 3, sample_count: 39 },
    learnings_per_pack: { p50: 0, p95: 2, sample_count: 39 },
    with_memory: 31,
    memory_reach: 31 / 39,
    carrying: { rules: 58, decisions: 34, learnings: 12 },
    attributed: 37,
    unattributed: 5,
    by_budget: [{ name: "small", count: 11 }, { name: "medium", count: 21 }, { name: "large", count: 10 }],
  },
  agents: [
    { agent: "claude-code", requests: 6204, context_requests: 288, search_requests: 5916, successes: 6140, failures: 64, surfaces: ["mcp", "http"], packs: 19, packs_with_memory: 16, last_seen_at: "2026-10-02T13:58:00Z", success_rate: 0.9897, pack_rate: 0.066 },
    { agent: "codex", requests: 4981, context_requests: 172, search_requests: 4809, successes: 4930, failures: 51, surfaces: ["mcp"], packs: 14, packs_with_memory: 10, last_seen_at: "2026-10-02T12:58:00Z", success_rate: 0.9898, pack_rate: 0.081 },
    { agent: "cursor", requests: 3102, context_requests: 98, search_requests: 3004, successes: 3054, failures: 48, surfaces: ["mcp"], packs: 6, packs_with_memory: 4, last_seen_at: "2026-10-02T12:31:00Z", success_rate: 0.9845, pack_rate: 0.061 },
    { agent: "devin", requests: 443, context_requests: 53, search_requests: 390, successes: 431, failures: 12, surfaces: ["mcp"], packs: 3, packs_with_memory: 1, last_seen_at: "2026-10-02T10:55:00Z", success_rate: 0.9729, pack_rate: 0.057 },
  ],
  rules: {
    active: 4,
    measurement_started_at: "2026-09-24T09:12:00Z",
    delivered_events: 58,
    flagged_events: 12,
    delivered_rules: 4,
    flagged_rules: 3,
    unused: 0,
    top: [{ rule_id: "R-030", delivered: 24, flagged: 7 }, { rule_id: "R-014", delivered: 18, flagged: 2 }, { rule_id: "R-021", delivered: 11, flagged: 0 }, { rule_id: "R-009", delivered: 9, flagged: 3 }],
  },
});

/** Null when the server cannot answer — the panel then says so instead of showing zeros. */
export async function getAgentValue(period: DashboardPeriod, repositoryId?: number): Promise<AgentValue | null> {
  await connection();
  if (!apiConfigured) return demoAgentValue(period);
  if ((repositoryId !== undefined && (!Number.isSafeInteger(repositoryId) || repositoryId <= 0))) return null;
  const query = new URLSearchParams({ period });
  if (repositoryId !== undefined) query.set("repository_id", String(repositoryId));
  const response = await brainFetch<AgentValue>(`/api/web/agent-value?${query}`, { fresh: true });
  return !response || response.unavailable ? null : response;
}

/** A 0–1 share as a percentage string, keeping "not measured" distinct from 0%. */
export function measuredShare(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "—" : measuredNumber(Math.round(value * 1000) / 10, "%");
}
