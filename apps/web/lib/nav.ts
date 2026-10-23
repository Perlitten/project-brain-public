import type { Capability } from "./capability-contract";
import type { IconName } from "@/components/Icon";

export interface NavItem {
  key: string; href: string; label: string; icon: IconName; hint: string;
  repoScoped?: boolean;
  section?: string;
  capability?: Capability;
}

// Grouped by what a supervisor came to do: watch and step in, dig into the
// evidence, or change how Brain runs. Diagnostics stay searchable in the palette.
export const navGroups: { label: string; hint: string; items: NavItem[] }[] = [{
  label: "Supervise", hint: "What agents know and do, and where a human should step in", items: [
    { key: "command", href: "/", label: "Dashboard", icon: "zap", hint: "Usage, memory health and what needs attention", repoScoped: true },
    { key: "projects", href: "/projects", label: "Projects", icon: "database", hint: "Connected repositories, indexing and code maps", repoScoped: true },
    { key: "memory", href: "/memory", label: "Memory", icon: "layers", hint: "Decisions, rules and saved context packs", repoScoped: true },
    { key: "team", href: "/team", label: "Team", icon: "key", hint: "Members, roles, team activity and board", capability: "team" },
  ],
}, {
  label: "Diagnose", hint: "Evidence behind what Brain served and how well it searched", items: [
    { key: "logs", href: "/activity", label: "Activity", icon: "activity", hint: "Background jobs, requests and agent work", repoScoped: true },
    { key: "quality", href: "/quality", label: "Quality", icon: "vectors", hint: "Whether Brain finds the right code, and the saved reports behind it" },
  ],
}, {
  label: "Configure", hint: "Models, schedules, alerts, access and first-time setup", items: [
    { key: "settings", href: "/settings", label: "Settings", icon: "gear", hint: "Notifications, models, integrations and access" },
    { key: "setup", href: "/setup", label: "Setup", icon: "flag", hint: "Six steps from an empty server to an agent using Brain" },
  ],
}];
const nested: NavItem[] = [
  { key: "graph", href: "/graph", label: "Code map", icon: "graph", hint: "Which parts of the code depend on which", repoScoped: true, section: "projects" },
  { key: "indexing", href: "/indexing", label: "Index runs", icon: "database", hint: "Each time Brain re-read your code", repoScoped: true, section: "projects" },
  { key: "insights", href: "/insights", label: "Findings", icon: "trend", hint: "Problems Brain noticed in the code structure", repoScoped: true, section: "projects" },
  { key: "packs", href: "/packs", label: "Context packs", icon: "layers", hint: "What Brain handed agents for each task", repoScoped: true, section: "memory" },
  { key: "review", href: "/review", label: "Review", icon: "layers", hint: "Memory proposals waiting for an independent reviewer (team state)", section: "memory", capability: "knowledge_review" },
  // /reports is now a tab of Quality and redirects there; the entry stays so
  // report detail pages (/reports/<id>) still resolve to the Quality section
  // and the word "Reports" keeps working in the command palette.
  { key: "reports", href: "/reports", label: "Reports", icon: "file", hint: "Every saved evaluation and check", section: "quality" },
  { key: "reranker", href: "/reranker", label: "Reranker", icon: "vectors", hint: "Optional second pass that re-orders search results", repoScoped: true, section: "settings" },
  { key: "runs", href: "/runs", label: "Agent tasks", icon: "automation", hint: "Tasks agents ran with Brain's help", repoScoped: true, section: "logs" },
  { key: "mcp", href: "/mcp", label: "Agent tools", icon: "chip", hint: "The tools agents can call and how they perform", section: "settings" },
  { key: "admin", href: "/admin", label: "Access", icon: "key", hint: "API keys and what each one may do", section: "settings" },
  // Before "team" so /team/me, /team/activity and /team/board resolve to their own entries; members can open all three.
  { key: "team-me", href: "/team/me", label: "Me", icon: "key", hint: "Your team role, project roles and key permissions", section: "team", capability: "team" },
  { key: "team-activity", href: "/team/activity", label: "Team activity", icon: "activity", hint: "Who changed memory, membership, tasks and jobs", section: "team", capability: "team_activity" },
  { key: "team-board", href: "/team/board", label: "Board", icon: "layers", hint: "Tasks by state, leases, overlap warnings and the integration queue", section: "team", capability: "team_board" },
  // Reached from the rail footer and the palette, not a daily destination.
  { key: "help", href: "/help", label: "Help", icon: "help", hint: "About this server, common problems, glossary and guides" },
];
export const primaryNavItems = navGroups.flatMap((g) => g.items);
export const allNavItems = [...primaryNavItems, ...nested];
/** Route boundaries keep unrelated prefixes out of a section. */
export function locate(path: string): { item: NavItem; group: string } | null {
  const item = [...allNavItems].sort((a, b) => b.href.length - a.href.length).find((i) => i.href === "/" ? path === "/" : path === i.href || path.startsWith(`${i.href}/`));
  if (!item) return null;
  const parent = primaryNavItems.find((i) => i.key === item.section);
  return { item, group: parent?.label ?? navGroups.find((g) => g.items.includes(item))?.label ?? "Workspace" };
}
export function activeSection(path: string): string | undefined {
  const item = locate(path)?.item;
  return item?.section ?? item?.key;
}

/** Keep project context through global evidence/settings without claiming a filter. */
export function withProject(href: string, slug?: string): string {
  const hashAt = href.indexOf("#");
  const fragment = hashAt >= 0 ? href.slice(hashAt) : "";
  const withoutFragment = hashAt >= 0 ? href.slice(0, hashAt) : href;
  const [pathname, query = ""] = withoutFragment.split("?");
  const params = new URLSearchParams(query);
  if (slug) params.set("repo", slug); else params.delete("repo");
  return `${pathname}${params.size ? `?${params}` : ""}${fragment}`;
}

/** Pages that cover the whole server: in team state Brain serves them to team admins only. */
const ADMIN_ONLY_KEYS: ReadonlySet<string> = new Set(["quality", "reports", "logs", "runs", "settings", "reranker", "admin", "setup"]);
export function adminOnly(href: string): boolean {
  const key = locate(href.split(/[?#]/)[0])?.item.key;
  return key !== undefined && ADMIN_ONLY_KEYS.has(key);
}
