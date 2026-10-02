import type { CSSProperties } from "react";
import type { ModuleEdge } from "@/lib/types";

// Fixed three-lane layout: entry points on the left, core logic in the middle,
// storage on the right. Edges draw from source to target; a rule violation
// keeps flowing so the eye lands on it first.
const lanes: Record<string, [number, number, string]> = {
  "apps/api": [0, 0, "web API"],
  "apps/mcp_server": [0, 1, "agent tools"],
  "brain/workers": [0, 2, "background jobs"],
  "brain/insights": [0, 3, "findings"],
  "brain/search": [1, 0, "code search"],
  "brain/context": [1, 1, "agent briefings"],
  "brain/retrieval": [1, 2, "retrieval"],
  "brain/indexers": [1, 3, "code reading"],
  "brain/graph": [1, 4, "structure"],
  "brain/database": [2, 1, "storage"],
  "brain/embeddings": [2, 3, "meaning vectors"],
};

const W = 900;
const NW = 170;
const NH = 52;
const colX = [70, 395, 720];
const rowY = (r: number) => 30 + r * 86;

type Node = { name: string; col: number; row: number; sub: string };

// Known modules keep their lane; any other module the API reports is placed by
// its role (only uses others → left, only used → right, both → middle) on the
// next free row of that lane. Only modules that appear in an edge are drawn.
function layout(edges: ModuleEdge[]): Node[] {
  const names = [...new Set(edges.flatMap((e) => [e.from, e.to]))];
  const used = [new Set<number>(), new Set<number>(), new Set<number>()];
  const nodes: Node[] = [];
  for (const name of names) {
    const known = lanes[name];
    if (known) {
      nodes.push({ name, col: known[0], row: known[1], sub: known[2] });
      used[known[0]].add(known[1]);
    }
  }
  for (const name of names) {
    if (lanes[name]) continue;
    const out = edges.some((e) => e.from === name);
    const inc = edges.some((e) => e.to === name);
    const col = out && !inc ? 0 : inc && !out ? 2 : 1;
    let row = 0;
    while (used[col].has(row)) row++;
    used[col].add(row);
    nodes.push({ name, col, row, sub: "" });
  }
  return nodes;
}

export function CodeGraph({ edges }: { edges: ModuleEdge[] }) {
  const nodes = layout(edges);
  const at = new Map(nodes.map((n) => [n.name, { x: colX[n.col], y: rowY(n.row) }]));
  const pos = (name: string) => at.get(name) ?? { x: colX[1], y: rowY(0) };
  const H = Math.max(470, Math.max(0, ...nodes.map((n) => n.row)) * 86 + 30 + NH + 40);
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
    const x1 = a.x + NW;
    const x2 = b.x;
    const bend = (x2 - x1) / 2;
    return `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`;
  };
  return (
    <svg className="graph" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Module dependency map">
      {edges.map((e, i) => (
        <path
          key={`${e.from}-${e.to}`}
          d={path(e)}
          pathLength={1}
          className={`graph__edge${e.violation ? " graph__edge--bad" : ""}`}
          strokeWidth={1 + Math.sqrt(e.weight) / 2.2}
          style={{ "--i": i } as CSSProperties}
        />
      ))}
      {edges
        .filter((e) => e.violation)
        .map((e) => (
          <path key={`flow-${e.from}-${e.to}`} d={path(e)} pathLength={1} className="graph__flow" strokeWidth={2.5} />
        ))}
      {nodes.map(({ name, sub }, i) => {
        const p = pos(name);
        const label = name.length > 22 ? `…${name.slice(-21)}` : name;
        return (
          <g key={name} className={`graph__node${bad.has(name) ? " graph__node--bad" : ""}`} style={{ "--i": i } as CSSProperties}>
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
