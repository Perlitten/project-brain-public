"use client";
// The one tab strip of the app, in two weights:
// - "section": the underlined strip that moves between pages of a section;
// - "view": the segmented strip that switches views inside one page.
// Both are plain links (server-rendered, shareable, back-button safe). The
// indicator slides to a clicked tab at once, before the server answers, so a
// switch never feels dead; a thin bar under the strip shows it is loading.

import Link from "next/link";
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";

export type TabTone = "bad" | "warn" | "ok" | "info";

export interface TabStripItem {
  id: string;
  href: string;
  label: ReactNode;
  /** Small count or state after the label. */
  badge?: ReactNode;
  /** Colours the badge: a problem, something to look at, or all clear. */
  tone?: TabTone;
}

// Section strips remount on every route; remembering where the indicator was
// lets it slide from the previous page's tab instead of popping into place.
let lastSection: { key: string; x: number; w: number } | null = null;

const useIso = typeof window === "undefined" ? useEffect : useLayoutEffect;

export function TabStrip({ items, current, label, variant = "view" }: { items: TabStripItem[]; current: string; label: string; variant?: "view" | "section" }) {
  const nav = useRef<HTMLElement>(null);
  const [active, setActive] = useState(current);
  const [placed, setPlaced] = useState(false);
  // The server's answer wins once it arrives (also covers back/forward).
  useEffect(() => setActive(current), [current]);

  const key = items.map((i) => i.id).join("|");
  useIso(() => {
    const el = nav.current;
    if (!el) return;
    const place = (animate: boolean) => {
      const tab = el.querySelector<HTMLElement>(`[data-tab="${CSS.escape(active)}"]`);
      if (!tab) return;
      const x = tab.offsetLeft, w = tab.offsetWidth;
      el.style.setProperty("--ind-x", `${x}px`);
      el.style.setProperty("--ind-w", `${w}px`);
      if (!animate) el.dataset.still = ""; else delete el.dataset.still;
      if (variant === "section") lastSection = { key, x, w };
      // Keep the chosen tab visible when the strip itself is wider than the screen.
      if (tab.offsetLeft < el.scrollLeft || tab.offsetLeft + w > el.scrollLeft + el.clientWidth) el.scrollTo({ left: tab.offsetLeft - 16, behavior: animate ? "smooth" : "auto" });
    };
    if (!placed) {
      // First paint: a section strip starts where the last page left it.
      if (variant === "section" && lastSection && lastSection.key === key) {
        el.style.setProperty("--ind-x", `${lastSection.x}px`);
        el.style.setProperty("--ind-w", `${lastSection.w}px`);
        el.dataset.still = "";
        requestAnimationFrame(() => requestAnimationFrame(() => place(true)));
      } else place(false);
      setPlaced(true);
    } else place(true);
    // Web fonts and window resizes change tab widths.
    const ro = new ResizeObserver(() => place(false));
    ro.observe(el);
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, key, variant]);

  const pending = active !== current;
  return (
    <nav ref={nav} className={`tabs tabs--${variant}${placed ? " tabs--placed" : ""}`} aria-label={label} aria-busy={pending || undefined}>
      <span className="tabs__ind" aria-hidden="true" />
      {items.map((t) => (
        <Link
          key={t.id}
          data-tab={t.id}
          href={t.href}
          className="tabs__tab"
          aria-current={t.id === active ? "page" : undefined}
          scroll={false}
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
            setActive(t.id);
          }}
        >
          <span className="tabs__label">{t.label}</span>
          {t.badge != null && t.badge !== "" && <span className={`tabs__badge num${t.tone ? ` tabs__badge--${t.tone}` : ""}`}>{t.badge}</span>}
        </Link>
      ))}
      <span className="tabs__loading" aria-hidden="true" />
    </nav>
  );
}
