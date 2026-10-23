"use client";

import Link from "next/link";
import type { CSSProperties, ReactNode } from "react";
import type { Segment, TileState } from "@/lib/atlas";
import "./atlas.css";

// Presentational pieces of the atlas. The boards (Dashboard, Memory) own the
// hover, selection and filter state; these only draw it, so both screens share
// one look and one set of motions.

export interface AtlasTileData {
  key: string;
  name: string;
  note?: string;
  noteWarn?: boolean;
  state: TileState;
  span: 2 | 3;
  /** Full description for assistive tech: the tile itself shows a clipped name. */
  label: string;
  dim?: boolean;
  ring?: boolean;
  pulse?: boolean;
}

export interface AtlasRegionData {
  key: string;
  name: string;
  href?: string;
  note?: string;
  noteTone?: "warn" | "bad" | "ok";
  /** Relative width on the row: bigger projects get more room. */
  weight: number;
  tiles: AtlasTileData[];
  /** Shown instead of tiles when the region has nothing to draw. */
  empty?: string;
  dim?: boolean;
}

/**
 * Scrolls an element into view only when it is out of sight, without the
 * smooth glide for people who asked for less motion.
 */
export function reveal(el: Element | null) {
  if (!el) return;
  const box = el.getBoundingClientRect();
  if (box.top >= 0 && box.bottom <= window.innerHeight) return;
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  el.scrollIntoView({ block: box.height > window.innerHeight ? "start" : "nearest", behavior: still ? "auto" : "smooth" });
}

/** Brings a project's region on the map into view. */
export const revealRegion = (key: string) => reveal(document.querySelector(`[data-region="${CSS.escape(key)}"]`));

/** The screen's opening sentence. Mentions point at their place on the map. */
export function AtlasVerdict({ segments, sub, actions, onMention, onPick }: {
  segments: Segment[];
  sub?: ReactNode;
  actions?: ReactNode;
  onMention?: (ref: string | null) => void;
  onPick?: (ref: string) => void;
}) {
  return (
    <div className="atlas-verdict">
      <div className="atlas-verdict__text">
        <p className="atlas-verdict__line">
          {segments.map((s, i) => typeof s === "string"
            ? <span key={i}>{s}</span>
            : "num" in s
              ? <span key={i} className={`atlas-num atlas-num--${s.tone}`}>{s.num}</span>
              : <button key={i} type="button" className="atlas-ref" onMouseEnter={() => onMention?.(s.ref)} onMouseLeave={() => onMention?.(null)} onFocus={() => onMention?.(s.ref)} onBlur={() => onMention?.(null)} onClick={() => onPick?.(s.ref)}>{s.label}</button>)}
        </p>
        {sub && <p className="atlas-verdict__sub">{sub}</p>}
      </div>
      {actions && <div className="atlas-verdict__actions">{actions}</div>}
    </div>
  );
}

export function AtlasMap({ label, regions, onHover, onPick }: {
  label: string;
  regions: AtlasRegionData[];
  onHover?: (region: string | null, tile: string | null) => void;
  onPick?: (region: string, tile: string) => void;
}) {
  return (
    <div className="atlas-map" role="group" aria-label={label}>
      {regions.map((g) => (
        <section key={g.key} className="atlas-region" data-region={g.key} data-dim={g.dim || undefined} style={{ "--w": g.weight } as CSSProperties} aria-label={g.name}>
          <div className="atlas-region__head">
            {g.href ? <Link className="atlas-region__name" href={g.href}>{g.name}</Link> : <span className="atlas-region__name">{g.name}</span>}
            {g.note && <span className={`atlas-region__note${g.noteTone ? ` atlas-region__note--${g.noteTone}` : ""}`}>{g.note}</span>}
          </div>
          {g.tiles.length ? (
            <div className="atlas-region__grid">
              {g.tiles.map((t) => (
                <button
                  key={t.key}
                  type="button"
                  className={`atlas-tile atlas-st-${t.state}`}
                  data-span={t.span}
                  data-dim={t.dim || undefined}
                  data-ring={t.ring || undefined}
                  data-pulse={t.pulse || undefined}
                  aria-label={t.label}
                  aria-pressed={onPick ? Boolean(t.ring) : undefined}
                  onMouseEnter={() => onHover?.(g.key, t.key)}
                  onMouseLeave={() => onHover?.(null, null)}
                  onFocus={() => onHover?.(g.key, t.key)}
                  onBlur={() => onHover?.(null, null)}
                  onClick={() => onPick?.(g.key, t.key)}
                >
                  <span className="atlas-tile__name" aria-hidden="true">{t.name}</span>
                  {t.note && <span className={`atlas-tile__note${t.noteWarn ? " atlas-tile__note--warn" : ""}`} aria-hidden="true">{t.note}</span>}
                </button>
              ))}
            </div>
          ) : <p className="atlas-region__empty">{g.empty ?? "Nothing to draw yet."}</p>}
        </section>
      ))}
    </div>
  );
}

export function AtlasLegend({ items, extra }: { items: { state: TileState; count: number; label: string }[]; extra?: ReactNode }) {
  return (
    <p className="atlas-legend">
      {items.map((it) => (
        <span key={it.state}>
          <span className={`atlas-sw atlas-sw--${it.state}`} aria-hidden="true" />
          <span className={`atlas-legend__num atlas-legend__num--${it.state}`}>{it.count.toLocaleString("en-US")}</span> {it.label}
        </span>
      ))}
      {extra}
    </p>
  );
}
