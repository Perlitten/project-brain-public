"use server";
// Background jobs: start the common ones and follow any job to the end.
import { brainFetch } from "../api";
import { guard, mutate } from "./gate";
import type { ActionResult, JobSnapshot } from "./types";

const FINISHED = new Set(["completed", "degraded", "failed", "cancelled"]);

export async function getJob(id: string): Promise<JobSnapshot | null> {
  if (guard()) return null;
  if (!/^[\w-]{1,80}$/.test(id)) return null;
  const job = await brainFetch<Record<string, unknown>>(`/jobs/${id}`, { fresh: true });
  if (!job) return null;
  const status = String(job.status ?? "unknown");
  const progress = job.progress && typeof job.progress === "object" ? (job.progress as Record<string, unknown>) : {};
  // Indexing reports {phase, files:{discovered, processed}}; others may send percent/stage.
  const files = progress.files && typeof progress.files === "object" ? (progress.files as Record<string, unknown>) : null;
  const seen = files && typeof files.discovered === "number" ? files.discovered : 0;
  const doneFiles = files && typeof files.processed === "number" ? files.processed + (typeof files.skipped === "number" ? files.skipped : 0) : 0;
  const pct =
    typeof progress.percent === "number"
      ? progress.percent
      : seen > 0
        ? Math.min(100, Math.round((doneFiles / seen) * 100))
        : undefined;
  const phase = typeof progress.phase === "string" ? progress.phase : typeof progress.stage === "string" ? progress.stage : undefined;
  const stage = phase ? (seen > 0 ? `${phase} · ${doneFiles}/${seen} files` : phase) : typeof progress.message === "string" ? progress.message : undefined;
  const err = typeof job.error === "string" && job.error ? job.error : undefined;
  return {
    id,
    status,
    done: FINISHED.has(status),
    ok: status === "completed",
    detail: err ?? stage,
    progress: pct,
  };
}

export async function startReindex(opts: { repoPath?: string; clean?: boolean } = {}): Promise<ActionResult> {
  return mutate("/jobs/reindex", {
    body: { repo_path: opts.repoPath || undefined, clean: Boolean(opts.clean), verify_after: true },
    success: opts.clean ? "Full re-index queued — Brain will re-read every file." : "Re-index queued — Brain will read what changed.",
    revalidate: ["/", "/indexing", "/setup"],
  });
}

export async function startHealthCheck(): Promise<ActionResult> {
  return mutate("/jobs/health-check", { body: {}, success: "Health check queued.", revalidate: ["/", "/setup"] });
}

export async function startEmbeddingVerify(repoPath?: string): Promise<ActionResult> {
  return mutate("/jobs/embedding-verify", {
    body: repoPath ? { repo_path: repoPath } : {},
    success: "Embedding check queued — Brain will confirm every chunk has a vector.",
    revalidate: ["/indexing"],
  });
}

export async function startEmbeddingBackfill(repoPath?: string): Promise<ActionResult> {
  return mutate("/jobs/embedding-backfill", {
    body: repoPath ? { repo_path: repoPath } : {},
    success: "Backfill queued — missing vectors will be filled in.",
    revalidate: ["/indexing"],
  });
}

export async function startInsights(): Promise<ActionResult> {
  return mutate("/jobs/proactive-insights", { body: {}, success: "Brain is looking for new findings.", revalidate: ["/insights", "/"] });
}

export async function startSelfDiagnosis(): Promise<ActionResult> {
  return mutate("/jobs/self-diagnosis", { body: {}, success: "Self-diagnosis queued.", revalidate: ["/", "/logs"] });
}

export async function startBenchmark(): Promise<ActionResult> {
  return mutate("/jobs/benchmark", { body: {}, success: "Search benchmark queued.", revalidate: ["/reranker", "/reports"] });
}

export async function retryJob(jobId: string): Promise<ActionResult> {
  if (!/^[\w-]{1,80}$/.test(jobId)) return { ok: false, message: "That job id doesn’t look right." };
  return mutate(`/operations/jobs/${jobId}/retry`, { body: {}, success: "Job sent back to the queue.", revalidate: ["/", "/indexing"] });
}
