// Demo data for the Team pages (Me, Team activity, notifications) when no Brain
// API is configured. The same people and project as lib/board-demo.ts, so the
// Board, Agent tasks and this activity tell one story.
import { DEMO_BOARD } from "./board-demo";
import type { ActivityFilters, ActivityRow, MeView } from "./team-activity-view";
import type { NotificationRow, TelegramLinkView } from "./team-notifications-view";
import type { TeamView } from "./team-view";
import type { InvitationsView } from "./invitation-view";
import type { WebMember } from "./web-auth";

const PROJECT = DEMO_BOARD.project.id;
const REPO = DEMO_BOARD.project.repositories[0].id;

/** Who the dashboard is signed in as: the reviewer the demo Board is seen through. */
export const DEMO_ME: MeView = {
  kind: "ok",
  principal: { id: DEMO_BOARD.selfId, name: "alice", kind: "human" },
  teamState: "team",
  teamAdmin: true,
  allRepositories: true,
  accessReason: "Team admin: every project and repository, and the team itself.",
  projects: [
    {
      id: PROJECT,
      name: DEMO_BOARD.project.name,
      role: DEMO_BOARD.project.role,
      disabled: false,
      effectivePermissions: [...DEMO_BOARD.project.effectivePermissions],
      repositories: DEMO_BOARD.project.repositories.map((r) => ({ ...r })),
    },
  ],
  scopes: ["*"],
  effectiveScopes: ["*"],
  canReadActivity: true,
};

const row = (n: number, kind: string, occurredAt: string, actor: [number, string], summary: string, detail: string | null = null, where: "project" | "repo" | "global" = "project"): ActivityRow => ({
  id: `demo-${n}`, kind, occurredAt, summary, detail, actor: actor[1], actorId: actor[0],
  projectId: where === "project" ? PROJECT : null, repositoryId: where === "repo" ? REPO : null,
});
const ALICE: [number, string] = [4, "alice"];
const BOB: [number, string] = [7, "bob"];
const CLAUDE: [number, string] = [12, "claude-laptop"];
const CODEX: [number, string] = [13, "codex-ci"];

/** Newest first, like Brain returns them. */
export const DEMO_ACTIVITY: ActivityRow[] = [
  row(1, "coordination.integration_queued", "2026-10-18T08:52:00Z", CLAUDE, "Queued “Wire retries into the checkout API” for integration", "Head abcd1234 on retries/03; base is current."),
  row(2, "coordination.overlap", "2026-10-18T08:47:00Z", CODEX, "“Upgrade the HTTP client” changes the same paths as “Wire retries into the checkout API”", "apps/api/checkout/client.py"),
  row(3, "run.requested", "2026-10-18T08:41:00Z", BOB, "Run requested for “Document the retry policy”"),
  row(4, "memory.proposed", "2026-10-18T08:30:00Z", CLAUDE, "Proposed rule “Retries use exponential backoff with jitter”", "Waiting for a reviewer."),
  row(5, "decision.created", "2026-10-17T16:12:00Z", ALICE, "Recorded decision D-41 “Idempotency keys for refunds live in the API layer”"),
  row(6, "task.created", "2026-10-17T15:05:00Z", BOB, "Created task “Payment retries” with five subtasks"),
  row(7, "job.queued", "2026-10-17T14:50:00Z", ALICE, "Queued a full re-index of checkout-service", null, "repo"),
  row(8, "project_role.granted", "2026-10-16T10:02:00Z", ALICE, "Gave bob the contributor role on Checkout"),
  row(9, "execution.granted", "2026-10-16T09:58:00Z", ALICE, "Allowed codex-ci to run tasks in a container", "Egress limited to the package registry."),
  row(10, "membership.added", "2026-10-16T09:40:00Z", ALICE, "Added bob to the team", null, "global"),
  row(11, "invitation.accepted", "2026-10-16T09:39:00Z", BOB, "Accepted the invitation and signed in with a new key", null, "global"),
  row(12, "rule.created", "2026-10-15T12:20:00Z", ALICE, "Recorded rule R-12 “Every checkout handler is covered by a contract test”"),
];

/** The filters the live endpoint honours, applied to the demo rows. */
export function demoActivity(filters: ActivityFilters, pageSize: number): { rows: ActivityRow[]; hasMore: boolean; nextOffset: number | null; offset: number } {
  const family = filters.kind && !filters.kind.includes(".") ? `${filters.kind}.` : null;
  const since = filters.since ? Date.parse(`${filters.since}T00:00:00Z`) : null;
  const all = DEMO_ACTIVITY.filter((r) =>
    (!filters.project || r.projectId === filters.project)
    && (!filters.repository || r.repositoryId === filters.repository)
    && (!filters.actor || r.actorId === filters.actor)
    && (!filters.kind || (family ? r.kind.startsWith(family) : r.kind === filters.kind))
    && (since === null || (r.occurredAt !== null && Date.parse(r.occurredAt) >= since)));
  const rows = all.slice(filters.offset, filters.offset + pageSize);
  const hasMore = filters.offset + pageSize < all.length;
  return { rows, hasMore, nextOffset: hasMore ? filters.offset + pageSize : null, offset: filters.offset };
}

export const DEMO_NOTIFICATIONS: NotificationRow[] = [
  { id: 1, kind: "memory.proposed", summary: "claude-laptop proposed rule “Retries use exponential backoff with jitter”", proposalId: 31, proposalState: "pending", createdAt: "2026-10-18T08:30:00Z", readAt: null },
  { id: 2, kind: "memory.approved", summary: "bob approved decision “Idempotency keys for refunds live in the API layer”", proposalId: 29, proposalState: "approved", createdAt: "2026-10-17T16:15:00Z", readAt: "2026-10-17T16:40:00Z" },
];

export const DEMO_TELEGRAM: TelegramLinkView = {
  kind: "ok", teamState: "team", available: true, linked: true, chat: "…6789", linkedAt: "2026-10-16T09:45:00Z",
  pending: false, pendingChat: null, codeExpiresAt: null,
};

/** The Team page's idea of the signed-in person: the same alice as DEMO_ME, offered team administration. */
export const DEMO_MEMBER: WebMember = { id: DEMO_BOARD.selfId, name: "alice", admin: true };

/** Members and projects as the Team page lists them. Agents are not members: they live under Access. */
export const DEMO_TEAM: TeamView = {
  kind: "ok",
  projects: [{ id: PROJECT, name: DEMO_BOARD.project.name, disabled: false }],
  members: [
    { id: 4, name: "alice", admin: true, revoked: false, verifiedAt: "2026-10-15", roles: { [PROJECT]: "reviewer" } },
    { id: 7, name: "bob", admin: false, revoked: false, verifiedAt: "2026-10-16", roles: { [PROJECT]: "contributor" } },
    { id: 9, name: "dana", admin: false, revoked: true, verifiedAt: "2026-09-30", roles: {} },
  ],
};

export const DEMO_INVITATIONS: InvitationsView = {
  kind: "ok",
  invitations: [
    { id: "22222222-2222-4222-8222-222222222222", state: "invited", name: "carol", email: "carol@example.com", projectIds: [PROJECT], role: "reader", admin: false, createdAt: "2026-10-18T07:10:00Z", expiresAt: "2026-10-25T07:10:00Z", acceptedAt: null, revokedAt: null, memberId: null },
    { id: "33333333-3333-4333-8333-333333333333", state: "active", name: "bob", email: null, projectIds: [PROJECT], role: "contributor", admin: false, createdAt: "2026-10-16T09:20:00Z", expiresAt: "2026-10-23T09:20:00Z", acceptedAt: "2026-10-16T09:39:00Z", revokedAt: null, memberId: 7 },
  ],
};
