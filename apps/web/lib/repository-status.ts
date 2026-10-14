import type { Repository, Tone } from "./types";

export function uncommittedCount(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

export function uncommittedMessage(count: number): string {
  return `${count} uncommitted change${count === 1 ? "" : "s"} not indexed yet`;
}

export function repositoryStatus(repo: Repository, uncommitted?: number | null): { tone: Tone; status: string } {
  const status = repo.freshness ?? (repo.behind === 0 ? "current" : "behind");
  const omitted = uncommittedCount(uncommitted);
  const note = omitted !== null && omitted > 0 ? uncommittedMessage(omitted) : "";
  if (status === "current") return note ? { tone: "warn", status: note } : { tone: "ok", status: "Up to date" };
  if (repo.behind !== null && repo.behind > 0) {
    return { tone: repo.behind > 20 ? "bad" : "warn", status: [`${repo.behind} changes not read yet`, note].filter(Boolean).join(" · ") };
  }
  const labels: Record<string, string> = {
    unindexed: "Not indexed yet", source_missing: "Source unavailable",
    stale: "Index freshness unverified", freshness_error: "Freshness check failed",
    error: "Freshness check failed", unknown: "Freshness unknown", behind: "Index is behind",
  };
  return { tone: status === "source_missing" || /error/.test(status) ? "bad" : "warn", status: [labels[status] ?? "Freshness unknown", note].filter(Boolean).join(" · ") };
}
