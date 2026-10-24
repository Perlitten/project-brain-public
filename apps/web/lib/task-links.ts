// Pair a task an agent ran with the context pack Brain gave it for that task.
// Brain records them separately, so a link is only safe when task, agent and
// project all match and that match is unique. Ambiguity deliberately yields no
// link instead of pointing at another repository's task.

import type { AgentRun, ContextPack } from "./types";

const norm = (s: string) => s.toLowerCase().replace(/\s+/g, " ").trim();

function pairs(run: AgentRun, pack: ContextPack): boolean {
  if (norm(run.task) !== norm(pack.task)) return false;
  if (norm(run.agent) !== norm(pack.consumer)) return false;
  return Boolean(run.repo && pack.repo && run.repo === pack.repo);
}

function unique<T>(rows: T[]): T | undefined {
  return rows.length === 1 ? rows[0] : undefined;
}

export function packForRun(run: AgentRun, packs: ContextPack[]): ContextPack | undefined {
  return unique(packs.filter((p) => pairs(run, p)));
}

export function runForPack(pack: ContextPack, runs: AgentRun[]): AgentRun | undefined {
  return unique(runs.filter((r) => pairs(r, pack)));
}
