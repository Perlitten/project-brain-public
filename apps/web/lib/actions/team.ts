"use server";
// Team page: add members (with their own dashboard key, shown once), change a
// member's role in a project, take them off a project, revoke them. Every call
// goes out with the signed-in admin's own key (lib/api.ts), so Brain decides —
// the page only hides what a non-admin can't do anyway.
import { mutate } from "./gate";
import type { ActionResult } from "./types";
import type { NewKey } from "./admin";
import { PROJECT_ROLES, memberKeyScopes, type ProjectRole } from "../team-scopes";

const TEAM = ["/team"];
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9 ._@-]{0,119}$/;
const PROJECT_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const goodId = (id: number) => Number.isInteger(id) && id > 0;

/**
 * Adds a person to the team. With `mintKey` (the usual case) Brain first creates
 * the person and their dashboard key; without it an existing person is linked
 * by name and keeps the keys they already have.
 */
export async function addMember(input: { name: string; admin: boolean; mintKey: boolean; ttlDays?: number }): Promise<ActionResult<NewKey | null>> {
  const name = input.name.trim();
  if (!NAME_RE.test(name)) return { ok: false, message: "Name them with letters, digits, spaces, dots, dashes or @ — e.g. “alice”." };
  const admin = Boolean(input.admin);
  let key: NewKey | null = null;
  let principalId: number | undefined;
  if (input.mintKey) {
    const scopes = memberKeyScopes(admin);
    const ttl = input.ttlDays && Number.isFinite(input.ttlDays) && input.ttlDays > 0 ? Math.min(3650, Math.round(input.ttlDays)) : undefined;
    const made = await mutate<{ api_key?: string; principal?: { id?: number; name?: string } }>("/admin/principals", {
      body: { name, kind: "human", scopes, ttl_days: ttl },
      success: "",
    });
    if (!made.ok || !made.data?.api_key || !goodId(made.data.principal?.id ?? 0)) {
      return { ok: false, message: made.ok ? "Brain didn’t return a key." : made.message };
    }
    principalId = made.data.principal!.id!;
    key = { apiKey: made.data.api_key, principalId, name: made.data.principal?.name ?? name, scopes };
  }
  const linked = await mutate("/team/projects/members", {
    body: principalId ? { principal_id: principalId, is_admin: admin } : { principal_name: name, is_admin: admin },
    success: key ? `“${name}” joined the team. Copy their key now — Brain won’t show it again.` : `“${name}” joined the team.`,
    revalidate: TEAM,
  });
  if (!linked.ok) {
    return {
      ok: false,
      message: key
        ? `Brain created “${name}” but didn’t add them to the team: ${linked.message} Add them again with “Use an existing person”.`
        : linked.message,
    };
  }
  return { ok: true, message: linked.message, data: key };
}

/** Gives a member a role in one project (adds them to it if needed). */
export async function setProjectRole(projectId: string, principalId: number, role: ProjectRole): Promise<ActionResult> {
  if (!PROJECT_RE.test(projectId) || !goodId(principalId)) return { ok: false, message: "That project or member doesn’t look right." };
  if (!PROJECT_ROLES.includes(role)) return { ok: false, message: "Pick reader, contributor or reviewer." };
  const res = await mutate(`/team/projects/${projectId}/members/${principalId}`, {
    method: "PUT",
    body: { role },
    success: `Role changed to ${role}.`,
    revalidate: TEAM,
  });
  return { ok: res.ok, message: res.message };
}

export async function removeFromProject(projectId: string, principalId: number): Promise<ActionResult> {
  if (!PROJECT_RE.test(projectId) || !goodId(principalId)) return { ok: false, message: "That project or member doesn’t look right." };
  const res = await mutate(`/team/projects/${projectId}/members/${principalId}`, {
    method: "DELETE",
    success: "Taken off the project.",
    revalidate: TEAM,
  });
  return { ok: res.ok, message: res.message };
}

/** Revokes team membership: their key stops opening the dashboard on its next request. */
export async function revokeMember(principalId: number): Promise<ActionResult> {
  if (!goodId(principalId)) return { ok: false, message: "That member doesn’t look right." };
  const res = await mutate(`/team/projects/members/${principalId}`, {
    method: "DELETE",
    success: "Revoked. Their key no longer opens the dashboard or reaches team projects.",
    revalidate: TEAM,
  });
  return { ok: res.ok, message: res.message };
}
