"use client";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { withProject } from "@/lib/nav";

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
  quality: { label: "Quality views", items: [["/quality", "Benchmark"], ["/reports", "Reports"], ["/reranker", "Reranker"]] },
  logs: { label: "Activity views", items: [["/logs", "Requests & jobs"], ["/runs", "Agent tasks"]] },
  settings: { label: "Settings views", items: [["/settings", "General"], ["/admin", "Access"], ["/mcp", "Agent tools"]] },
} as const;

export function SectionTabs({ section, active }: { section: keyof typeof SECTIONS; active: string }) {
  const s = SECTIONS[section];
  return <SectionNav label={s.label} items={s.items.map(([href, label]) => ({ href, label, active: href === active }))} />;
}
