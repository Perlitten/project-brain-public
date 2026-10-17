"use client";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { adminOnly, withProject } from "@/lib/nav";
import { useRestrictedMember } from "./MemberScope";

export type SectionNavItem = { href: string; label: string; active?: boolean };

/** Secondary owner-task navigation. The primary Shell navigation remains unchanged. */
export function SectionNav({ label, items }: { label: string; items: SectionNavItem[] }) {
  const params = useSearchParams();
  const repo = params.get("repo") ?? undefined;
  return (
    <nav className="page-tabs section-nav" aria-label={label}>
      {items.map((item) => (
        <Link key={item.href} href={withProject(item.href, repo)} className="page-tabs__tab" aria-current={item.active ? "page" : undefined} scroll={false}>
          {item.label}
        </Link>
      ))}
    </nav>
  );
}

// One tab set per section, defined once, so every page of a section shows the
// same tabs in the same order with the same names.
const SECTIONS = {
  projects: { label: "Project views", items: [["/projects", "Projects"], ["/indexing", "Index runs"], ["/graph", "Code map"], ["/insights", "Findings"]] },
  memory: { label: "Memory views", items: [["/memory", "Decisions & rules"], ["/packs", "Context packs"]] },
  quality: { label: "Quality views", items: [["/quality", "Quality"]] },
  logs: { label: "Activity views", items: [["/logs", "Requests & jobs"], ["/runs", "Agent tasks"]] },
  // Reranker is a switch with a measurement next to it, not a report: it
  // belongs with the other things you configure.
  settings: { label: "Settings views", items: [["/settings", "General"], ["/admin", "Access"], ["/team", "Team"], ["/team/me", "Me"], ["/team/activity", "Team activity"], ["/mcp", "Agent tools"], ["/reranker", "Reranker"]] },
} as const;

export function SectionTabs({ section, active }: { section: keyof typeof SECTIONS; active: string }) {
  const restricted = useRestrictedMember();
  const s = SECTIONS[section];
  const items = s.items.filter(([href]) => !restricted || !adminOnly(href));
  // A single-destination section is not a choice, so it draws no tab strip.
  if (items.length < 2) return null;
  return <SectionNav label={s.label} items={items.map(([href, label]) => ({ href, label, active: href === active }))} />;
}
