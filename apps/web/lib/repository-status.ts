import type { Repository, Tone } from "./types";

export function repositoryStatus(repo: Repository): { tone: Tone; status: string } {
  const status = repo.freshness ?? (repo.behind === 0 ? "current" : "behind");
  if (status === "current") return { tone: "ok", status: "Up to date" };
  if (repo.behind !== null && repo.behind > 0) {
    return { tone: repo.behind > 20 ? "bad" : "warn", status: `${repo.behind} changes not read yet` };
  }
  const labels: Record<string, string> = {
    unindexed: "Not indexed yet", source_missing: "Source unavailable",
    stale: "Index freshness unverified", freshness_error: "Freshness check failed",
    error: "Freshness check failed", unknown: "Freshness unknown", behind: "Index is behind",
  };
  return { tone: status === "source_missing" || /error/.test(status) ? "bad" : "warn", status: labels[status] ?? "Freshness unknown" };
}
