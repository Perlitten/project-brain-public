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
