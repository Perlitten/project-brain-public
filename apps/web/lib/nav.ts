import type { IconName } from "@/components/Icon";

export interface NavItem {
  key: string;
  href: string;
  label: string;
  icon: IconName;
  /** One plain sentence: what the screen is for. Shown in the palette. */
  hint: string;
  /** The screen follows the repository switcher (?repo=). */
  repoScoped?: boolean;
}

// Single source for the rail, the drawer and the command palette.
export const navGroups: { label: string; hint: string; items: NavItem[] }[] = [
  {
    label: "Status",
    hint: "Is Brain healthy and what needs you",
    items: [
      { key: "setup", href: "/setup", label: "Get started", icon: "flag", hint: "Connect Brain to your code and your AI agents" },
      { key: "command", href: "/", label: "Overview", icon: "zap", hint: "Is everything OK, and what needs attention", repoScoped: true },
      { key: "insights", href: "/insights", label: "Findings", icon: "trend", hint: "Problems Brain spotted in your code structure" },
      { key: "logs", href: "/logs", label: "Activity", icon: "activity", hint: "Everything that happened, newest first", repoScoped: true },
    ],
  },
  {
    label: "Code",
    hint: "What Brain has read from your repository",
    items: [
      { key: "graph", href: "/graph", label: "Code map", icon: "graph", hint: "Which parts of the code depend on which", repoScoped: true },
      { key: "indexing", href: "/indexing", label: "Indexing", icon: "database", hint: "Each time Brain re-read your code", repoScoped: true },
      { key: "reranker", href: "/reranker", label: "Search quality", icon: "vectors", hint: "How well search finds the right code" },
      { key: "reports", href: "/reports", label: "Reports", icon: "file", hint: "Saved evaluations and checks" },
    ],
  },
  {
    label: "Memory",
    hint: "What Brain hands to agents",
    items: [
      { key: "packs", href: "/packs", label: "Context packs", icon: "layers", hint: "Briefings Brain prepared for AI agents", repoScoped: true },
      { key: "memory", href: "/memory", label: "Decisions & rules", icon: "code", hint: "Team decisions and rules agents must follow", repoScoped: true },
    ],
  },
  {
    label: "Agents & access",
    hint: "Who uses Brain and how",
    items: [
      { key: "runs", href: "/runs", label: "Agent runs", icon: "automation", hint: "What AI agents did with Brain’s help", repoScoped: true },
      { key: "mcp", href: "/mcp", label: "Agent tools", icon: "chip", hint: "The tools agents call, and how they perform" },
      { key: "admin", href: "/admin", label: "Access", icon: "key", hint: "Who and what can access Brain" },
      { key: "settings", href: "/settings", label: "Settings", icon: "gear", hint: "Telegram alerts, scheduled jobs, automation, search and indexing" },
    ],
  },
];

export const allNavItems = navGroups.flatMap((g) => g.items);

/** The nav item and its group for a path ("/" matches only itself). */
export function locate(path: string): { item: NavItem; group: string } | null {
  for (const g of navGroups)
    for (const item of g.items)
      if (item.href === "/" ? path === "/" : path.startsWith(item.href)) return { item, group: g.label };
  return null;
}
