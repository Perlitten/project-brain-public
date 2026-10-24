"use server";
import { getCapabilities } from "../capabilities";
import { mutate } from "./gate";
import type { ActionResult } from "./types";

export async function dismissFindings(targets: { id: string; lastSeenAt: string }[], reason: string): Promise<ActionResult> {
  const text = reason.trim();
  if (!text || text.length > 1000) return { ok: false, message: "Provide a dismissal reason of 1–1000 characters." };
  if (!targets.length || targets.length > 100 || targets.some(t => !/^\d+$/.test(t.id) || !Number.isFinite(Date.parse(t.lastSeenAt)))) return { ok: false, message: "Reload the findings before dismissing them." };
  if (!(await getCapabilities()).supported.includes("finding_actions")) return { ok: false, message: "Update the API before dismissing findings." };
  let done = 0;
  for (const target of targets) {
    const result = await mutate(`/insights/${target.id}/dismiss`, { body: { reason: text, expected_last_seen_at: target.lastSeenAt }, success: "Finding dismissed.", revalidate: ["/insights"] });
    if (!result.ok) return { ok: false, message: `${done} of ${targets.length} findings dismissed. ${result.message}` };
    done++;
  }
  return { ok: true, message: `${done} finding${done === 1 ? "" : "s"} dismissed with your reason.` };
}
