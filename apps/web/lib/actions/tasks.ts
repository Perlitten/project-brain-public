"use server";
// Agent tasks: the one thing a person can do to a task from the dashboard is
// stop it. Brain marks the task cancelled and the agent sees the flag on its
// next check-in; the write carries the version the page showed, so a stale
// row gets a 409 instead of fighting the agent.
import { mutate } from "./gate";
import type { ActionResult } from "./types";

const SCREENS = ["/runs", "/agents", "/", "/team/board"];
const UUID = /^[0-9a-f-]{32,36}$/i;

export async function cancelTask(taskId: string, version: number, note?: string): Promise<ActionResult> {
  if (typeof taskId !== "string" || !UUID.test(taskId)) return { ok: false, message: "That task doesn’t look right. Reload the page." };
  if (!Number.isInteger(version) || version < 1) return { ok: false, message: "That task doesn’t look right. Reload the page." };
  const res = await mutate(`/harness/tasks/${encodeURIComponent(taskId)}`, {
    method: "PATCH",
    body: { status: "cancelled", expected_task_version: version, ...(typeof note === "string" && note.trim() ? { note: note.trim().slice(0, 1000) } : {}) },
    success: "Cancelled — the agent is told to stop at its next check-in.",
    revalidate: SCREENS,
  });
  return { ok: res.ok, message: res.message };
}

const PRIORITIES = new Set(["P0", "P1", "P2", "P3"]);
const clean = (v: unknown, max: number) => (typeof v === "string" ? v.trim().slice(0, max) : "");

/** Puts a task on the queue for an agent to claim; Brain hands it a context pack when it starts. */
export async function createTask(input: { title: string; goal: string; repoPath: string; targetAgent?: string; priority?: string }): Promise<ActionResult> {
  const title = clean(input?.title, 300);
  const goal = clean(input?.goal, 10000);
  const repoPath = clean(input?.repoPath, 1000);
  if (!title) return { ok: false, message: "A task needs a title." };
  if (!goal) return { ok: false, message: "Say what done looks like — the agent reads this." };
  if (!repoPath) return { ok: false, message: "Pick the project the task is for." };
  const targetAgent = clean(input?.targetAgent, 100);
  const priority = PRIORITIES.has(clean(input?.priority, 2)) ? clean(input?.priority, 2) : "P2";
  const res = await mutate(`/harness/tasks`, {
    body: { title, goal, repo_path: repoPath, owner_agent: "dashboard", target_agent: targetAgent || undefined, priority },
    success: targetAgent ? `Queued for ${targetAgent} — it shows here as soon as the agent claims it.` : "Queued — the next agent that asks for work claims it.",
    revalidate: SCREENS,
  });
  return { ok: res.ok, message: res.message };
}
