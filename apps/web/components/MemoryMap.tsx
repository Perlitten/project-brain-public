"use client";

import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import type { CodeModule } from "@/lib/types";
import { Term } from "./Term";
import { Odometer } from "./motion";

type CellState = "current" | "outdated" | "missing" | "excluded";

const STATES: CellState[] = ["current", "outdated", "missing", "excluded"];
// 840 = 56 × 15 on wide screens, 28 × 30 on phones.
const CELLS = 840;

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

type RGB = [number, number, number];
const hex = (v: string): RGB => {
  const h = v.trim().replace("#", "");
  const n = parseInt(h.length === 3 ? h.replace(/./g, "$&$&") : h, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
};
const mix = (a: RGB, b: RGB, t: number): RGB => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
const css = (c: RGB, a = 1) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`;
const clamp01 = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v);
const easeOut = (t: number) => 1 - Math.pow(1 - t, 3);

// Everything the animation loop reads, kept outside React so pointer moves and
// frames never re-render the component.
interface Live {
  active: number | null;
  origin: number;
  focusAt: number;
  pointer: { x: number; y: number } | null;
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
  const stageRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const tipRef = useRef<HTMLDivElement>(null);
  const live = useRef<Live>({ active: null, origin: 0, focusAt: 0, pointer: null });

  const cells = useMemo(() => {
    const perModule = split(
      modules.map((m) => m.chunks),
      CELLS,
    );
    const list: { module: number; state: CellState }[] = [];
    modules.forEach((m, mi) => {
      const counts = split(
        STATES.map((s) => m[s]),
        perModule[mi],
      );
      STATES.forEach((s, si) => {
        for (let k = 0; k < counts[si]; k++) list.push({ module: mi, state: s });
      });
    });
    return list;
  }, [modules]);

  const focusOn = (module: number | null, cell?: number) => {
    const l = live.current;
    if (l.active === module) return;
    l.active = module;
    l.origin = cell ?? (module === null ? 0 : Math.max(0, cells.findIndex((c) => c.module === module)));
    l.focusAt = performance.now();
    setActive(module);
  };

  useEffect(() => {
    const canvas = canvasRef.current;
    const stage = stageRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !stage || !ctx) return;
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const n = cells.length;

    const cs = getComputedStyle(document.documentElement);
    const v = (name: string) => hex(cs.getPropertyValue(name) || "#000");
    const base = v("--gray-3");
    const white = v("--text-1");
    const info = v("--info-fg");
    const tone: Record<CellState, RGB> = {
      current: v("--teal-fg"),
      outdated: v("--warn-fg"),
      missing: v("--bad-fg"),
      excluded: v("--gray-7"),
    };
    const rest: Record<CellState, RGB> = {
      current: mix(base, tone.current, 0.58),
      outdated: mix(base, tone.outdated, 0.78),
      missing: mix(base, tone.missing, 0.82),
      excluded: v("--gray-5"),
    };

    // Per-cell animated values: neural activity, focus brightness, lens size.
    const act = new Float32Array(n);
    const lit = new Float32Array(n).fill(1);
    const glow = new Float32Array(n);
    const grow = new Float32Array(n);
    const phase = Float32Array.from({ length: n }, () => Math.random() * Math.PI * 2);
    const currentIdx = cells.flatMap((c, i) => (c.state === "current" ? [i] : []));

    let cols = 56;
    let rows = 15;
    let size = 10;
    let gap = 3;
    let width = 0;
    let height = 0;
    let dpr = 1;
    const layout = () => {
      width = stage.clientWidth;
      cols = width < 560 ? 28 : 56;
      rows = Math.ceil(n / cols);
      gap = width < 560 ? 2 : 3;
      size = (width - gap * (cols - 1)) / cols;
      height = rows * (size + gap) - gap;
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      canvas.style.height = `${height}px`;
    };
    layout();
    const ro = new ResizeObserver(layout);
    ro.observe(stage);

    const cellAt = (x: number, y: number) => {
      const c = Math.floor(x / (size + gap));
      const r = Math.floor(y / (size + gap));
      const i = r * cols + c;
      return c >= 0 && c < cols && r >= 0 && i < n ? i : -1;
    };
    const cellDist = (a: number, b: number) => Math.hypot(Math.floor(a / cols) - Math.floor(b / cols), (a % cols) - (b % cols));

    // Thought sparks: a ring of activity spreading from a remembered cell.
    const sparks: { at: number; t: number; power: number }[] = [];
    let nextSpark = 0;

    let t0 = -1; // first frame on screen: the intro wave starts here
    let raf = 0;
    let visible = false;

    const frame = (now: number) => {
      raf = 0;
      if (t0 < 0) t0 = now;
      const t = now - t0;
      const l = live.current;
      const intro = reduced ? 1e9 : t;
      const waveSpan = 760 / (rows + cols);

      // Spawn sparks, a little faster than one every 180ms.
      if (!reduced && currentIdx.length && t > 900 && now > nextSpark) {
        sparks.push({ at: currentIdx[(Math.random() * currentIdx.length) | 0], t: now, power: 0.55 + Math.random() * 0.45 });
        nextSpark = now + 120 + Math.random() * 160;
      }
      for (let s = sparks.length - 1; s >= 0; s--) if (now - sparks[s].t > 1400) sparks.splice(s, 1);

      const scanX = scanning && !reduced ? ((t - 1200) / 4800) % 1 : -1;
      const pointer = l.pointer;

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);

      for (let i = 0; i < n; i++) {
        const cell = cells[i];
        const r = Math.floor(i / cols);
        const c = i % cols;
        const x = c * (size + gap);
        const y = r * (size + gap);

        // Focus: the hovered module brightens, the rest dims, rippling out
        // from where the pointer entered.
        const on = l.active !== null && cell.module === l.active;
        const reached = reduced || now - l.focusAt > cellDist(i, l.origin) * 18;
        const litTarget = l.active === null ? 1 : on ? 1 : 0.16;
        const glowTarget = on && reached ? 1 : 0;
        if (reached || l.active === null) lit[i] += (litTarget - lit[i]) * (reduced ? 1 : 0.16);
        glow[i] += (glowTarget - glow[i]) * (reduced ? 1 : 0.14);

        // Neural activity from sparks.
        let a = 0;
        if (cell.state === "current") {
          for (const s of sparks) {
            const age = now - s.t;
            const d = cellDist(i, s.at);
            const ring = age * 0.009;
            const dd = d - ring;
            if (dd > -2 && dd < 2) a += Math.exp(-dd * dd * 1.6) * s.power * (1 - age / 1400) * Math.exp(-d * 0.22);
          }
        }
        act[i] += (Math.min(1, a) - act[i]) * 0.35;

        // Lens: cells near the pointer swell and brighten.
        let lens = 0;
        if (pointer && !reduced) {
          const dx = x + size / 2 - pointer.x;
          const dy = y + size / 2 - pointer.y;
          lens = clamp01(1 - Math.hypot(dx, dy) / 84);
          lens *= lens;
        }
        grow[i] += (lens - grow[i]) * 0.3;

        // Intro: a diagonal wave flashes each cell white, then it settles.
        const p = clamp01((intro - (r + c) * waveSpan) / 620);
        if (p <= 0) continue;
        const pop = easeOut(clamp01(p / 0.45));
        const settle = clamp01((p - 0.45) / 0.55);

        let col = rest[cell.state];
        if (cell.state === "outdated" && !reduced) col = mix(col, tone.outdated, 0.25 + 0.25 * Math.sin(now / 900 + phase[i]));
        col = mix(col, tone[cell.state], glow[i] * 0.85);
        col = mix(col, white, act[i] * 0.75 + grow[i] * 0.35);
        if (scanX >= 0) {
          const sx = scanX * width;
          const behind = sx - (x + size / 2);
          if (behind > -6 && behind < 60) col = mix(col, info, (1 - Math.max(0, behind) / 60) * 0.85);
        }
        if (settle < 1) col = mix(white, col, settle);

        const alpha = lit[i] * pop;
        const s = size * (0.25 + 0.75 * pop) * (1 + grow[i] * 0.55);
        const off = (size - s) / 2;
        ctx.fillStyle = css(col, alpha);
        ctx.fillRect(x + off, y + off, s, s);

        // Fake bloom for the brightest cells.
        const bloom = act[i] * 0.9 + glow[i] * grow[i] * 0.6;
        if (bloom > 0.12) {
          ctx.globalCompositeOperation = "lighter";
          ctx.fillStyle = css(cell.state === "current" ? tone.current : tone[cell.state], bloom * 0.18 * alpha);
          const b = s * 2.2;
          ctx.fillRect(x + (size - b) / 2, y + (size - b) / 2, b, b);
          ctx.globalCompositeOperation = "source-over";
        }
      }

      if (scanX >= 0) {
        const sx = scanX * width;
        const g = ctx.createLinearGradient(sx - 70, 0, sx, 0);
        g.addColorStop(0, css(info, 0));
        g.addColorStop(1, css(info, 0.22));
        ctx.fillStyle = g;
        ctx.fillRect(sx - 70, -4, 70, height + 8);
        ctx.fillStyle = css(info, 0.95);
        ctx.fillRect(sx - 1, -4, 2, height + 8);
      }

      if (visible && !document.hidden) raf = requestAnimationFrame(frame);
    };

    const kick = () => {
      if (!raf && visible) raf = requestAnimationFrame(frame);
    };
    // Starts (and replays the intro) only once the map is actually on screen,
    // e.g. after the start-up overlay, and sleeps while it is scrolled away.
    const io = new IntersectionObserver(([e]) => {
      visible = e.isIntersecting;
      kick();
    });
    io.observe(canvas);
    const onVis = () => kick();
    document.addEventListener("visibilitychange", onVis);

    const onMove = (e: PointerEvent) => {
      const rect = canvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      live.current.pointer = { x, y };
      const i = cellAt(x, y);
      const tip = tipRef.current;
      if (i >= 0) {
        const cell = cells[i];
        if (live.current.active !== cell.module) focusOn(cell.module, i);
        if (tip) {
          tip.textContent = `${modules[cell.module].name} · ${stateWords[cell.state]}`;
          tip.style.transform = `translate(${Math.min(x + 14, rect.width - tip.offsetWidth)}px, ${y - 34}px)`;
          tip.dataset.on = "";
        }
      }
      kick();
    };
    const onLeave = () => {
      live.current.pointer = null;
      focusOn(null);
      if (tipRef.current) delete tipRef.current.dataset.on;
      kick();
    };
    canvas.addEventListener("pointermove", onMove);
    canvas.addEventListener("pointerleave", onLeave);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      io.disconnect();
      document.removeEventListener("visibilitychange", onVis);
      canvas.removeEventListener("pointermove", onMove);
      canvas.removeEventListener("pointerleave", onLeave);
    };
    // focusOn only touches refs and a state setter; cells/modules drive the scene.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cells, modules, scanning]);

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
              Each square is a slice of your code. Flashes are Brain recalling what it remembers. Point at any square to
              see which part of the code it is.
            </p>
          </>
        )}
        <ul className="map__key">
          {STATES.map((s) => (
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
          {fmt(Math.max(1, Math.round((relevant + totals.excluded) / CELLS)))} pieces.
        </p>
      </div>

      <div className="map__stage">
        <div className="map__canvas" ref={stageRef}>
          <canvas
            ref={canvasRef}
            role="img"
            aria-label={`Memory map: ${fmt(totals.current)} pieces remembered, ${fmt(totals.outdated)} changed since, ${fmt(totals.missing)} not read yet, ${fmt(totals.excluded)} skipped.`}
          />
          <div className="map__tip" ref={tipRef} aria-hidden="true" />
        </div>
        <div className="map__modules" role="group" aria-label="Parts of the code">
          {modules.map((m, i) => {
            const bad = m.outdated + m.missing;
            return (
              <button
                key={m.name}
                type="button"
                className={`map__module${active === i ? " is-on" : ""}`}
                style={{ "--i": Math.min(i, 24) } as CSSProperties}
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
