"use client";

import { useEffect, useRef, useState } from "react";
import type { ModuleEdge } from "@/lib/types";
import { graphLayout, graphView } from "@/lib/code-graph";
import { EmptyState } from "./ui";

export function CodeGraph({ edges }: { edges: ModuleEdge[] }) {
  const container = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(960);
  const [minWeight, setMinWeight] = useState(2);
  const [includeTests, setIncludeTests] = useState(false);
  const [query, setQuery] = useState("");
  const [focus, setFocus] = useState<string | null>(null);
  const [hops, setHops] = useState<1 | 2>(1);
  const [expanded, setExpanded] = useState(new Set<string>());
  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const view = graphView(edges, { minWeight, includeTests, query, focus, hops, expanded });
  const layout = graphLayout(view.nodes, width);
  const maxWeight = Math.max(2, ...edges.map((edge) => edge.weight));
  const toggleArea = (area: string) => {
    setFocus(null);
    setQuery("");
    setExpanded((previous) => {
      const next = new Set(previous);
      if (next.has(area)) next.delete(area); else next.add(area);
      return next;
    });
  };
  const path = (edge: ModuleEdge) => {
    const a = layout.positions.get(edge.from)!, b = layout.positions.get(edge.to)!;
    const forwards = a.x < b.x;
    const x1 = a.x + (forwards ? layout.nodeWidth : 0), x2 = b.x + (forwards ? 0 : layout.nodeWidth);
    const y1 = a.y + layout.nodeHeight / 2, y2 = b.y + layout.nodeHeight / 2;
    const bend = a.x === b.x ? -24 : (x2 - x1) / 2;
    return `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`;
  };
  return (
    <div className="code-map" ref={container}>
      <div className="code-map__controls">
        <label className="code-map__search">Search modules
          <input type="search" value={query} placeholder="For example, brain/search" onChange={(event) => { setQuery(event.target.value); setFocus(null); }} />
        </label>
        <label>Minimum edge weight: <output>{minWeight}</output>
          <input type="range" aria-label="Minimum edge weight" min={1} max={maxWeight} value={minWeight} onChange={(event) => { setMinWeight(Number(event.target.value)); setFocus(null); }} />
        </label>
        <label className="code-map__check"><input type="checkbox" checked={includeTests} onChange={(event) => { setIncludeTests(event.target.checked); setFocus(null); }} /> Include tests</label>
        <label>Focus depth
          <select aria-label="Focus depth" value={hops} onChange={(event) => setHops(Number(event.target.value) as 1 | 2)}><option value={1}>1 hop</option><option value={2}>2 hops</option></select>
        </label>
      </div>
      <div className="code-map__summary" role="status">
        <span>{view.modules} modules · {view.connections} filtered connections · {view.edges.length} visible links</span>
        {view.focus && <button className="btn btn--ghost" onClick={() => setFocus(null)}>Clear focus: {view.focus}</button>}
        <button className="btn btn--ghost" onClick={() => { setExpanded(new Set()); setFocus(null); setQuery(""); }}>Collapse all</button>
      </div>
      <div className="code-map__areas" aria-label="Expand or collapse areas">
        {view.areas.map((area) => <button key={area} className="btn btn--ghost" aria-pressed={expanded.has(area)} onClick={() => toggleArea(area)}>{expanded.has(area) ? "Collapse" : "Expand"} {area}</button>)}
      </div>
      {view.modules === 0 || (query.trim() && view.matches === 0) ? <EmptyState title="No matching modules" body="Try a different search, a lower edge weight, or include tests." /> : (
        <svg className={`graph code-map__canvas${view.focus ? " graph--focus" : ""}`} viewBox={`0 0 ${layout.width} ${layout.height}`} role="group" aria-label="Module dependency map">
          {view.edges.map((edge) => <path key={`${edge.from}-${edge.to}`} d={path(edge)} className={`code-map__edge${edge.violation ? " code-map__edge--bad" : ""}`} strokeWidth={Math.min(4, 1 + Math.sqrt(edge.weight) / 6)}><title>{edge.from} → {edge.to}: {edge.weight} imports</title></path>)}
          {view.nodes.map((node) => {
            const position = layout.positions.get(node.id)!;
            const activate = () => node.module ? setFocus((previous) => previous === node.id ? null : node.id) : toggleArea(node.area);
            const hot = view.focus === node.id || node.matched;
            const limit = Math.max(9, Math.floor((layout.nodeWidth - 24) / 7));
            const label = node.label.length > limit ? `…${node.label.slice(-(limit - 1))}` : node.label;
            return <g key={node.id} className={`graph__node code-map__node${hot ? " is-hot" : !node.dimmed ? " is-near" : " is-dimmed"}${node.module ? "" : " code-map__area"}`} role="button" tabIndex={0}
              aria-label={node.module ? `Focus module ${node.id}` : `Expand area ${node.area}, ${node.count} modules`}
              aria-pressed={node.module ? view.focus === node.id : undefined}
              onClick={activate} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); activate(); } if (event.key === "Escape") setFocus(null); }}>
              <title>{node.label}{node.module ? "" : `: ${node.count} modules; click to expand`}</title>
              <rect x={position.x} y={position.y} width={layout.nodeWidth} height={layout.nodeHeight} rx={4} />
              <text x={position.x + 12} y={position.y + 20}>{label}</text>
              <text x={position.x + 12} y={position.y + 36} className="graph__sub">{node.module ? node.area : `${node.count} modules · expand`}</text>
            </g>;
          })}
        </svg>
      )}
      <p className="code-map__hint">Areas start collapsed. Expand an area, then click a module to focus on its neighbours. Other areas dim; Escape clears focus. Red links break an architecture rule.</p>
    </div>
  );
}
