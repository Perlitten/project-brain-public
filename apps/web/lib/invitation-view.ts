// Invitations for the Team page, read with the signed-in admin's own key
// (lib/api.ts), fresh on every render. "unavailable" is never shown as an
// empty list: an admin must not conclude nobody is invited because Brain
// didn't answer.
import "server-only";
import { brainRead } from "./api";
import { INVITATION_STATES, type InvitationState } from "./invitations";
import { PROJECT_ROLES, type ProjectRole } from "./team-scopes";

export interface InvitationRow {
  id: string;
  state: InvitationState;
  name: string;
  email: string | null;
  projectIds: string[];
  role: ProjectRole;
  admin: boolean;
  createdAt: string | null;
  expiresAt: string | null;
  acceptedAt: string | null;
  revokedAt: string | null;
  /** The member the invitation created, once accepted. */
  memberId: number | null;
}

export type InvitationsView =
  | { kind: "ok"; invitations: InvitationRow[] }
  /** Brain refused: not a team admin (or the key lacks team:admin). */
  | { kind: "denied" }
  | { kind: "unavailable" };

const text = (v: unknown) => (typeof v === "string" && v ? v : null);

/** Invited and accepted ones first, then the rest; newest first within each. */
const ORDER: Record<InvitationState, number> = { invited: 0, verified: 1, active: 2, expired: 3, revoked: 4 };

export function invitationRows(data: unknown): InvitationRow[] | null {
  if (!Array.isArray(data)) return null;
  const rows: InvitationRow[] = [];
  for (const raw of data) {
    const r = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : null;
    if (!r || typeof r.id !== "string" || !INVITATION_STATES.includes(r.state as InvitationState)) continue;
    rows.push({
      id: r.id,
      state: r.state as InvitationState,
      name: text(r.principal_name) ?? "(unnamed)",
      email: text(r.email),
      projectIds: Array.isArray(r.project_ids) ? r.project_ids.filter((p): p is string => typeof p === "string") : [],
      role: PROJECT_ROLES.includes(r.role as ProjectRole) ? (r.role as ProjectRole) : "contributor",
      admin: r.is_admin === true,
      createdAt: text(r.created_at),
      expiresAt: text(r.expires_at),
      acceptedAt: text(r.accepted_at),
      revokedAt: text(r.revoked_at),
      memberId: typeof r.accepted_principal_id === "number" ? r.accepted_principal_id : null,
    });
  }
  return rows.sort((a, b) => ORDER[a.state] - ORDER[b.state] || (b.createdAt ?? "").localeCompare(a.createdAt ?? ""));
}

export async function getInvitationsView(): Promise<InvitationsView> {
  const res = await brainRead<unknown>("/team/invitations", { fresh: true });
  if (!res.ok) return { kind: res.status === 401 || res.status === 403 ? "denied" : "unavailable" };
  const rows = invitationRows(res.data);
  return rows ? { kind: "ok", invitations: rows } : { kind: "unavailable" };
}
