"use server";
// "Run now" for a scheduled job: the same POST /jobs/* a schedule slot uses.
import { mutate } from "./gate";
import type { ActionResult } from "./types";

const TRIGGERS: Record<string, string> = {
  nightly_maintenance: "/jobs/nightly-maintenance",
  health_check: "/jobs/health-check",
  self_diagnosis: "/jobs/self-diagnosis",
  benchmark: "/jobs/benchmark",
};

export async function runScheduledJob(jobType: string): Promise<ActionResult> {
  const path = TRIGGERS[jobType];
  if (!path) return { ok: false, message: "That job can’t be started from here." };
  return mutate(path, { body: {}, success: `${jobType.replace(/_/g, " ")} queued.`, revalidate: ["/settings"] });
}
