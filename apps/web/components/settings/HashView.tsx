"use client";
// Old links point at a block by its anchor (/settings#git, /setup#provider).
// Those blocks now live in view tabs, so an anchor that belongs to another
// view switches to it (keeping the anchor), instead of landing on the first
// view where the anchor does not exist.

import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";

export function HashView({ views, current, first }: { views: Record<string, string>; current: string; first: string }) {
  const router = useRouter();
  const pathname = usePathname();
  useEffect(() => {
    const go = () => {
      const view = views[decodeURIComponent(location.hash.slice(1))];
      if (!view || view === current) return;
      const q = new URLSearchParams(location.search);
      if (view === first) q.delete("view"); else q.set("view", view);
      const s = q.toString();
      router.replace(`${pathname}${s ? `?${s}` : ""}${location.hash}`);
    };
    go();
    window.addEventListener("hashchange", go);
    return () => window.removeEventListener("hashchange", go);
  }, [views, current, first, pathname, router]);
  return null;
}
