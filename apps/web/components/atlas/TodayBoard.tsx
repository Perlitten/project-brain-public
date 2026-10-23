"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { AtlasLegend, AtlasMap, AtlasVerdict, type AtlasRegionData } from "./Atlas";
import { moduleNote, type ModuleTile, type ModuleTotals, type Segment } from "@/lib/atlas";
import type { ContextPack } from "@/lib/types";

export interface TodayRegion extends Omit<AtlasRegionData, "tiles" | "dim"> {
  tiles: ModuleTile[];
  totals: ModuleTotals;
}

// Live data refreshes while the tab is visible; demo data never changes.
const REFRESH_MS = 45_000;

const packMemory = (p: ContextPack) => {
  if (!p.memory) return "contents not recorded";
  const { rules, decisions } = p.memory;
  if (rules === null || decisions === null) return "contents not recorded";
  if (!rules && !decisions) return "code only, no memory";
  return [rules ? `${rules} ${rules === 1 ? "rule" : "rules"}` : "", decisions ? `${decisions} ${decisions === 1 ? "decision" : "decisions"}` : ""].filter(Boolean).join(" · ");
};
const packRevision = (p: ContextPack) => p.stale === null ? "revision not recorded" : p.stale ? "built from an older revision" : "built from current code";

export function TodayBoard({ segments, sub, actions, regions, hidden, packs, live, footer }: {
  segments: Segment[];
  sub: ReactNode;
  actions: ReactNode;
  regions: TodayRegion[];
  hidden: number;
  packs: ContextPack[];
  live: boolean;
  footer: ReactNode;
}) {
  const router = useRouter();
  // Hover is a glance; a click pins the project so the feed stays filtered to it.
  const [hover, setHover] = useState<{ region: string | null; tile: string | null }>({ region: null, tile: null });
  const [pinned, setPinned] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const focus = hover.region ?? pinned;

  // Packs that arrive after the first render slide in; the first render is still.
  const seen = useRef<Set<string> | null>(null);
  const fresh = new Set<string>();
  if (seen.current) for (const p of packs) if (!seen.current.has(p.id)) fresh.add(p.id);
  useEffect(() => { seen.current = new Set(packs.map((p) => p.id)); }, [packs]);

  useEffect(() => {
    if (!live) return;
    const timer = setInterval(() => { if (document.visibilityState === "visible") router.refresh(); }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [live, router]);

  const shown = focus ? regions.find((r) => r.key === focus) : undefined;
  const totals = shown?.totals ?? regions.reduce((t, r) => ({ current: t.current + r.totals.current, outdated: t.outdated + r.totals.outdated, missing: t.missing + r.totals.missing, excluded: t.excluded + r.totals.excluded }), { current: 0, outdated: 0, missing: 0, excluded: 0 });

  const mapRegions: AtlasRegionData[] = regions.map((r) => ({
    ...r,
    dim: Boolean(focus && focus !== r.key),
    tiles: r.tiles.map((t) => ({
      key: t.key,
      name: t.name,
      note: moduleNote(t),
      noteWarn: t.state === "chg" && t.changed > 0,
      state: t.state,
      span: t.span,
      ring: pinned === r.key && hover.tile === t.key,
      label: `${r.name}, ${t.more ? `${t.more} smaller modules` : t.name}: ${t.chunks.toLocaleString("en-US")} pieces of code, ${t.changed.toLocaleString("en-US")} changed since Brain read them, ${t.unread.toLocaleString("en-US")} not read yet`,
    })),
  }));

  return (
    <div className="atlas">
      <AtlasVerdict segments={segments} sub={sub} actions={actions} onMention={(ref) => setHover({ region: ref, tile: null })} onPick={(ref) => setPinned((p) => (p === ref ? null : ref))} />
      <div className="atlas-split">
        <section aria-label="What Brain remembers, by module">
          {mapRegions.length > 0 && <>
            <AtlasMap
              label="Indexed code by project and module"
              regions={mapRegions}
              onHover={(region, tile) => setHover({ region, tile })}
              onPick={(region) => setPinned((p) => (p === region ? null : region))}
            />
            <AtlasLegend
              items={[
                { state: "ok", count: totals.current, label: "remembered" },
                { state: "chg", count: totals.outdated, label: "changed since" },
                { state: "miss", count: totals.missing, label: "not read yet" },
              ]}
              extra={<span className="text-faint">{totals.excluded.toLocaleString("en-US")} skipped{shown ? ` · ${shown.name}` : ""}</span>}
            />
            {(hidden > 0 || pinned) && <p className="atlas-foot">
              {hidden > 0 && `${hidden} more ${hidden === 1 ? "project is" : "projects are"} counted above but not drawn. `}
              {pinned && <>Showing packs for {regions.find((r) => r.key === pinned)?.name ?? pinned}. <button type="button" className="link" onClick={() => setPinned(null)}>Show all</button></>}
            </p>}
          </>}
        </section>

        <aside aria-label="What your agents got from Brain">
          <div className="atlas-side__head">
            <h2 className="atlas-side__title">Given to agents</h2>
            {live && <span className="live-dot" aria-hidden="true" />}
            <span className="atlas-side__count">{packs.length ? `latest ${packs.length}` : ""}</span>
          </div>
          {packs.length ? packs.map((p) => {
            const isOpen = open === p.id;
            return (
              <button
                key={p.id}
                type="button"
                className="atlas-ev"
                data-new={fresh.has(p.id) || undefined}
                data-dim={Boolean(focus && p.repo && p.repo !== focus) || undefined}
                aria-expanded={isOpen}
                onClick={() => setOpen(isOpen ? null : p.id)}
                onMouseEnter={() => p.repo && setHover({ region: p.repo, tile: null })}
                onMouseLeave={() => p.repo && setHover({ region: null, tile: null })}
              >
                <span className="atlas-ev__top">
                  <span className="atlas-ev__time">{p.createdAt}</span>
                  <span className="atlas-ev__who"><b>{p.consumer}</b><span> got a context pack</span></span>
                </span>
                <span className="atlas-ev__line">{p.task}</span>
                <span className="atlas-ev__meta">{[packMemory(p), p.repo].filter(Boolean).join(" · ")}</span>
                {isOpen && <span className="atlas-ev__more">
                  {p.tokens.toLocaleString("en-US")} tokens from {p.files} {p.files === 1 ? "file" : "files"}, {packRevision(p)}.{" "}
                  <Link href={`/packs/${encodeURIComponent(p.id)}`} onClick={(e) => e.stopPropagation()}>Open pack {p.id}</Link>
                </span>}
              </button>
            );
          }) : <p className="atlas-empty">No agent has asked Brain for context yet. Packs appear here as agents start tasks.</p>}
          {footer && <p className="atlas-side__foot">{footer}</p>}
        </aside>
      </div>
    </div>
  );
}
