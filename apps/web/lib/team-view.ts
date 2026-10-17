// Data for the Team page. Read with the signed-in member's own key (lib/api.ts),
// fresh on every render: membership changes must show at once, and Brain
// answers team admins only — anyone else gets the read-only view.
import "server-only";
import { brainRead } from "./api";
import { PROJECT_ROLES, type ProjectRole } from "./team-scopes";

export interface TeamMemberRow {
  id: number;
  name: string;
  admin: boolean;
  revoked: boolean;
  verifiedAt: string | null;
  /** Project id → role, for live project memberships. */
  roles: Record<string, ProjectRole>;
}

export interface TeamProjectRow {
  id: string;
  name: string;
  disabled: boolean;
  /** Brain didn't return this project's member list: roles in it are unknown, not "no access". */
  unread?: boolean;
}

export type TeamView =
  | { kind: "ok"; members: TeamMemberRow[]; projects: TeamProjectRow[] }
  /** Brain refused: not a team admin (or the key lacks team:admin). */
  | { kind: "denied" }
  | { kind: "unavailable" };

interface RawMember {
  principal_id?: unknown;
  principal_name?: unknown;
  is_admin?: unknown;
  revoked_at?: unknown;
  verified_at?: unknown;
}
interface RawProject {
  id?: unknown;
  name?: unknown;
  disabled_at?: unknown;
}
interface RawProjectMember {
  principal_id?: unknown;
  role?: unknown;
  revoked_at?: unknown;
}

const day = (v: unknown) => (typeof v === "string" && v ? v.slice(0, 10) : null);

export async function getTeamView(): Promise<TeamView> {
  const [members, projects] = await Promise.all([
    brainRead<RawMember[]>("/team/projects/members", { fresh: true }),
    brainRead<RawProject[]>("/team/projects/", { fresh: true }),
  ]);
  if (!members.ok || !projects.ok) {
    const refused = [members, projects].some((r) => !r.ok && (r.status === 401 || r.status === 403));
    return { kind: refused ? "denied" : "unavailable" };
  }
  if (!Array.isArray(members.data) || !Array.isArray(projects.data)) return { kind: "unavailable" };

  const projectRows: TeamProjectRow[] = projects.data
    .filter((p): p is RawProject & { id: string } => typeof p?.id === "string")
    .map((p) => ({ id: p.id, name: typeof p.name === "string" ? p.name : p.id, disabled: Boolean(p.disabled_at) }));

  const rows: TeamMemberRow[] = members.data
    .filter((m): m is RawMember & { principal_id: number } => typeof m?.principal_id === "number")
    .map((m) => ({
      id: m.principal_id,
      name: typeof m.principal_name === "string" && m.principal_name ? m.principal_name : `#${m.principal_id}`,
      admin: m.is_admin === true,
      revoked: Boolean(m.revoked_at),
      verifiedAt: day(m.verified_at),
      roles: {},
    }));
  const byId = new Map(rows.map((r) => [r.id, r]));

  const live = projectRows.filter((p) => !p.disabled);
  const lists = await Promise.all(live.map((p) => brainRead<RawProjectMember[]>(`/team/projects/${p.id}/members`, { fresh: true })));
  lists.forEach((res, i) => {
    if (!res.ok || !Array.isArray(res.data)) {
      live[i].unread = true;
      return;
    }
    for (const pm of res.data) {
      const row = typeof pm?.principal_id === "number" ? byId.get(pm.principal_id) : undefined;
      if (row && !pm.revoked_at && PROJECT_ROLES.includes(pm.role as ProjectRole)) row.roles[live[i].id] = pm.role as ProjectRole;
    }
  });

  rows.sort((a, b) => Number(a.revoked) - Number(b.revoked) || Number(b.admin) - Number(a.admin) || a.name.localeCompare(b.name));
  return { kind: "ok", members: rows, projects: projectRows };
}
