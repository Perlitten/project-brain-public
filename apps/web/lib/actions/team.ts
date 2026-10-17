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

// ── Invitations (POST /team/invitations, /{id}/revoke) ──
// The invitee redeems the token on /invite (proxy.ts); Brain creates the member
// and their key then, so nothing secret but the token passes through here.

/** brain/team/invitations.py NAME_PATTERN and EMAIL_PATTERN. */
const INVITEE_NAME_RE = /^[A-Za-z][A-Za-z0-9._@+-]{0,63}$/;
const EMAIL_RE = /^[^@\s]{1,64}@[^@\s]{1,189}$/;
const MAX_INVITE_PROJECTS = 16;
const MAX_INVITE_HOURS = 720;

export interface NewInvitation {
  token: string;
  id: string;
  name: string;
  expiresAt: string | null;
}

export async function createInvitation(input: {
  name?: string;
  email?: string;
  projects: string[];
  role: ProjectRole;
  admin: boolean;
  expiresHours: number;
}): Promise<ActionResult<NewInvitation>> {
  const name = (input.name ?? "").trim();
  if (name && !INVITEE_NAME_RE.test(name)) {
    return { ok: false, message: "Start the name with a letter and use only letters, digits and . _ @ + - (no spaces), e.g. “alice”." };
  }
  const email = (input.email ?? "").trim();
  if (email && !EMAIL_RE.test(email)) return { ok: false, message: "That email address doesn’t look right." };
  const projects = [...new Set(Array.isArray(input.projects) ? input.projects : [])];
  if (projects.length > MAX_INVITE_PROJECTS) return { ok: false, message: `An invitation can name at most ${MAX_INVITE_PROJECTS} projects.` };
  if (!projects.every((p) => typeof p === "string" && PROJECT_RE.test(p))) return { ok: false, message: "That project doesn’t look right." };
  if (!PROJECT_ROLES.includes(input.role)) return { ok: false, message: "Pick reader, contributor or reviewer." };
  const hours = Math.round(Number(input.expiresHours));
  if (!Number.isFinite(hours) || hours < 1 || hours > MAX_INVITE_HOURS) return { ok: false, message: "Pick how long the invitation stays valid." };
  const res = await mutate<{ token?: unknown; invitation?: { id?: unknown; principal_name?: unknown; expires_at?: unknown } }>("/team/invitations", {
    body: { projects, role: input.role, is_admin: Boolean(input.admin), email: email || null, name: name || null, expires_hours: hours },
    success: "Invitation created. Copy the link now — Brain won’t show it again.",
    revalidate: TEAM,
  });
  if (!res.ok) return { ok: false, message: res.message };
  const { token, invitation } = res.data ?? {};
  if (typeof token !== "string" || !token || typeof invitation?.id !== "string") return { ok: false, message: "Brain didn’t return the invitation token." };
  return {
    ok: true,
    message: res.message,
    data: {
      token,
      id: invitation.id,
      name: typeof invitation.principal_name === "string" ? invitation.principal_name : name,
      expiresAt: typeof invitation.expires_at === "string" ? invitation.expires_at : null,
    },
  };
}

/**
 * Revokes an invitation. One that was already accepted can only be revoked
 * together with the member it created (`revokeMember`); Brain refuses otherwise.
 */
export async function revokeInvitation(invitationId: string, revokeMember = false): Promise<ActionResult> {
  if (!PROJECT_RE.test(invitationId)) return { ok: false, message: "That invitation doesn’t look right." };
  const res = await mutate(`/team/invitations/${invitationId}/revoke`, {
    body: { revoke_member: Boolean(revokeMember) },
    success: revokeMember ? "Revoked, with the member it created: their key stops working at once." : "Invitation revoked. Its link no longer works.",
    revalidate: TEAM,
  });
  return { ok: res.ok, message: res.message };
}
