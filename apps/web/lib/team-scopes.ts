// Permissions a team member's dashboard key gets when an admin adds them on
// the Team page. Brain still decides every call: project roles limit what the
// team:* scopes allow on each repository, and only a member flagged as admin
// can use team:admin. Kept out of the "use server" action module so both the
// action and the tests can read it.

/** team:<permission> for every project role (brain/team/provisioning.py member_scopes). */
export const TEAM_ROLE_SCOPES = [
  "team:knowledge.approve",
  "team:knowledge.propose",
  "team:read",
  "team:run.request",
  "team:task.accept",
  "team:task.create",
];

/** What the dashboard's own screens read, plus recording decisions and rules and queuing jobs (Brain checks the role per repository). */
export const DASHBOARD_SCOPES = [
  "control:read",
  "core:write",
  "coupling:read",
  "drift:read",
  "freshness:read",
  "graph:read",
  "harness:read",
  "impact:read",
  "jobs:write",
  "ledger:read",
  "operations:read",
  "portfolio:read",
  "setup:read",
  "workspace:read",
];

/** Extra for team admins: the Team page, member keys, and the admin screens (Setup, Settings, Access, job retries). */
export const TEAM_ADMIN_SCOPES = ["operations:write", "principals:read", "principals:write", "setup:write", "team:admin"];

export function memberKeyScopes(admin: boolean): string[] {
  return [...new Set([...TEAM_ROLE_SCOPES, ...DASHBOARD_SCOPES, ...(admin ? TEAM_ADMIN_SCOPES : [])])].sort();
}

export const PROJECT_ROLES = ["reader", "contributor", "reviewer"] as const;
export type ProjectRole = (typeof PROJECT_ROLES)[number];

export const ROLE_WORD: Record<ProjectRole, string> = {
  reader: "Reader — reads code memory",
  contributor: "Contributor — also creates tasks, requests runs, proposes knowledge",
  reviewer: "Reviewer — also approves knowledge and accepts tasks",
};
