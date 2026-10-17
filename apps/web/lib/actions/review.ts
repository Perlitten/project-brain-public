"use server";
// Review page: approve, reject or withdraw a memory proposal. Every call goes out
// with the signed-in member's own key (lib/api.ts) and the proposal version the
// page showed, so Brain refuses self-approval, the wrong role and a stale view.
import { mutate } from "./gate";
import type { ActionResult } from "./types";

const REVIEW = ["/review", "/memory"];
const goodId = (n: number) => Number.isInteger(n) && n > 0;
const cleanReason = (reason?: string) => {
  const trimmed = reason?.trim();
  return trimmed ? trimmed.slice(0, 2000) : undefined;
};

async function decide(path: "approve" | "reject" | "withdraw", id: number, version: number, success: string, reason?: string): Promise<ActionResult> {
  if (!goodId(id) || !goodId(version)) return { ok: false, message: "That proposal doesn’t look right. Reload the page." };
  const res = await mutate(`/memory/proposals/${id}/${path}`, {
    body: { expected_version: version, reason: cleanReason(reason) },
    success,
    revalidate: REVIEW,
  });
  return { ok: res.ok, message: res.message };
}

/** Publishes the proposal as approved knowledge; Brain keeps the author and who approved it. */
export async function approveProposal(id: number, version: number, reason?: string): Promise<ActionResult> {
  return decide("approve", id, version, "Approved. Agents get it from their next context pack.", reason);
}

/** Closes the proposal; nothing reaches approved memory. */
export async function rejectProposal(id: number, version: number, reason?: string): Promise<ActionResult> {
  return decide("reject", id, version, "Rejected. Nothing was added to memory.", reason);
}

/** The author takes their own pending proposal back. */
export async function withdrawProposal(id: number, version: number): Promise<ActionResult> {
  return decide("withdraw", id, version, "Withdrawn.");
}
