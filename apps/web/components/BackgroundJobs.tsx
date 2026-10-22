import { EmptyState, JobChip } from "./ui";
import { RetryRead } from "./RetryRead";
import type { Job } from "@/lib/types";

const jobWords: Record<string, string> = {
  "index.incremental": "Reading recent changes",
  reindex: "Re-reading the code",
  "embeddings.backfill": "Making new code searchable",
  "context_pack.build": "Building a context pack",
  "graph.sync": "Updating the code map",
  "eval.golden_tasks": "Checking answer quality",
  benchmark: "Checking answer quality",
  health_check: "Checking service health",
  self_diagnosis: "Looking for problems",
  nightly_maintenance: "Nightly maintenance",
};

export function BackgroundJobs({ jobs, unavailable = false }: { jobs: Job[]; unavailable?: boolean }) {
  if (!jobs.length && unavailable) return <EmptyState title="Background jobs unavailable" body="The recent-jobs request failed. This does not establish that the worker is stopped. Retry; if it persists, check Setup for worker status and the API logs." action={<RetryRead />} />;
  if (!jobs.length) return <EmptyState title="No background jobs reported" body="Recent indexing, maintenance and checks will appear here." />;
  return <ul aria-label="Recent background jobs">
    {jobs.map((job) => <li key={job.id} className="ledger__row job-row">
      <span className="ledger__time num">{job.startedAt}</span>
      <span className="job-row__main">
        <span>{jobWords[job.kind] ?? job.kind}</span>
        <span className="sub">{job.repo}{job.detail ? ` · ${job.detail}` : ""}{job.duration ? ` · took ${job.duration}` : ""}</span>
      </span>
      <JobChip status={job.status} />
    </li>)}
  </ul>;
}
