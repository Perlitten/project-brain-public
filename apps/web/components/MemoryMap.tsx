"use client";

import { useMemo, useState, type CSSProperties } from "react";
import type { CodeModule } from "@/lib/types";
import { Term } from "./Term";
import { Odometer } from "./motion";

type CellState = "current" | "outdated" | "missing" | "excluded";

const COLS = 40;
const CELLS = 480;

const stateWords: Record<CellState, string> = {
  current: "remembered",
  outdated: "changed since",
  missing: "not read yet",
  excluded: "skipped on purpose",
};

const fmt = (n: number) => n.toLocaleString("en-US");

// Largest-remainder split of `slots` across weights, keeping at least one slot
// for any non-zero weight so a small module or a small gap never disappears.
function split(weights: number[], slots: number): number[] {
  const total = weights.reduce((s, w) => s + w, 0);
  // Nothing to share out (all-zero weights, or no slots): without this the
  // remainder loop below would never terminate.
  if (total <= 0 || slots <= 0) return weights.map(() => 0);
  const raw = weights.map((w) => (w / total) * slots);
  const out = raw.map((r, i) => (weights[i] > 0 ? Math.max(1, Math.floor(r)) : 0));
  let left = slots - out.reduce((s, v) => s + v, 0);
  const order = raw.map((r, i) => [r - Math.floor(r), i] as const).sort((a, b) => b[0] - a[0]);
  for (let k = 0; left > 0 && k < order.length * slots; k++) {
    const i = order[k % order.length][1];
    if (weights[i] > 0) {
      out[i]++;
      left--;
    }
  }
  while (left < 0 && out.length) {
    const i = out.indexOf(Math.max(...out));
    out[i]--;
    left++;
  }
  return out;
}

export function MemoryMap({
  modules,
  repoName,
  scanning,
}: {
  modules: CodeModule[];
  repoName: string;
  scanning: boolean;
}) {
  const [active, setActive] = useState<number | null>(null);
  // The cell the pointer entered: the module highlight ripples out from here.
  const [origin, setOrigin] = useState<number | null>(null);
  const focusOn = (module: number | null, cell: number | null = null) => {
    setActive(module);
    setOrigin(cell);
  };

  const cells = useMemo(() => {
    const perModule = split(
      modules.map((m) => m.chunks),
      CELLS,
    );
    const list: { module: number; state: CellState }[] = [];
    modules.forEach((m, mi) => {
      const states: CellState[] = ["current", "outdated", "missing", "excluded"];
      const counts = split(
        states.map((s) => m[s]),
        perModule[mi],
      );
      states.forEach((s, si) => {
        for (let k = 0; k < counts[si]; k++) list.push({ module: mi, state: s });
      });
    });
    return list;
  }, [modules]);

  const totals = useMemo(
    () =>
      modules.reduce(
        (t, m) => ({
          current: t.current + m.current,
          outdated: t.outdated + m.outdated,
          missing: t.missing + m.missing,
          excluded: t.excluded + m.excluded,
        }),
        { current: 0, outdated: 0, missing: 0, excluded: 0 },
      ),
    [modules],
  );
  const relevant = totals.current + totals.outdated + totals.missing;
  const pct = relevant ? (totals.current / relevant) * 100 : 0;
  const focus = active === null ? null : modules[active];
  const focusRelevant = focus ? focus.current + focus.outdated + focus.missing : 0;
  // Focused from the module list there is no pointer: ripple from the module's first cell.
  const start = active === null ? null : (origin ?? cells.findIndex((c) => c.module === active));

  return (
    <section className="map" aria-labelledby="map-title">
      <div className="map__readout" key={active ?? "all"}>
        <p className="eyebrow">Memory map · {repoName}</p>
        {focus ? (
          <>
            <h2 className="map__headline" id="map-title">
              <span className="mono">{focus.name}</span>
            </h2>
            <p className="map__hero num">
              <Odometer value={String(focusRelevant ? Math.round((focus.current / focusRelevant) * 100) : 100)} />
              <span className="map__hero-unit">%</span>
            </p>
            <p className="map__lede">
              {focus.outdated + focus.missing === 0
                ? "Brain knows this part of the code completely."
                : `${fmt(focus.outdated + focus.missing)} of ${fmt(focusRelevant)} pieces here need a refresh.`}
            </p>
          </>
        ) : (
          <>
            <h2 className="map__headline" id="map-title">
              How much of your code Brain remembers
            </h2>
            <p className="map__hero num">
              <Odometer value={pct.toFixed(pct === 100 ? 0 : 1)} />
              <span className="map__hero-unit">%</span>
            </p>
            <p className="map__lede">
              Each square is a slice of your code. Bright squares are remembered and up to date. Point at any square to
              see which part of the code it is.
            </p>
          </>
        )}
        <ul className="map__key">
          {(["current", "outdated", "missing", "excluded"] as CellState[]).map((s) => (
            <li key={s}>
              <span className={`map__swatch cell--${s}`} aria-hidden="true" />
              <span className="map__key-word">{stateWords[s]}</span>
              <span className="map__key-num num">
                <Odometer value={fmt(focus ? focus[s] : totals[s])} />
              </span>
            </li>
          ))}
        </ul>
        <p className="map__foot">
          Counted in <Term k="chunk">pieces of code</Term>. Squares are proportional, one square ≈{" "}
          {fmt(Math.round((relevant + totals.excluded) / CELLS))} pieces.
        </p>
      </div>

      <div className="map__stage">
        <div
          className={`map__grid${active !== null ? " map__grid--focus" : ""}${scanning ? " map__grid--scan" : ""}`}
          role="img"
          aria-label={`Memory map: ${fmt(totals.current)} pieces remembered, ${fmt(totals.outdated)} changed since, ${fmt(totals.missing)} not read yet, ${fmt(totals.excluded)} skipped.`}
          onMouseLeave={() => focusOn(null)}
          style={{ "--cols": COLS } as CSSProperties}
        >
          {cells.map((c, i) => {
            const row = Math.floor(i / COLS);
            const col = i % COLS;
            const on = active === c.module;
            let ripple = 0;
            if (on && start !== null) {
              const dr = row - Math.floor(start / COLS);
              const dc = col - (start % COLS);
              ripple = Math.round(Math.hypot(dr, dc) * 22);
            }
            return (
              <span
                key={i}
                className={`cell cell--${c.state}${on ? " cell--on" : ""}`}
                style={
                  {
                    "--d": `${(row + col) * 9}ms`,
                    "--r": `${ripple}ms`,
                    "--c": col,
                  } as CSSProperties
                }
                onMouseEnter={() => active !== c.module && focusOn(c.module, i)}
              />
            );
          })}
          {scanning && <span className="map__scan" aria-hidden="true" />}
        </div>
        <div className="map__modules" role="group" aria-label="Parts of the code">
          {modules.map((m, i) => {
            const bad = m.outdated + m.missing;
            return (
              <button
                key={m.name}
                type="button"
                className={`map__module${active === i ? " is-on" : ""}`}
                onMouseEnter={() => focusOn(i)}
                onMouseLeave={() => focusOn(null)}
                onFocus={() => focusOn(i)}
                onBlur={() => focusOn(null)}
                aria-pressed={active === i}
              >
                <span className={`map__module-dot ${bad ? "tone-warn" : "tone-ok"}`} aria-hidden="true" />
                {m.name}
              </button>
            );
          })}
        </div>
        {scanning && (
          <p className="map__status">
            <span className="live-dot" aria-hidden="true" /> Brain is reading the latest changes right now
          </p>
        )}
      </div>
    </section>
  );
}
