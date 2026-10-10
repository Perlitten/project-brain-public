"use server";
// The rail's live vital signs: is Brain answering, how fast, which build,
// what it remembers and which models it thinks with. Read-only, so it runs in
// every mode except demo.
import { apiConfigured, brainFetch } from "../api";
import { getRepository } from "../data";

export interface Pulse {
  at: number;
  reachable: boolean;
  /** Dashboard server → Brain API round trip for /health. */
  latencyMs?: number;
  healthy?: boolean;
  /** Services that aren't healthy, by name. */
  down: string[];
  /** Scheduled jobs reported overdue by Brain, distinct from a service outage. */
  warnings?: string[];
  version?: string;
  build?: string;
  llm?: string;
  embedding?: string;
  index?: { status: string; at?: string };
  setup?: { done: number; total: number; next?: string; nextId?: string };
}

type Json = Record<string, unknown>;
const o = (v: unknown): Json => (v && typeof v === "object" && !Array.isArray(v) ? (v as Json) : {});
const s = (v: unknown) => (typeof v === "string" && v ? v : undefined);

export async function getPulse(repoSlug?: string): Promise<Pulse | null> {
  if (!apiConfigured) return null;
  // All four at once: the rail shows "Checking…" until the slowest answers,
  // and waiting for /health first only added a round trip.
  const t0 = performance.now();
  let latencyMs = 0;
  const [health, version, indexing, setup] = await Promise.all([
    brainFetch<Json>("/health", { fresh: true }).then((h) => ((latencyMs = Math.round(performance.now() - t0)), h)),
    brainFetch<Json>("/api/version", { revalidate: 300 }),
    repoSlug === "" ? Promise.resolve(null) : getRepository(repoSlug).then(repo => repo.id ? brainFetch<Json>(`/api/web/index-runs?repository_id=${repo.id}&page_size=1`, { fresh: true }) : null),
    brainFetch<Json>("/api/setup/status", { fresh: true }),
  ]);
  if (!health) return { at: Date.now(), reachable: false, down: [] };
  const details = o(health.details);
  const down = Object.entries(details)
    .filter(([, v]) => o(v).status !== "healthy")
    .map(([k]) => k);
  const stale = o(health.scheduler).stale;
  const run = o((Array.isArray(indexing?.runs) ? indexing.runs : [])[0]);
  const steps = (Array.isArray(setup?.steps) ? setup.steps : []).map(o);
  const provider = o(steps.find((st) => st.id === "provider"));
  const sha = s(o(version).build_sha);
  return {
    at: Date.now(),
    reachable: true,
    latencyMs,
    healthy: health.status === "ok",
    down,
    warnings: Array.isArray(stale) ? stale.filter((job): job is string => typeof job === "string") : [],
    version: s(o(version).version) ?? s(health.version),
    build: sha && sha !== "unknown" ? sha.slice(0, 7) : undefined,
    llm: s(o(provider.llm).provider),
    embedding: s(o(provider.embedding).provider),
    index: run.status ? { status: String(run.status), at: s(run.completed_at) ?? s(run.started_at) } : undefined,
    setup: steps.length
      ? {
          done: steps.filter((st) => st.status === "done").length,
          total: steps.length,
          next: s(steps.find((st) => st.status !== "done")?.title),
          nextId: s(steps.find((st) => st.status !== "done")?.id),
        }
      : undefined,
  };
}
