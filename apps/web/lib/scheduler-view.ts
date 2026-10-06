// Scheduled jobs as /settings shows them: GET /scheduler/jobs, with times
// preformatted on the server (BRAIN_TIMEZONE) so SSR and hydration agree.
import "server-only";
import { brainFetch } from "./api";

type Json = Record<string, unknown>;
const str = (v: unknown) => (typeof v === "string" ? v : v == null ? "" : String(v));
const whenFmt = new Intl.DateTimeFormat("en-GB", { timeZone: process.env.BRAIN_TIMEZONE || "UTC", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const when = (iso: string) => {
  if (!iso) return undefined;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : whenFmt.format(d);
};

export type ScheduleStatus = "ok" | "stale" | "degraded" | "pending" | "disabled";

export interface ScheduledJob {
  jobType: string;
  description: string;
  cron: string;
  pool: string;
  nextRun?: string;
  lastSuccess?: string;
  lastFailure?: string;
  lastError?: string;
  attempts?: number;
  maxAttempts: number;
  status: ScheduleStatus;
  deadman: boolean;
}

export interface SchedulerView {
  enabled: boolean;
  graceHours: number;
  jobs: ScheduledJob[];
}

export function toSchedulerView(raw: Json | null): SchedulerView | null {
  if (!raw || !Array.isArray(raw.jobs)) return null;
  return {
    enabled: Boolean(raw.enabled),
    graceHours: Math.round((Number(raw.grace_seconds) || 0) / 3600),
    jobs: (raw.jobs as Json[]).map((j) => ({
      jobType: str(j.job_type),
      description: str(j.description) || str(j.job_type),
      cron: str(j.cron),
      pool: str(j.pool),
      nextRun: when(str(j.next_run_at)),
      lastSuccess: when(str(j.last_success_at)),
      lastFailure: when(str(j.last_failure_at)),
      lastError: str(j.last_error) || undefined,
      attempts: typeof j.last_attempts === "number" ? j.last_attempts : undefined,
      maxAttempts: Number(j.max_attempts) || 1,
      status: (str(j.status) || "pending") as ScheduleStatus,
      deadman: Boolean(j.deadman_configured),
    })),
  };
}

export async function getSchedulerJobs(): Promise<SchedulerView | null> {
  return toSchedulerView(await brainFetch<Json>("/scheduler/jobs", { fresh: true }));
}

export function demoScheduler(): SchedulerView {
  return {
    enabled: true,
    graceHours: 2,
    jobs: [
      { jobType: "nightly_maintenance", description: "Nightly deep maintenance", cron: "30 0 * * *", pool: "deep", nextRun: when("2026-10-07T00:30:00Z"), lastSuccess: when("2026-10-06T00:41:12Z"), maxAttempts: 3, attempts: 1, status: "ok", deadman: true },
      { jobType: "health_check", description: "Nightly harness health", cron: "0 2 * * *", pool: "fast", nextRun: when("2026-10-07T02:00:00Z"), lastSuccess: when("2026-10-06T02:00:31Z"), maxAttempts: 3, attempts: 1, status: "ok", deadman: false },
      { jobType: "self_diagnosis", description: "Nightly LLM self-diagnosis", cron: "30 3 * * *", pool: "fast", nextRun: when("2026-10-07T03:30:00Z"), lastSuccess: when("2026-10-05T03:31:02Z"), lastFailure: when("2026-10-06T03:34:40Z"), lastError: "LLM provider timed out after 120s (attempt 3/3)", maxAttempts: 3, attempts: 3, status: "degraded", deadman: false },
      { jobType: "benchmark", description: "Weekly smoke benchmark", cron: "0 6 * * 1", pool: "maintenance", nextRun: when("2026-10-12T06:00:00Z"), lastSuccess: when("2026-09-28T06:04:55Z"), maxAttempts: 3, attempts: 1, status: "stale", deadman: false },
    ],
  };
}
