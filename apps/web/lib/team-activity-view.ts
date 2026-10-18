// Data for the Me and Team activity pages. Read with the signed-in member's own
// key (lib/api.ts), fresh on every render. Brain decides what each caller may
// see: /team/me describes the presented key, and /team/activity only returns
// events the caller may read, so these screens never filter anything out.
import "server-only";
import { brainRead } from "./api";

export interface MeRepository { id: number; name: string; path: string }
export interface MeProject {
  id: string;
  name: string;
  role: string;
  disabled: boolean;
  effectivePermissions: string[];
  repositories: MeRepository[];
}
export interface MeView {
  kind: "ok";
  principal: { id: number; name: string; kind: string };
  teamState: "solo" | "team";
  teamAdmin: boolean;
  allRepositories: boolean;
  accessReason: string;
  projects: MeProject[];
  scopes: string[];
  effectiveScopes: string[];
  canReadActivity: boolean;
}
export type MeResult = MeView | { kind: "unavailable" };

/** Families and kinds Brain accepts in the activity ``kind`` filter (brain/team/activity.py KINDS). */
export const ACTIVITY_KINDS: Record<string, string[]> = {
  decision: ["decision.created", "decision.updated"],
  rule: ["rule.created", "rule.updated"],
  learning: ["learning.created", "learning.updated"],
  episode: ["episode.created", "episode.updated"],
  skill: ["skill.created", "skill.updated"],
  membership: ["membership.added", "membership.revoked", "membership.admin_granted", "membership.admin_revoked"],
  project_role: ["project_role.granted", "project_role.changed", "project_role.revoked"],
  execution: ["execution.granted", "execution.revoked"],
  invitation: ["invitation.created", "invitation.accepted", "invitation.revoked"],
  sso: ["sso.bound", "sso.unbound"],
  task: ["task.created"],
  run: ["run.requested"],
  job: ["job.queued"],
  coordination: [
    "coordination.overlap", "coordination.integration_queued", "coordination.integrated", "coordination.needs_rebase",
    "coordination.stale_instructions", "coordination.integration_rejected",
  ],
};
const KNOWN_KINDS = new Set([...Object.keys(ACTIVITY_KINDS), ...Object.values(ACTIVITY_KINDS).flat()]);
export const ACTIVITY_PAGE_SIZE = 50;
const MAX_OFFSET = 5000;

export interface ActivityFilters {
  project?: string;
  repository?: number;
  actor?: number;
  kind?: string;
  /** YYYY-MM-DD; sent to Brain as the start of that day in UTC. */
  since?: string;
  offset: number;
}

export interface ActivityRow {
  id: string;
  kind: string;
  occurredAt: string | null;
  summary: string;
  actor: string;
  actorId: number | null;
  projectId: string | null;
  repositoryId: number | null;
}
export type ActivityResult =
  | { kind: "ok"; rows: ActivityRow[]; hasMore: boolean; nextOffset: number | null; offset: number }
  /** 403: the key lacks team:read, or the project or repository filter is outside the caller's access. */
  | { kind: "denied"; reason: string }
  | { kind: "invalid"; reason: string }
  | { kind: "unavailable" };

type Params = Record<string, string | string[] | undefined>;
const one = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v)?.trim() || undefined;
const positive = (v: string | undefined) => (v && /^\d{1,12}$/.test(v) && Number(v) > 0 ? Number(v) : undefined);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Page filters from the URL; anything malformed is dropped instead of being sent to Brain. */
export function activityFilters(params: Params): ActivityFilters {
  const project = one(params.project);
  const kind = one(params.kind);
  const since = one(params.since);
  const offset = positive(one(params.offset)) ?? 0;
  return {
    project: project && UUID.test(project) ? project.toLowerCase() : undefined,
    repository: positive(one(params.repository)),
    actor: positive(one(params.actor)),
    kind: kind && KNOWN_KINDS.has(kind) ? kind : undefined,
    since: since && /^\d{4}-\d{2}-\d{2}$/.test(since) && !Number.isNaN(Date.parse(`${since}T00:00:00Z`)) ? since : undefined,
    offset: Math.min(offset, MAX_OFFSET),
  };
}

/** The Brain API path for one page of activity. */
export function activityPath(filters: ActivityFilters, limit = ACTIVITY_PAGE_SIZE): string {
  const q = new URLSearchParams({ limit: String(limit) });
  if (filters.offset) q.set("offset", String(filters.offset));
  if (filters.project) q.set("project_id", filters.project);
  if (filters.repository) q.set("repository_id", String(filters.repository));
  if (filters.actor) q.set("actor_id", String(filters.actor));
  if (filters.kind) q.set("kind", filters.kind);
  if (filters.since) q.set("since", `${filters.since}T00:00:00Z`);
  return `/team/activity?${q}`;
}

/** The dashboard URL for the same filters at another offset (pager links). */
export function activityHref(filters: ActivityFilters, offset: number): string {
  const q = new URLSearchParams();
  if (filters.project) q.set("project", filters.project);
  if (filters.repository) q.set("repository", String(filters.repository));
  if (filters.actor) q.set("actor", String(filters.actor));
  if (filters.kind) q.set("kind", filters.kind);
  if (filters.since) q.set("since", filters.since);
  if (offset > 0) q.set("offset", String(offset));
  return `/team/activity${q.size ? `?${q}` : ""}`;
}

const str = (v: unknown) => (typeof v === "string" ? v : "");
const strings = (v: unknown) => (Array.isArray(v) ? v.filter((s): s is string => typeof s === "string") : []);
const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : null);

export function toActivityRow(raw: unknown): ActivityRow | null {
  if (!raw || typeof raw !== "object") return null;
  const e = raw as Record<string, unknown>;
  if (typeof e.id !== "string" || typeof e.kind !== "string") return null;
  const actor = e.actor && typeof e.actor === "object" ? (e.actor as Record<string, unknown>) : null;
  const actorId = num(actor?.principal_id);
  const label = actorId !== null ? str(actor?.name) || `#${actorId}` : str(e.actor_label) || "System";
  return {
    id: e.id,
    kind: e.kind,
    occurredAt: str(e.occurred_at) || null,
    summary: str(e.summary) || e.kind,
    actor: label,
    actorId,
    projectId: str(e.project_id) || null,
    repositoryId: num(e.repository_id),
  };
}

export function toMeView(raw: unknown): MeResult {
  if (!raw || typeof raw !== "object") return { kind: "unavailable" };
  const d = raw as Record<string, unknown>;
  const p = d.principal && typeof d.principal === "object" ? (d.principal as Record<string, unknown>) : null;
  if (!p || typeof p.id !== "number") return { kind: "unavailable" };
  const access = d.access && typeof d.access === "object" ? (d.access as Record<string, unknown>) : {};
  const projects: MeProject[] = (Array.isArray(d.projects) ? d.projects : [])
    .filter((x): x is Record<string, unknown> => Boolean(x) && typeof x === "object" && typeof (x as { id?: unknown }).id === "string")
    .map((x) => ({
      id: x.id as string,
      name: str(x.name) || (x.id as string),
      role: str(x.role),
      disabled: x.disabled === true,
      effectivePermissions: strings(x.effective_permissions),
      repositories: (Array.isArray(x.repositories) ? x.repositories : [])
        .filter((r): r is Record<string, unknown> => Boolean(r) && typeof r === "object" && typeof (r as { id?: unknown }).id === "number")
        .map((r) => ({ id: r.id as number, name: str(r.name), path: str(r.path) })),
    }));
  return {
    kind: "ok",
    principal: { id: p.id, name: str(p.name) || `#${p.id}`, kind: str(p.kind) },
    teamState: d.team_state === "team" ? "team" : "solo",
    teamAdmin: d.is_team_admin === true,
    allRepositories: access.repositories === "all",
    accessReason: str(access.reason),
    projects,
    scopes: strings(d.scopes),
    effectiveScopes: strings(d.effective_scopes),
    canReadActivity: d.can_read_activity === true,
  };
}

export async function getMeView(): Promise<MeResult> {
  const res = await brainRead<unknown>("/team/me", { fresh: true });
  return res.ok ? toMeView(res.data) : { kind: "unavailable" };
}

export async function getActivity(filters: ActivityFilters): Promise<ActivityResult> {
  const res = await brainRead<unknown>(activityPath(filters), { fresh: true, timeoutMs: 8000 });
  if (!res.ok) {
    if (res.status === 401 || res.status === 403) {
      return { kind: "denied", reason: filters.project || filters.repository
        ? "You can't read that project or repository. Clear the filter to see the activity you can read."
        : "This key can't read team activity: it needs the team:read permission." };
    }
    // A 404 without a project filter means a server that predates the feed.
    if (res.status === 400 || res.status === 422 || (res.status === 404 && filters.project)) {
      return { kind: "invalid", reason: "Brain didn't accept these filters. Check the project and repository." };
    }
    return { kind: "unavailable" };
  }
  const d = res.data && typeof res.data === "object" ? (res.data as Record<string, unknown>) : null;
  if (!d || !Array.isArray(d.events)) return { kind: "unavailable" };
  return {
    kind: "ok",
    rows: d.events.map(toActivityRow).filter((r): r is ActivityRow => r !== null),
    hasMore: d.has_more === true,
    nextOffset: num(d.next_offset),
    offset: filters.offset,
  };
}

/** Projects to offer in the filter: every project for a team admin (or the solo owner), otherwise the caller's own. */
export async function activityProjects(me: MeResult): Promise<{ id: string; name: string }[]> {
  if (me.kind !== "ok") return [];
  if (me.allRepositories) {
    const res = await brainRead<unknown>("/team/projects/", { fresh: true });
    if (res.ok && Array.isArray(res.data)) {
      return res.data
        .filter((p): p is Record<string, unknown> => Boolean(p) && typeof p === "object" && typeof (p as { id?: unknown }).id === "string")
        .map((p) => ({ id: p.id as string, name: str(p.name) || (p.id as string) }));
    }
  }
  return me.projects.map((p) => ({ id: p.id, name: p.name }));
}
