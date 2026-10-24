"use client";
// Older links reach Activity as /activity#jobs or #events, from before each
// block got its own view tab. The hash never reaches the server, so the page
// would open on the first tab: move such a visit to the matching ?view=.
import { useEffect } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

export function ActivityViewHash({ views }: { views: readonly string[] }) {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  useEffect(() => {
    const hash = window.location.hash.slice(1);
    if (!views.includes(hash) || params.get("view") === hash) return;
    const next = new URLSearchParams(params);
    if (hash === views[0]) next.delete("view"); else next.set("view", hash);
    router.replace(`${pathname}${next.size ? `?${next}` : ""}`, { scroll: false });
  }, [params, pathname, router, views]);
  return null;
}
