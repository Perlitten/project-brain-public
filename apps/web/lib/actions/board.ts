"use server";
// Board: release or renew your own lease, request a fresh run of a task, and
// integrate or reject an integration-queue entry. Every call goes out with the
// signed-in member's own key (lib/api.ts) and the fencing token or entry version
// the page showed, so Brain refuses the wrong holder, the wrong role, a
// non-independent reviewer and a stale view.
import { randomUUID } from "node:crypto";
import { mutate } from "./gate";
import type { ActionResult } from "./types";

const BOARD = ["/team/board"];
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SHA = /^[0-9a-f]{40}$/;
const BRANCH = /^[^\s]{1,255}$/;
const goodId = (n: number) => Number.isInteger(n) && n > 0;
const project = (id: string) => `/team/projects/${id.toLowerCase()}`;
const STALE = "That item doesn’t look right. Reload the page.";

export interface NewRun { runId: string; credential: string | null }

export async function releaseLease(projectId: string, leaseId: number, fencingToken: number): Promise<ActionResult> {
  if (!UUID.test(projectId) || !goodId(leaseId) || !goodId(fencingToken)) return { ok: false, message: STALE };
  const res = await mutate(`${project(projectId)}/leases/${leaseId}/release`, {
    body: { fencing_token: fencingToken },
    success: "Lease released. Others can claim the path now.",
    revalidate: BOARD,
  });
  return { ok: res.ok, message: res.message };
}

export async function renewLease(projectId: string, leaseId: number, fencingToken: number, ttlSeconds = 300): Promise<ActionResult> {
  if (!UUID.test(projectId) || !goodId(leaseId) || !goodId(fencingToken) || !Number.isInteger(ttlSeconds) || ttlSeconds < 10 || ttlSeconds > 3600) {
    return { ok: false, message: STALE };
  }
  const res = await mutate(`${project(projectId)}/leases/${leaseId}/renew`, {
    body: { fencing_token: fencingToken, ttl_seconds: ttlSeconds },
    success: `Lease renewed for ${Math.round(ttlSeconds / 60)} minutes.`,
    revalidate: BOARD,
  });
  return { ok: res.ok, message: res.message };
}

/**
 * A fresh run of the task: Brain resolves the current decisions and rules for it
 * and refuses while dependencies are not done. The run's credential is returned
 * once, for the member to hand to the agent that does the work.
 */
export async function requestRun(projectId: string, taskId: string, branch: string): Promise<ActionResult<NewRun>> {
  if (!UUID.test(projectId) || !UUID.test(taskId) || !BRANCH.test(branch)) return { ok: false, message: STALE };
  const res = await mutate<{ id?: unknown; credential?: unknown }>(`${project(projectId)}/tasks/${taskId.toLowerCase()}/runs`, {
    body: { branch, permissions: ["read"], idempotency_key: `board-${randomUUID()}` },
    success: "Run requested. Copy its key now: Brain shows it only once.",
    revalidate: BOARD,
  });
  if (!res.ok || !res.data || typeof res.data.id !== "string") return { ok: false, message: res.message };
  return { ok: true, message: res.message, data: { runId: res.data.id, credential: typeof res.data.credential === "string" ? res.data.credential : null } };
}

export async function integrateEntry(projectId: string, entryId: number, version: number, currentBaseSha?: string): Promise<ActionResult> {
  const base = currentBaseSha?.trim().toLowerCase() || undefined;
  if (!UUID.test(projectId) || !goodId(entryId) || !goodId(version) || (base !== undefined && !SHA.test(base))) {
    return { ok: false, message: base !== undefined && !SHA.test(base) ? "The current base must be a full 40-character commit SHA." : STALE };
  }
  const res = await mutate<{ status?: unknown; reason?: unknown }>(`${project(projectId)}/integration/${entryId}/integrate`, {
    body: { expected_version: version, current_base_sha: base },
    success: (d) => d.status === "integrated"
      ? "Integrated."
      : `Not integrated: ${d.status === "needs_rebase" ? "the run needs a rebase" : d.status === "stale_instructions" ? "its instructions changed" : String(d.status ?? "unknown")}. The verdict is on the board.`,
    revalidate: BOARD,
  });
  return { ok: res.ok, message: res.message };
}

export async function rejectEntry(projectId: string, entryId: number, version: number, reason: string): Promise<ActionResult> {
  const why = reason.trim().slice(0, 255);
  if (!UUID.test(projectId) || !goodId(entryId) || !goodId(version)) return { ok: false, message: STALE };
  if (!why) return { ok: false, message: "Give a reason: the member who queued it sees it." };
  const res = await mutate(`${project(projectId)}/integration/${entryId}/reject`, {
    body: { expected_version: version, reason: why },
    success: "Removed from the integration queue.",
    revalidate: BOARD,
  });
  return { ok: res.ok, message: res.message };
}
