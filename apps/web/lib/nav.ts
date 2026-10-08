import type { IconName } from "@/components/Icon";

export interface NavItem {
  key: string; href: string; label: string; icon: IconName; hint: string;
  repoScoped?: boolean;
  section?: string;
}

// Daily owner tasks. Diagnostics stay searchable in the command palette.
export const navGroups: { label: string; hint: string; items: NavItem[] }[] = [{
  label: "Workspace", hint: "Memory, usage and quality across your projects", items: [
    { key: "command", href: "/", label: "Dashboard", icon: "zap", hint: "Usage, memory health and what needs attention", repoScoped: true },
    { key: "projects", href: "/projects", label: "Projects", icon: "database", hint: "Connected repositories, indexing and code maps", repoScoped: true },
    { key: "memory", href: "/memory", label: "Memory", icon: "layers", hint: "Decisions, rules and saved context packs", repoScoped: true },
    { key: "quality", href: "/quality", label: "Quality", icon: "vectors", hint: "Measured retrieval quality and saved evaluations" },
    { key: "logs", href: "/logs", label: "Activity", icon: "activity", hint: "Background jobs, requests and agent work", repoScoped: true },
    { key: "settings", href: "/settings", label: "Settings", icon: "gear", hint: "Notifications, models, integrations and access" },
  ],
}];
const nested: NavItem[] = [
  { key: "setup", href: "/setup", label: "Connect a project", icon: "flag", hint: "Connect Brain to code and AI agents", section: "projects" },
  { key: "graph", href: "/graph", label: "Code map", icon: "graph", hint: "Which parts of the code depend on which", repoScoped: true, section: "projects" },
  { key: "indexing", href: "/indexing", label: "Indexing", icon: "database", hint: "Each time Brain re-read your code", repoScoped: true, section: "projects" },
  { key: "insights", href: "/insights", label: "Findings", icon: "trend", hint: "Recorded observations about code structure", section: "projects" },
  { key: "packs", href: "/packs", label: "Context packs", icon: "layers", hint: "Saved briefings prepared for AI agents", repoScoped: true, section: "memory" },
  { key: "reports", href: "/reports", label: "Reports", icon: "file", hint: "Saved evaluations and checks", section: "quality" },
  { key: "reranker", href: "/reranker", label: "Reranker diagnostics", icon: "vectors", hint: "The optional second pass of retrieval", repoScoped: true, section: "quality" },
  { key: "runs", href: "/runs", label: "Agent runs", icon: "automation", hint: "Recorded agent task history", repoScoped: true, section: "logs" },
  { key: "mcp", href: "/mcp", label: "Agent tools", icon: "chip", hint: "Available agent integrations and tools", section: "settings" },
  { key: "admin", href: "/admin", label: "Access", icon: "key", hint: "Identities, credentials and permissions", section: "settings" },
];
export const primaryNavItems = navGroups.flatMap((g) => g.items);
export const allNavItems = [...primaryNavItems, ...nested];
/** Route boundaries keep unrelated prefixes out of a section. */
export function locate(path: string): { item: NavItem; group: string } | null {
  const item = allNavItems.find((i) => i.href === "/" ? path === "/" : path === i.href || path.startsWith(`${i.href}/`));
  if (!item) return null;
  const parent = primaryNavItems.find((i) => i.key === item.section);
  return { item, group: parent?.label ?? "Workspace" };
}
export function activeSection(path: string): string | undefined {
  const item = locate(path)?.item;
  return item?.section ?? item?.key;
}

/** Keep project context through global evidence/settings without claiming a filter. */
export function withProject(href: string, slug?: string): string {
  const [pathname, query = ""] = href.split("?");
  const params = new URLSearchParams(query);
  if (slug) params.set("repo", slug); else params.delete("repo");
  return `${pathname}${params.size ? `?${params}` : ""}`;
}
