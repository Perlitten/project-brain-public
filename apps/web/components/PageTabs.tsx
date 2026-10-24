// Tabs that split one page into full screens. Each tab is a link (?view=…),
// so it renders on the server, survives a reload and can be shared; other
// query parameters (e.g. ?repo=) are kept.
//
// The page anatomy every screen follows:
//   PageHead (title + one status line) → section tabs → view tabs → ViewPane.
// A view tab's badge says what is inside before you open it (a count, or a
// tone: bad = something is broken, warn = look at it, ok = all clear), and
// each ViewPane fits one screen: long lists page instead of scrolling.
import type { ReactNode } from "react";
import { TabStrip, type TabTone } from "./TabStrip";

export type { TabTone } from "./TabStrip";

export interface PageTab {
  id: string;
  label: ReactNode;
  /** Small count or status after the label. */
  badge?: ReactNode;
  /** Colours the badge. */
  tone?: TabTone;
  /** Old name for tone "bad". */
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
    // A list's page, search and filter belong to the view they were set in.
    for (const [k, v] of Object.entries(params)) if (v && !VIEW_LOCAL.has(k)) q.set(k, v);
    if (i > 0) q.set("view", id);
    const s = q.toString();
    return s ? `${path}?${s}` : path;
  };
  return (
    <TabStrip
      label={label}
      current={current}
      items={tabs.map((t, i) => ({ id: t.id, href: href(t.id, i), label: t.label, badge: t.badge, tone: t.tone ?? (t.alarm ? "bad" : undefined) }))}
    />
  );
}

const VIEW_LOCAL = new Set(["view", "page", "page_size", "q", "status"]);

/**
 * The body of the selected view. Re-keyed per view so it enters with motion.
 * On a desktop screen the page itself never scrolls: the head and tabs stay
 * put and this pane scrolls on its own. `fill` goes one step further for a
 * view that is one list: the list scrolls inside its frame and its search bar
 * and pager stay in sight.
 */
export function ViewPane({ id, children, className, fill }: { id: string; children: ReactNode; className?: string; fill?: boolean }) {
  return (
    <div key={id} className={`view-pane${fill ? " view-pane--fill" : ""}${className ? ` ${className}` : ""}`} data-view={id}>
      {children}
    </div>
  );
}

/** The selected tab id, falling back to the first one. */
export const pickTab = <T extends string>(ids: readonly T[], view?: string): T => (ids.includes(view as T) ? (view as T) : ids[0]);
