// Data for the Review page: memory proposals waiting for an independent reviewer
// (team state only; solo owners write decisions and rules directly). Read with the
// signed-in member's own key (lib/api.ts), fresh on every render. Brain decides who
// sees what: team admins read every queue, a Reviewer reads the queues of their
// projects, everyone reads their own proposals.
import "server-only";
import { apiConfigured, brainRead } from "./api";
import { getRepositories } from "./data";

export type ProposalKind = "decision" | "rule" | "learning";
export type ProposalState = "pending" | "approved" | "rejected" | "withdrawn";

export interface ProposalRow {
  id: number;
  kind: ProposalKind;
  state: ProposalState;
  /** Compare-and-swap token: approve/reject send the version the reviewer saw. */
  version: number;
  /** Repository path, or null for system-global knowledge (team admins review it). */
  repoPath: string | null;
  title: string;
  /** Short text of what is proposed (description or statement). */
  summary: string;
  authorId: number | null;
  reviewerId: number | null;
  reason: string | null;
  /** An edit to an existing approved item rather than a new one. */
  edit: boolean;
  createdAt: string | null;
  decidedAt: string | null;
}

export type ReviewView =
  | { kind: "ok"; queue: ProposalRow[]; mine: ProposalRow[]; queueScopes: number }
  | { kind: "unavailable" };

interface RawProposal {
  id?: unknown;
  kind?: unknown;
  state?: unknown;
  version?: unknown;
  repo_path?: unknown;
  title?: unknown;
  payload?: unknown;
  author_principal_id?: unknown;
  reviewed_by_principal_id?: unknown;
  decision_reason?: unknown;
  target_key?: unknown;
  created_at?: unknown;
  decided_at?: unknown;
}

const KINDS: readonly ProposalKind[] = ["decision", "rule", "learning"];
const STATES: readonly ProposalState[] = ["pending", "approved", "rejected", "withdrawn"];
const num = (v: unknown) => (typeof v === "number" && Number.isInteger(v) ? v : null);
const text = (v: unknown) => (typeof v === "string" && v ? v : null);
/** Most members review a handful of repositories; cap the fan-out so the page stays quick. */
const MAX_SCOPES = 25;

function toRow(raw: RawProposal): ProposalRow | null {
  const id = num(raw.id);
  const version = num(raw.version);
  const kind = KINDS.find((k) => k === raw.kind);
  const state = STATES.find((s) => s === raw.state);
  if (id === null || version === null || !kind || !state) return null;
  const payload = raw.payload && typeof raw.payload === "object" ? (raw.payload as Record<string, unknown>) : {};
  return {
    id,
    kind,
    state,
    version,
    repoPath: text(raw.repo_path),
    title: text(raw.title) ?? `Proposal ${id}`,
    summary: text(payload.description) ?? text(payload.statement) ?? "",
    authorId: num(raw.author_principal_id),
    reviewerId: num(raw.reviewed_by_principal_id),
    reason: text(raw.decision_reason),
    edit: kind !== "learning" && text(raw.target_key) !== null,
    createdAt: text(raw.created_at)?.slice(0, 16).replace("T", " ") ?? null,
    decidedAt: text(raw.decided_at)?.slice(0, 16).replace("T", " ") ?? null,
  };
}

function rows(data: unknown): ProposalRow[] {
  const list = data && typeof data === "object" ? (data as { proposals?: unknown }).proposals : null;
  return Array.isArray(list) ? list.map((r) => toRow(r as RawProposal)).filter((r): r is ProposalRow => r !== null) : [];
}

export async function getReviewView(admin: boolean): Promise<ReviewView> {
  if (!apiConfigured) return { kind: "ok", queue: [], mine: [], queueScopes: 0 };
  const mine = await brainRead<unknown>("/memory/proposals?mine=true&state=all&limit=50", { fresh: true });
  if (!mine.ok && mine.status !== 401 && mine.status !== 403) return { kind: "unavailable" };

  let queue: ProposalRow[] = [];
  let queueScopes = 0;
  if (admin) {
    // Team admins review every scope, system-global included, in one list.
    const all = await brainRead<unknown>("/memory/proposals?state=pending&limit=200", { fresh: true });
    if (all.ok) {
      queue = rows(all.data);
      queueScopes = 1;
    }
  } else {
    // A Reviewer's queues are per repository; Brain answers 403 where they don't review.
    const repos = (await getRepositories()).filter((r) => r.path).slice(0, MAX_SCOPES);
    const answers = await Promise.all(
      repos.map((r) => brainRead<unknown>(`/memory/proposals?state=pending&repo_path=${encodeURIComponent(r.path)}`, { fresh: true })),
    );
    const seen = new Set<number>();
    for (const answer of answers) {
      if (!answer.ok) continue;
      queueScopes += 1;
      for (const row of rows(answer.data)) {
        if (!seen.has(row.id)) {
          seen.add(row.id);
          queue.push(row);
        }
      }
    }
    queue.sort((a, b) => b.id - a.id);
  }
  return { kind: "ok", queue, mine: mine.ok ? rows(mine.data) : [], queueScopes };
}
