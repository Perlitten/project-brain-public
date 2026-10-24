// An agent as one object: the name tasks and context packs report, joined to
// the identity (and keys) it holds in Access. Names that report tasks without
// an identity still get a page, so nothing an agent did is unreachable.
import "server-only";
import { cache } from "react";
import { getAccessView, getAgentRuns, getContextPacks, getMcpTools } from "./data";
import type { AgentRun, ContextPack, Identity, McpTool, Tone } from "./types";

export interface AgentSummary {
  /** The name agents report and the slug of its page. */
  name: string;
  identity: Identity | null;
  runs: AgentRun[];
  packs: ContextPack[];
  running: number;
  failed: number;
  completed: number;
  /** Most recent activity Brain recorded, as the source formats it. */
  lastSeen: string;
  tone: Tone;
  /** One line: what the agent is up to and whether its access is in order. */
  note: string;
}

export interface AgentsView {
  agents: AgentSummary[];
  /** False when Access could not be read; identities are then unknown, not absent. */
  accessReadable: boolean;
  tools: McpTool[] | null;
}

const ANON = new Set(["", "agent", "unknown", "an agent"]);
const FAILED = new Set(["failed", "error", "timed_out"]);
const RUNNING = new Set(["running", "retrying", "queued"]);
const same = (a: string, b: string) => a.toLowerCase() === b.toLowerCase();

function summarize(name: string, identity: Identity | null, runs: AgentRun[], packs: ContextPack[]): AgentSummary {
  const running = runs.filter((r) => RUNNING.has(r.status)).length;
  const failed = runs.filter((r) => FAILED.has(r.status)).length;
  const completed = runs.filter((r) => r.status === "completed").length;
  const lastSeen = identity?.lastSeen ?? runs[0]?.startedAt ?? packs[0]?.createdAt ?? "never";
  const tone: Tone = identity?.disabled ? "idle" : failed ? "warn" : running ? "ok" : runs.length || packs.length ? "ok" : "info";
  const parts: string[] = [];
  if (identity?.disabled) parts.push("disabled — none of its keys work");
  else if (running) parts.push(`${running} task${running === 1 ? "" : "s"} running`);
  else parts.push("idle");
  if (failed) parts.push(`${failed} failed`);
  if (identity) {
    if (!identity.disabled) parts.push(identity.activeKeys ? `${identity.activeKeys} active key${identity.activeKeys === 1 ? "" : "s"}` : "no active key");
  } else parts.push("no identity in Access");
  return { name, identity, runs, packs, running, failed, completed, lastSeen, tone, note: parts.join(" · ") };
}

/** Every agent Brain knows: identities of kind agent, plus every name that reported a task or took a pack. */
export const getAgents = cache(async (): Promise<AgentsView> => {
  const [access, runs, packs, tools] = await Promise.all([getAccessView(), getAgentRuns(), getContextPacks(500), getMcpTools()]);
  const identities = access.readable ? access.identities.filter((i) => i.kind === "agent") : [];
  const names = new Map<string, string>();
  const add = (n: string) => { if (!ANON.has(n.trim().toLowerCase()) && !names.has(n.toLowerCase())) names.set(n.toLowerCase(), n); };
  for (const i of identities) add(i.name);
  for (const r of runs) add(r.agent);
  for (const p of packs) add(p.consumer);
  const agents = [...names.values()].map((name) => summarize(
    name,
    identities.find((i) => same(i.name, name)) ?? null,
    runs.filter((r) => same(r.agent, name)),
    packs.filter((p) => same(p.consumer, name)),
  ));
  // Working agents first, then the ones with trouble, then by name.
  agents.sort((a, b) => (b.running - a.running) || (b.failed - a.failed) || Number(Boolean(a.identity?.disabled)) - Number(Boolean(b.identity?.disabled)) || a.name.localeCompare(b.name));
  return { agents, accessReadable: access.readable, tools };
});

/** One agent by the name on its page, or null when Brain has never heard of it. */
export const getAgent = cache(async (name: string): Promise<AgentSummary | null> => {
  const { agents } = await getAgents();
  return agents.find((a) => same(a.name, name)) ?? null;
});
