// The dashboard inbox: everything that asks for a person, gathered from every
// screen into one list (most blocking first), plus what agents are doing right
// now. Each item names where it comes from and the one thing to do about it.
// Server-only and cached per request, so the shell can count it for the rail.
import "server-only";
import { cache } from "react";
import { getAccessView, getAgentRuns, getInsights, getJobs, getMcpTools, getSetupSteps } from "./data";
import { groupFindings } from "./finding-groups";
import { attention, getBoardView, type ProjectBoard } from "./board-view";
import { getReviewView } from "./review-view";
import { canReindex, repositoryAction } from "./repository-status";
import type { AgentRun, Repository, Tone } from "./types";

/** One row of the inbox: a stage word, what, why, and the way to act on it. */
export interface InboxItem {
  key: string;
  /** Where it comes from, for grouping and the rail count. */
  source: "board" | "finding" | "tool" | "key" | "review" | "setup" | "index" | "run" | "job";
  stage: string;
  tone: Tone;
  title: string;
  why: string;
  /** Where the full picture lives. */
  href: string;
  /** The one-click action the dashboard can offer, when there is one. */
  action?:
    | { kind: "reindex"; repoPath: string; label: string }
    | { kind: "revoke"; credentialId: number; label: string }
    | { kind: "link"; href: string; label: string };
}

/** An agent at work this minute. */
export interface AgentNow {
  key: string;
  agent: string;
  task: string;
  since: string;
  /** Where it runs: a repository name or a branch. */
  where: string;
  href: string;
  /** The board task it belongs to, when Brain coordinates it. */
  boardHref?: string;
}

export interface Inbox {
  needs: InboxItem[];
  agents: AgentNow[];
  /** How many of the items block work outright. */
  urgent: number;
  /** False when at least one source could not be measured. */
  complete: boolean;
  /** Human-readable source names that were unavailable. */
  unavailable: string[];
}

export interface InboxRepo {
  repo: Repository;
  tone: Tone;
  status: string;
  /** False when this project's corpus measurements could not be read. */
  available?: boolean;
}

const RANK: Record<Tone, number> = { bad: 0, warn: 1, info: 2, idle: 2, ok: 3 };
const RECENT_RUN_MS = 7 * 86_400_000;
const short = (sha: string) => sha.slice(0, 7);

function isRecentRun(r: AgentRun, now: number): boolean {
  const at = Date.parse(r.startedAt);
  return Number.isNaN(at) || now - at <= RECENT_RUN_MS;
}

function fromBoard(board: ProjectBoard, selfId: number | null): InboxItem[] {
  const title = (id: string) => board.tasks.find((t) => t.id === id)?.title ?? `task ${id.slice(0, 8)}`;
  const at = (id: string) => `/team/board#task-${id}`;
  const out: InboxItem[] = [];
  for (const a of attention(board, selfId)) {
    if (a.kind === "held") {
      const rebase = a.entry.status === "needs_rebase";
      out.push({
        key: `board-held-${a.entry.id}`, source: "board", tone: "bad",
        stage: rebase ? "Can’t land" : "Stale brief",
        title: title(a.taskId),
        why: rebase
          ? `The run started from ${short(a.entry.baseSha)}; the base moved${a.entry.checkedBaseSha ? ` to ${short(a.entry.checkedBaseSha)}` : ""}. Rebase and run again.`
          : `${(a.entry.reason || "A decision or rule changed since the run started").replace(/[.\s]+$/, "")}. A fresh run picks up the current versions.`,
        href: at(a.taskId),
        action: { kind: "link", href: at(a.taskId), label: "Resolve" },
      });
    } else if (a.kind === "integrate") {
      out.push({
        key: `board-integrate-${a.entry.id}`, source: "board", tone: "info", stage: "Your review",
        title: title(a.taskId),
        why: `First in the integration queue of ${board.name}; it lands when you approve it.`,
        href: at(a.taskId),
        action: { kind: "link", href: at(a.taskId), label: "Review" },
      });
    } else if (a.kind === "lease") {
      out.push({
        key: `board-lease-${a.lease.id}`, source: "board", tone: "warn", stage: "Your lease",
        title: a.lease.resource,
        why: `You hold this path${a.taskId ? ` for “${title(a.taskId)}”` : ""}; others wait until you release or renew it.`,
        href: a.taskId ? at(a.taskId) : "/team/board",
        action: { kind: "link", href: a.taskId ? at(a.taskId) : "/team/board", label: "Open" },
      });
    } else {
      const o = a.overlap;
      out.push({
        key: `board-overlap-${a.taskId}-${o.otherTaskId ?? o.leaseId ?? o.resource}`, source: "board", tone: "warn", stage: "Overlap",
        title: title(a.taskId),
        why: o.kind === "lease"
          ? `Needs ${o.resource}, leased by another member.`
          : `${o.kind === "dependency" ? "Imports code from" : "Changes the same paths as"} “${o.otherTitle ?? title(o.otherTaskId ?? "")}”.`,
        href: at(a.taskId),
        action: { kind: "link", href: at(a.taskId), label: "What can I do?" },
      });
    }
  }
  return out;
}

const loadInboxSources = cache((admin: boolean) => Promise.all([
  getBoardView(),
  getInsights(),
  admin ? getMcpTools() : Promise.resolve([]),
  admin ? getAccessView() : Promise.resolve(null),
  getReviewView(admin),
  admin ? getSetupSteps() : Promise.resolve([]),
  getAgentRuns(),
  admin ? getJobs() : Promise.resolve(null),
]));

/** Every item that asks for a person, most blocking first, and what agents are doing now. */
export const getInbox = cache(async (admin: boolean, repos: InboxRepo[]): Promise<Inbox> => {
  const [boardView, insights, tools, access, review, steps, runs, jobs] = await loadInboxSources(admin);

  const needs: InboxItem[] = [];
  const unavailable = new Set<string>();

  if (boardView.kind === "unavailable") unavailable.add("Board");
  if (boardView.kind === "ok" && boardView.projects.some((p) => p.missing.length > 0)) unavailable.add("Board measurements");
  if (admin && tools === null) unavailable.add("tool health");
  if (admin && (!access || !access.readable)) unavailable.add("Access");
  if (review.kind === "unavailable") unavailable.add("memory review");
  if (admin && jobs === null) unavailable.add("background jobs");
  if (repos.some((r) => r.available === false)) unavailable.add("project memory");

  // 1. The Board: runs that can't land, reviews waiting on you, overlaps.
  if (boardView.kind === "ok") for (const board of boardView.projects) needs.push(...fromBoard(board, boardView.selfId));

  // 2. Projects whose memory is stale or unreadable.
  for (const { repo, tone, status } of repos) {
    if (tone === "ok") continue;
    const next = repositoryAction(repo);
    needs.push({
      key: `index-${repo.slug}`, source: "index", tone, stage: tone === "bad" ? "Memory stale" : "Behind",
      title: repo.name,
      why: `${status}. Agents working here get an older picture of the code.`,
      href: `/projects?repo=${encodeURIComponent(repo.slug)}`,
      action: admin && canReindex(repo) ? { kind: "reindex", repoPath: repo.path, label: "Re-index" } : { kind: "link", href: next.href, label: next.label.replace(/ for .*$/, "") },
    });
  }

  // 3. Findings that break a rule.
  for (const g of groupFindings(insights.filter((i) => i.tone === "bad"))) {
    const f = g.rep;
    needs.push({
      key: `finding-${f.id}`, source: "finding", tone: "bad", stage: "Rule broken",
      title: f.title, why: f.evidence, href: `/insights?q=${encodeURIComponent(f.id)}`,
      action: { kind: "link", href: `/insights?q=${encodeURIComponent(f.id)}`, label: "Open finding" },
    });
  }

  // 4. Agent tasks that ended badly.
  const nowMs = Date.now();
  for (const r of runs.filter((r) => (r.status === "failed" || r.status === "timed_out" || r.status === "error") && isRecentRun(r, nowMs))) {
    needs.push({
      key: `run-${r.id}`, source: "run", tone: "warn", stage: r.status === "timed_out" ? "Timed out" : "Failed",
      title: r.task,
      why: `${r.agent} · ${r.startedAt}${r.duration && r.duration !== "—" ? ` · ran ${r.duration}` : ""}${r.packHit === false ? " · answered without a context pack" : ""}.`,
      href: `/runs?q=${encodeURIComponent(r.id)}`,
      action: { kind: "link", href: `/runs?q=${encodeURIComponent(r.id)}`, label: "Open task" },
    });
  }

  // 5. Agent tools that fail.
  for (const t of (tools ?? []).filter((t) => t.status === "bad")) {
    const rate = t.errorRate !== undefined ? `${(t.errorRate * 100).toFixed(1)}% of calls fail` : "failing";
    needs.push({
      key: `tool-${t.name}`, source: "tool", tone: "bad", stage: "Tool failing",
      title: t.name,
      why: `${rate}${t.p95ms !== undefined ? `, p95 ${(t.p95ms / 1000).toFixed(1)}s` : ""}${t.calls24h !== undefined ? ` over ${t.calls24h.toLocaleString("en-US")} calls today` : ""}. Agents calling it get errors.`,
      href: `/mcp#${encodeURIComponent(t.name)}`,
      action: { kind: "link", href: `/mcp#${encodeURIComponent(t.name)}`, label: "Open tool" },
    });
  }

  // 6. Background jobs that failed.
  for (const j of (jobs ?? []).filter((j) => j.status === "failed" || j.status === "error")) {
    needs.push({
      key: `job-${j.id}`, source: "job", tone: "warn", stage: "Job failed",
      title: j.kind, why: `${j.repo} · ${j.startedAt}${j.detail ? ` · ${j.detail}` : ""}.`,
      href: `/activity#jobs`, action: { kind: "link", href: "/activity#jobs", label: "Open jobs" },
    });
  }

  // 7. Memory proposals waiting for a reviewer.
  if (review.kind === "ok" && review.queue.length) {
    const n = review.queue.length;
    needs.push({
      key: "review", source: "review", tone: "info", stage: "To review",
      title: n === 1 ? review.queue[0].title : `${n} memory proposals`,
      why: n === 1 ? `A ${review.queue[0].kind} proposed by an agent; it reaches agents once approved.` : "Decisions and rules proposed by agents; they reach agents once approved.",
      href: "/review", action: { kind: "link", href: "/review", label: "Review" },
    });
  }

  // 8. Keys that expire soon or were never used.
  if (access?.readable) {
    for (const i of access.identities) {
      if (i.disabled) continue;
      for (const k of i.keys) {
        if (k.status !== "active") continue;
        if (k.expiringSoon) {
          needs.push({
            key: `key-exp-${k.id}`, source: "key", tone: "warn", stage: "Key expiring",
            title: `${i.name} · key ${k.id}`, why: `Expires ${k.expires}; ${i.kind === "agent" ? "the agent" : i.name} loses access then. Mint a new key before it does.`,
            href: `/admin#identity-${i.id}`, action: { kind: "link", href: `/admin?new=key&for=${i.id}#identity-${i.id}`, label: "New key" },
          });
        } else if (k.neverUsed) {
          needs.push({
            key: `key-unused-${k.id}`, source: "key", tone: "info", stage: "Key unused",
            title: `${i.name} · key ${k.id}`, why: `Created ${k.created}, never used. Revoke it if it was never handed over.`,
            href: `/admin#identity-${i.id}`, action: { kind: "revoke", credentialId: k.id, label: "Revoke" },
          });
        }
      }
    }
  }

  // 9. Setup steps still open.
  const open = steps.filter((s) => !s.done);
  if (open.length) {
    needs.push({
      key: "setup", source: "setup", tone: "info", stage: "Setup",
      title: open[0].title, why: open.length > 1 ? `${open.length} steps left before agents get the full picture.` : "The last step before agents get the full picture.",
      href: "/setup", action: { kind: "link", href: "/setup", label: "Continue setup" },
    });
  }

  needs.sort((a, b) => RANK[a.tone] - RANK[b.tone]);

  // Agents right now: runs in progress, joined to the board task where there is one.
  const running = runs.filter((r) => r.status === "running" || r.status === "retrying");
  const agents: AgentNow[] = running.map((r: AgentRun) => ({
    key: r.id, agent: r.agent, task: r.task, since: r.startedAt, where: r.repo ?? "", href: `/runs?q=${encodeURIComponent(r.id)}`,
  }));
  if (boardView.kind === "ok") {
    for (const board of boardView.projects) {
      const groups = new Set(board.tasks.map((t) => t.parentId).filter((id): id is string => id !== null));
      for (const t of board.tasks) {
        if (t.column !== "running" || groups.has(t.id)) continue;
        const match = agents.find((a) => a.task === t.title);
        if (match) { match.boardHref = `/team/board#task-${t.id}`; continue; }
        agents.push({ key: `board-${t.id}`, agent: "agent", task: t.title, since: "", where: t.branch, href: `/team/board#task-${t.id}`, boardHref: `/team/board#task-${t.id}` });
      }
    }
  }

  return {
    needs,
    agents,
    urgent: needs.filter((n) => n.tone === "bad").length,
    complete: unavailable.size === 0,
    unavailable: [...unavailable],
  };
});
