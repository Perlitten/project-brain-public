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
    body: { title, repo_path: scope(target.repoPath), expected_version: target.version, status: text(target.status, 50) || "active", ...fields },
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

// ---------------------------------------------------------------------------
// New records from the dashboard. Brain keys a decision by title and scope and
// a rule by its id, so a create is a write with expected_version 0: it fails
// instead of replacing something an agent already recorded under that name.

const slug = (name: string) => name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 80);

/** Records a new decision; agents receive it from their next matching context pack. */
export async function createDecision(input: { title: string; description: string; reason?: string; repoPath?: string | null }): Promise<ActionResult> {
  const title = text(input?.title, 500);
  const description = text(input?.description, 10000);
  if (!title) return { ok: false, message: "A decision needs a title." };
  if (!description) return { ok: false, message: "Say what was decided." };
  const res = await mutate("/decisions", {
    body: { title, description, reason: text(input?.reason, 5000), repo_path: scope(input?.repoPath), status: "active", expected_version: 0 },
    success: success("Recorded. Agents get it from their next context pack."),
    revalidate: MEMORY,
  });
  return { ok: res.ok, message: res.ok ? res.message : res.message.includes("409") || /version|exists|conflict/i.test(res.message) ? "A decision with this title already exists in that scope — edit it instead." : res.message };
}

/** Records a new rule; reviews start checking it and agents receive it with their packs. */
export async function createRule(input: { name: string; description: string; severity: string; repoPath?: string | null }): Promise<ActionResult> {
  const name = text(input?.name, 500);
  const description = text(input?.description, 10000);
  const severity = text(input?.severity, 50).toLowerCase();
  if (!name) return { ok: false, message: "A rule needs a name." };
  if (!description) return { ok: false, message: "Say what the rule is." };
  if (!SEVERITIES.has(severity)) return { ok: false, message: "Choose what happens when it is broken." };
  const ruleId = slug(name);
  if (!ruleId) return { ok: false, message: "A rule needs a name with letters or digits in it." };
  const res = await mutate("/rules", {
    body: { rule_id: ruleId, name, description, type: "architecture", severity, status: "active", applies_to: null, repo_path: scope(input?.repoPath), expected_version: 0 },
    success: success("Recorded. Reviews check it from now on; agents get it from their next context pack."),
    revalidate: MEMORY,
  });
  return { ok: res.ok, message: res.ok ? res.message : /version|exists|conflict|409/i.test(res.message) ? `A rule named “${ruleId}” already exists in that scope — edit it instead.` : res.message };
}
