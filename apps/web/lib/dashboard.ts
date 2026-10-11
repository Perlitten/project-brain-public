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

/** An unknown repository never becomes an all-project query. */
export async function getRequestTelemetry(period: DashboardPeriod, repositoryId?: number): Promise<RequestTelemetry | null> {
  await connection();
  if (!apiConfigured || (repositoryId !== undefined && (!Number.isSafeInteger(repositoryId) || repositoryId <= 0))) return null;
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
  generated_at: string | null;
  source: { build_sha: string; corpus_sha256: string; search_sample_count: number; context_fixture_count: number; provenance: { artifact: string; measured_build_sha: string; sha256: string }[] };
  search: { hit1_any_pct: number; hit3_any_pct: number; hit5_any_pct: number; mrr_any: number };
  context: { normal_count: number; mean_recall: number; mean_mrr: number; mean_fixture_noise_ratio: number; adversarial_status: string };
  quality_limits: { native_tasks_passed: number; native_task_count: number; native_precision: number; native_recall: number };
}

export async function getQualityEvidence(): Promise<QualityEvidence | null> {
  await connection();
  if (!apiConfigured) return null;
  return brainFetch<QualityEvidence>("/api/web/quality", { fresh: true });
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

/** Null when the server cannot answer — the panel then says so instead of showing zeros. */
export async function getAgentValue(period: DashboardPeriod, repositoryId?: number): Promise<AgentValue | null> {
  await connection();
  if (!apiConfigured || (repositoryId !== undefined && (!Number.isSafeInteger(repositoryId) || repositoryId <= 0))) return null;
  const query = new URLSearchParams({ period });
  if (repositoryId !== undefined) query.set("repository_id", String(repositoryId));
  const response = await brainFetch<AgentValue>(`/api/web/agent-value?${query}`, { fresh: true });
  return !response || response.unavailable ? null : response;
}

/** A 0–1 share as a percentage string, keeping "not measured" distinct from 0%. */
export function measuredShare(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "—" : measuredNumber(Math.round(value * 1000) / 10, "%");
}
