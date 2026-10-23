import type { Repository, Tone } from "./types";

export function uncommittedCount(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

export function uncommittedMessage(count: number): string {
  return `${count} uncommitted change${count === 1 ? "" : "s"} not indexed yet`;
}

// Same rules as brain.indexers.source_mirror.redact_url: "https://user:token@host",
// "oauth2:token@host/org/repo" and "token@host/org/repo" lose their userinfo;
// query/fragment data is removed; scp-style "git@host:org/repo" stays readable.
const SCHEME_USERINFO = /([A-Za-z][A-Za-z0-9+.-]*:\/\/)[^/@\s]+@/g;
const BARE_USERINFO = /(?<![^\s'"(<=,;])(?:[^\s/@:'"]+:[^\s/@'"]*|[^\s/@:'"]+(?=@[^\s/@:'"]+\/))@/g;
const REMOTE_TOKEN = /(?:[A-Za-z][A-Za-z0-9+.-]*:\/\/|\*\*\*@)[^\s'"]+/g;

/** Origin remotes and poll errors are redacted by the API; rendering them redacts again. */
export function redactRemote(text: string): string {
  return text
    .replace(SCHEME_USERINFO, "$1***@")
    .replace(BARE_USERINFO, "***@")
    .replace(REMOTE_TOKEN, (remote) => remote.split("?", 1)[0].split("#", 1)[0]);
}

export const SOURCE_UNAVAILABLE_HELP = "The Brain server cannot read this repository folder. Check that the checkout exists inside the API and worker containers, is mounted at the registered path and is readable. Reindex after restoring access.";

/** A missing checkout needs source access restored before an index run helps. */
export function repositoryAction(repo: Repository): { href: string; label: string } {
  const scope = `?repo=${encodeURIComponent(repo.slug)}`;
  return repo.freshness === "source_missing"
    ? { href: `/projects${scope}#source`, label: `Check source for ${repo.name}` }
    : { href: `/indexing${scope}`, label: `Open index runs for ${repo.name}` };
}

/** An index run can only help once Brain can read the checkout at its registered path. */
export function canReindex(repo: Repository): boolean {
  return repo.freshness !== "source_missing" && Boolean(repo.path?.trim());
}

export function repositorySummary(repositories: Repository[], uncommitted: ReadonlyMap<number, number | null> = new Map()): string {
  const missing = repositories.filter((r) => r.freshness === "source_missing").length;
  const behind = repositories.filter((r) => r.freshness !== "source_missing" && (r.freshness === "behind" || (r.behind ?? 0) > 0)).length;
  const omitted = repositories.filter(r => r.freshness === "current" && (uncommittedCount(uncommitted.get(r.id)) ?? 0) > 0).length;
  const other = repositories.filter((r) => r.freshness !== "source_missing" && r.freshness !== "behind" && !(r.behind && r.behind > 0) && repositoryStatus(r).tone !== "ok").length;
  const notes = [omitted ? `${omitted} with uncommitted changes omitted by the last index` : "", missing ? `${missing} source unavailable` : "", behind ? `${behind} behind their code` : "", other ? `${other} need a freshness check or indexing` : ""].filter(Boolean);
  return notes.length ? `${repositories.length} projects: ${notes.join("; ")}` : `All ${repositories.length} projects are up to date`;
}

export function repositoryStatus(repo: Repository, uncommitted?: number | null): { tone: Tone; status: string } {
  const status = repo.freshness ?? (repo.behind === 0 ? "current" : "behind");
  const omitted = uncommittedCount(uncommitted);
  const note = omitted !== null && omitted > 0 ? uncommittedMessage(omitted) : "";
  if (status === "current") return note ? { tone: "warn", status: note } : { tone: "ok", status: "Up to date" };
  if (status !== "source_missing" && repo.behind !== null && repo.behind > 0) {
    return { tone: repo.behind > 20 ? "bad" : "warn", status: [`${repo.behind} changes not read yet`, note].filter(Boolean).join(" · ") };
  }
  const labels: Record<string, string> = {
    unindexed: "Not indexed yet", source_missing: "Source unavailable",
    stale: "Index freshness unverified", freshness_error: "Freshness check failed",
    error: "Freshness check failed", unknown: "Freshness unknown", behind: "Index is behind",
  };
  return { tone: status === "source_missing" || /error/.test(status) ? "bad" : "warn", status: [labels[status] ?? "Freshness unknown", note].filter(Boolean).join(" · ") };
}
