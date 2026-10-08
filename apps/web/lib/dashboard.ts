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
