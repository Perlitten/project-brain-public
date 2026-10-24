// Pair a task an agent ran with the context pack Brain gave it for that task.
// Brain records them separately, so we match on the task wording and the agent
// (and the project, when both sides name one).

import type { AgentRun, ContextPack } from "./types";

const norm = (s: string) => s.toLowerCase().replace(/\s+/g, " ").trim();

function pairs(run: AgentRun, pack: ContextPack): boolean {
  if (norm(run.task) !== norm(pack.task)) return false;
  if (norm(run.agent) !== norm(pack.consumer)) return false;
  if (run.repo && pack.repo && run.repo !== pack.repo) return false;
  return true;
}

export function packForRun(run: AgentRun, packs: ContextPack[]): ContextPack | undefined {
  return packs.find((p) => pairs(run, p));
}

export function runForPack(pack: ContextPack, runs: AgentRun[]): AgentRun | undefined {
  return runs.find((r) => pairs(r, pack));
}
