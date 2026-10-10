"use client";

import { useState, type CSSProperties } from "react";
import type { ModuleEdge } from "@/lib/types";

// Three-lane layout: entry points on the left, core logic in the middle,
// storage on the right. Edges draw from source to target; a rule violation
// keeps flowing so the eye lands on it first. Particles travel each edge in its
// direction (more particles = more calls), and pointing at a module lights up
// only its own connections.
const lanes: Record<string, [number, number, string]> = {
  "apps/api": [0, 0, "web API"],
  "apps/mcp_server": [0, 1, "agent tools"],
  "brain/workers": [0, 2, "background jobs"],
  "brain/insights": [0, 3, "findings"],
  "brain/search": [1, 0, "code search"],
  "brain/context": [1, 1, "context packs"],
  "brain/retrieval": [1, 2, "retrieval"],
  "brain/indexers": [1, 3, "code reading"],
  "brain/graph": [1, 4, "structure"],
  "brain/database": [2, 1, "storage"],
  "brain/embeddings": [2, 3, "meaning vectors"],
};

const NW = 170;
const NH = 52;
const PITCH = NW + 36; // between columns inside one lane
const LANE_GAP = 84; // extra room between lanes for the curves
const ROW_H = 86;
const rowY = (r: number) => 30 + r * ROW_H;

type Node = { name: string; lane: number; sub: number; row: number; label: string };

// Known modules keep their lane and row; any other module the API reports is
// placed by its role (only uses others → left, only used → right, both →
// middle). A lane that outgrows `rows` wraps into another column instead of
// growing downwards, so the map stays about one screen tall however large the
// project is. Only modules that appear in an edge are drawn.
function layout(edges: ModuleEdge[]) {
  const names = [...new Set(edges.flatMap((e) => [e.from, e.to]))].sort();
  const rows = Math.min(8, Math.max(5, Math.ceil(names.length / 4)));
  const used = [new Set<string>(), new Set<string>(), new Set<string>()];
  const nodes: Node[] = [];
  for (const name of names) {
    const known = lanes[name];
    if (!known) continue;
    nodes.push({ name, lane: known[0], sub: 0, row: known[1], label: known[2] });
    used[known[0]].add(`0:${known[1]}`);
  }
  for (const name of names) {
    if (lanes[name]) continue;
    const out = edges.some((e) => e.from === name);
    const inc = edges.some((e) => e.to === name);
    const lane = out && !inc ? 0 : inc && !out ? 2 : 1;
    let slot = 0;
    while (used[lane].has(`${Math.floor(slot / rows)}:${slot % rows}`)) slot++;
    used[lane].add(`${Math.floor(slot / rows)}:${slot % rows}`);
    nodes.push({ name, lane, sub: Math.floor(slot / rows), row: slot % rows, label: "" });
  }
  const subs = [0, 1, 2].map((l) => Math.max(1, ...nodes.filter((n) => n.lane === l).map((n) => n.sub + 1)));
  const first = [0, subs[0], subs[0] + subs[1]];
  const x = (n: Node) => 70 + (first[n.lane] + n.sub) * PITCH + n.lane * LANE_GAP;
  const cols = subs[0] + subs[1] + subs[2];
  const W = 70 + (cols - 1) * PITCH + 2 * LANE_GAP + NW + 40;
  const H = Math.max(470, Math.max(0, ...nodes.map((n) => n.row)) * ROW_H + 30 + NH + 40);
  return { nodes, x, W, H };
}

export function CodeGraph({ edges }: { edges: ModuleEdge[] }) {
  const { nodes, x, W, H } = layout(edges);
  const at = new Map(nodes.map((n) => [n.name, { x: x(n), y: rowY(n.row), sub: n.label }]));
  const pos = (name: string) => at.get(name) ?? { x: 70, y: rowY(0), sub: "" };
  const bad = new Set(edges.filter((e) => e.violation).flatMap((e) => [e.from, e.to]));
  const path = (e: ModuleEdge) => {
    const a = pos(e.from);
    const b = pos(e.to);
    const y1 = a.y + NH / 2;
    const y2 = b.y + NH / 2;
    if (a.x === b.x) {
      // Same lane: loop around the left edge so the line never crosses a node.
      return `M ${a.x} ${y1} C ${a.x - 70} ${y1}, ${b.x - 70} ${y2}, ${b.x} ${y2}`;
    }
    // Leave from the side that faces the target, so no line cuts through a box.
    const x1 = a.x < b.x ? a.x + NW : a.x;
    const x2 = a.x < b.x ? b.x : b.x + NW;
    const bend = (x2 - x1) / 2;
    return `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`;
  };
  const [hot, setHot] = useState<string | null>(null);
  const near = hot ? new Set(edges.flatMap((e) => (e.from === hot || e.to === hot ? [e.from, e.to] : []))) : null;
  const touches = (e: ModuleEdge) => hot !== null && (e.from === hot || e.to === hot);
  return (
    <svg
      className={`graph${hot ? " graph--focus" : ""}`}
      viewBox={`0 0 ${W} ${H}`}
      style={{ minWidth: Math.round(W * 0.66) }}
      role="group"
      aria-label="Module dependency map"
    >
      <defs>
        <filter id="graph-glow" x="-20%" y="-20%" width="140%" height="140%">
          <feGaussianBlur stdDeviation="3" result="b" />
          <feMerge>
            <feMergeNode in="b" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>
      {edges.map((e, i) => (
        <path
          key={`${e.from}-${e.to}`}
          id={`edge-${i}`}
          d={path(e)}
          pathLength={1}
          className={`graph__edge${e.violation ? " graph__edge--bad" : ""}${touches(e) ? " is-hot" : ""}`}
          strokeWidth={1 + Math.sqrt(e.weight) / 2.2}
          style={{ "--i": Math.min(i, 24) } as CSSProperties}
        />
      ))}
      {edges
        .filter((e) => e.violation)
        .map((e) => (
          <path
            key={`flow-${e.from}-${e.to}`}
            d={path(e)}
            pathLength={1}
            className="graph__flow"
            strokeWidth={2.5}
            filter="url(#graph-glow)"
          />
        ))}
      <g className="graph__sparks" aria-hidden="true">
        {edges.flatMap((e, i) => {
          const count = Math.min(4, Math.max(1, Math.round(Math.sqrt(e.weight) / 1.6)));
          const dur = 2.6 + (i % 5) * 0.35;
          return Array.from({ length: count }, (_, k) => (
            <circle
              key={`${i}-${k}`}
              r={e.violation ? 2.6 : 2}
              className={`graph__spark${e.violation ? " graph__spark--bad" : ""}${touches(e) ? " is-hot" : ""}`}
            >
              <animateMotion dur={`${dur}s`} repeatCount="indefinite" begin={`${-(dur * k) / count - (i % 7) * 0.4}s`}>
                <mpath href={`#edge-${i}`} />
              </animateMotion>
            </circle>
          ));
        })}
      </g>
      {nodes.map(({ name }, i) => {
        const p = pos(name);
        const sub = p.sub;
        const label = name.length > 22 ? `…${name.slice(-21)}` : name;
        const cls = [
          "graph__node",
          bad.has(name) && "graph__node--bad",
          hot === name && "is-hot",
          near && near.has(name) && hot !== name && "is-near",
        ]
          .filter(Boolean)
          .join(" ");
        return (
          <g
            key={name}
            className={cls}
            style={{ "--i": Math.min(i, 24) } as CSSProperties}
            onPointerEnter={() => setHot(name)}
            onPointerLeave={() => setHot((h) => (h === name ? null : h))}
            role="button"
            tabIndex={0}
            aria-label={`Highlight connections for ${name}${sub ? `, ${sub}` : ""}`}
            aria-pressed={hot === name}
            onFocus={() => setHot(name)}
            onBlur={() => setHot((h) => h === name ? null : h)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setHot((h) => h === name ? null : name); }
              if (e.key === "Escape") setHot(null);
            }}
          >
            <title>{name}</title>
            <rect x={p.x} y={p.y} width={NW} height={NH} rx={4} />
            <text x={p.x + 12} y={p.y + (sub ? 22 : 31)}>{label}</text>
            {sub && (
              <text x={p.x + 12} y={p.y + 39} className="graph__sub">
                {sub}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}
