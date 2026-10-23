"use server";
// Memory page: edit, accept or retire a decision or rule. Brain has no separate
// update endpoint — a write to the same title (decision) or rule id (rule) in
// the same scope replaces the stored row and keeps the old text as history.
// Every write carries the version the page showed, so a stale view gets a 409
// instead of overwriting someone else's edit. In team mode a member's write
// becomes a proposal for review rather than a direct change.
import { mutate } from "./gate";
import type { ActionResult } from "./types";
import type { DecisionWrite, RuleWrite } from "../types";

const MEMORY = ["/memory", "/review"];
const SEVERITIES = new Set(["critical", "high", "medium", "low", "info"]);
const text = (value: unknown, max: number) => (typeof value === "string" ? value.trim().slice(0, max) : "");
const goodVersion = (n: unknown): n is number => typeof n === "number" && Number.isInteger(n) && n > 0;
const scope = (repoPath: unknown) => text(repoPath, 1000) || undefined;
const success = (done: string) => (data: Record<string, unknown>) =>
  data.status === "proposed" ? "Sent for review — it reaches agents once a reviewer approves it." : done;
const STALE = "That item doesn’t look right. Reload the page.";

async function writeDecision(target: DecisionWrite, fields: { description?: string; reason?: string; status?: string }, done: string): Promise<ActionResult> {
  const title = text(target?.title, 500);
  if (!title || !goodVersion(target?.version)) return { ok: false, message: STALE };
  const res = await mutate("/decisions", {
    body: { title, repo_path: scope(target.repoPath), expected_version: target.version, ...fields },
    success: success(done),
    revalidate: MEMORY,
  });
  return { ok: res.ok, message: res.message };
}

/** Rewrites the decision's text; the previous wording stays in its history. */
export async function saveDecision(target: DecisionWrite, edit: { description: string; reason: string }): Promise<ActionResult> {
  const description = text(edit?.description, 10000);
  if (!description) return { ok: false, message: "A decision needs a description." };
  return writeDecision(target, { description, reason: text(edit?.reason, 5000) }, "Saved. Agents get the new wording from their next context pack.");
}

/** Makes a proposed decision active, so agents start receiving it. */
export async function acceptDecision(target: DecisionWrite): Promise<ActionResult> {
  return writeDecision(target, { status: "active" }, "Accepted. Agents get it from their next context pack.");
}

/** Stops delivering the decision; the record stays as history. */
export async function retireDecision(target: DecisionWrite): Promise<ActionResult> {
  return writeDecision(target, { status: "deprecated" }, "Retired. Agents no longer receive it.");
}

// A rule write replaces every field, so each one goes back exactly as stored
// unless the edit changes it.
async function writeRule(target: RuleWrite, fields: { description?: string; severity?: string; status?: string }, done: string): Promise<ActionResult> {
  const ruleId = text(target?.ruleId, 200);
  const name = text(target?.name, 500);
  if (!ruleId || !name || !goodVersion(target?.version)) return { ok: false, message: STALE };
  const appliesTo = target.appliesTo && typeof target.appliesTo === "object" && !Array.isArray(target.appliesTo) ? target.appliesTo : null;
  const res = await mutate("/rules", {
    body: {
      rule_id: ruleId, name, repo_path: scope(target.repoPath),
      description: text(target.description, 10000), type: text(target.type, 100) || "architecture",
      severity: text(target.severity, 50) || "medium", status: text(target.status, 50) || "active", applies_to: appliesTo,
      expected_version: target.version, ...fields,
    },
    success: success(done),
    revalidate: MEMORY,
  });
  return { ok: res.ok, message: res.message };
}

/** Rewrites the rule's text or severity; the previous version stays in its history. */
export async function saveRule(target: RuleWrite, edit: { description: string; severity: string }): Promise<ActionResult> {
  const description = text(edit?.description, 10000);
  if (!description) return { ok: false, message: "A rule needs a description." };
  const severity = text(edit?.severity, 50).toLowerCase();
  if (!SEVERITIES.has(severity)) return { ok: false, message: "Choose a severity." };
  return writeRule(target, { description, severity }, "Saved. Agents get the new wording from their next context pack.");
}

/** Stops checking the rule; the record stays as history. */
export async function retireRule(target: RuleWrite): Promise<ActionResult> {
  return writeRule(target, { status: "deprecated" }, "Retired. Agents no longer receive it.");
}
