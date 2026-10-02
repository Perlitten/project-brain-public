import type { IconName } from "@/components/Icon";

export interface NavItem {
  key: string;
  href: string;
  label: string;
  icon: IconName;
  /** One plain sentence: what the screen is for. Shown in the palette. */
  hint: string;
}

// Single source for the rail, the drawer and the command palette.
export const navGroups: { label: string; items: NavItem[] }[] = [
  {
    label: "Signal",
    items: [
      { key: "setup", href: "/setup", label: "Get started", icon: "flag", hint: "Connect Brain to your code and your AI agents" },
      { key: "command", href: "/", label: "Overview", icon: "zap", hint: "Is everything OK, and what needs attention" },
      { key: "insights", href: "/insights", label: "Findings", icon: "trend", hint: "Problems Brain spotted in your code structure" },
      { key: "logs", href: "/logs", label: "Activity", icon: "search", hint: "Everything that happened, newest first" },
    ],
  },
  {
    label: "Code",
    items: [
      { key: "graph", href: "/graph", label: "Code map", icon: "graph", hint: "Which parts of the code depend on which" },
      { key: "indexing", href: "/indexing", label: "Indexing", icon: "database", hint: "Each time Brain re-read your code" },
      { key: "reranker", href: "/reranker", label: "Search quality", icon: "vectors", hint: "How well search finds the right code" },
      { key: "reports", href: "/reports", label: "Reports", icon: "file", hint: "Saved evaluations and checks" },
    ],
  },
  {
    label: "Memory",
    items: [
      { key: "packs", href: "/packs", label: "Context packs", icon: "layers", hint: "Briefings Brain prepared for AI agents" },
      { key: "memory", href: "/memory", label: "Decisions & rules", icon: "code", hint: "Team decisions and rules agents must follow" },
    ],
  },
  {
    label: "Runtime",
    items: [
      { key: "runs", href: "/runs", label: "Agent runs", icon: "automation", hint: "What AI agents did with Brain’s help" },
      { key: "mcp", href: "/mcp", label: "Agent tools", icon: "chip", hint: "The tools agents call, and how they perform" },
      { key: "admin", href: "/admin", label: "Access", icon: "key", hint: "Who and what can access Brain" },
    ],
  },
];

export const allNavItems = navGroups.flatMap((g) => g.items);
