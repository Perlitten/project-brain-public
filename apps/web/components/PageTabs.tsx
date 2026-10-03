// Tabs that split one page into full screens. Each tab is a link (?view=…),
// so it renders on the server, survives a reload and can be shared; other
// query parameters (e.g. ?repo=) are kept.
import Link from "next/link";
import type { ReactNode } from "react";

export interface PageTab {
  id: string;
  label: ReactNode;
  /** Small count or status after the label. */
  badge?: ReactNode;
  /** Draw the badge as a problem (e.g. rule breaks). */
  alarm?: boolean;
}

export function PageTabs({
  tabs,
  current,
  path,
  params = {},
  label,
}: {
  tabs: PageTab[];
  current: string;
  path: string;
  params?: Record<string, string | undefined>;
  label: string;
}) {
  const href = (id: string, i: number) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v && k !== "view") q.set(k, v);
    if (i > 0) q.set("view", id);
    const s = q.toString();
    return s ? `${path}?${s}` : path;
  };
  return (
    <nav className="page-tabs" aria-label={label}>
      {tabs.map((t, i) => (
        <Link key={t.id} href={href(t.id, i)} className="page-tabs__tab" aria-current={t.id === current ? "page" : undefined} scroll={false}>
          {t.label}
          {t.badge != null && <span className={`page-tabs__badge num${t.alarm ? " page-tabs__badge--bad" : ""}`}>{t.badge}</span>}
        </Link>
      ))}
    </nav>
  );
}

/** The selected tab id, falling back to the first one. */
export const pickTab = <T extends string>(ids: readonly T[], view?: string): T => (ids.includes(view as T) ? (view as T) : ids[0]);
