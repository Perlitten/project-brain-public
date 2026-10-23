import type { ModuleEdge } from "./types";

export type GraphOptions = {
  minWeight: number;
  includeTests: boolean;
  query: string;
  focus: string | null;
  hops: 1 | 2;
  expanded: ReadonlySet<string>;
};
export type GraphNode = {
  id: string;
  area: string;
  label: string;
  module: boolean;
  count: number;
  dimmed: boolean;
  matched: boolean;
};

export const areaOf = (name: string) => name.split("/")[0];
const areaOrder = ["apps", "brain", "eval", "scripts", "tests"];

export function neighbours(edges: ModuleEdge[], root: string, hops: 1 | 2): Set<string> {
  const found = new Set([root]);
  let frontier = new Set([root]);
  for (let hop = 0; hop < hops; hop++) {
    const next = new Set<string>();
    for (const edge of edges) {
      if (frontier.has(edge.from) && !found.has(edge.to)) next.add(edge.to);
      if (frontier.has(edge.to) && !found.has(edge.from)) next.add(edge.from);
    }
    next.forEach((name) => found.add(name));
    frontier = next;
  }
  return found;
}

export function graphView(input: ModuleEdge[], options: GraphOptions) {
  const edges = input.filter((edge) => Number.isFinite(edge.weight) && edge.weight >= options.minWeight &&
    (options.includeTests || (areaOf(edge.from) !== "tests" && areaOf(edge.to) !== "tests")));
  const names = [...new Set(edges.flatMap((edge) => [edge.from, edge.to]))].sort();
  const query = options.query.trim().toLowerCase();
  const matches = new Set(names.filter((name) => name.toLowerCase().includes(query)));
  const focus = options.focus && names.includes(options.focus) ? options.focus : null;
  const context = focus ? neighbours(edges, focus, options.hops) : query ?
    new Set([...matches].flatMap((name) => [...neighbours(edges, name, 1)])) : new Set(names);
  const areas = [...new Set(names.map(areaOf))].sort((a, b) => {
    const rank = (area: string) => areaOrder.includes(area) ? areaOrder.indexOf(area) : areaOrder.length;
    return rank(a) - rank(b) || a.localeCompare(b);
  });
  const nodes: GraphNode[] = [];
  const represented = new Map<string, string>();
  for (const area of areas) {
    const members = names.filter((name) => areaOf(name) === area);
    const active = members.filter((name) => context.has(name));
    const expanded = focus ? active.length > 0 : query ? members.some((name) => matches.has(name)) : options.expanded.has(area);
    if (expanded) {
      for (const name of active) {
        represented.set(name, name);
        nodes.push({ id: name, area, label: name, module: true, count: 1, dimmed: false, matched: Boolean(query && matches.has(name)) });
      }
    } else {
      const id = `area:${area}`;
      members.forEach((name) => represented.set(name, id));
      nodes.push({ id, area, label: area, module: false, count: members.length, dimmed: active.length === 0, matched: false });
    }
  }
  const connections = new Map<string, ModuleEdge>();
  for (const edge of edges) {
    if (!context.has(edge.from) || !context.has(edge.to)) continue;
    if (query && !focus && !matches.has(edge.from) && !matches.has(edge.to)) continue;
    const from = represented.get(edge.from), to = represented.get(edge.to);
    if (!from || !to || from === to) continue;
    const key = JSON.stringify([from, to]);
    const previous = connections.get(key);
    connections.set(key, { from, to, weight: edge.weight + (previous?.weight ?? 0), violation: edge.violation || Boolean(previous?.violation) });
  }
  return { nodes, edges: [...connections.values()], areas, focus, matches: matches.size, modules: names.length, connections: edges.length };
}

export function graphLayout(nodes: GraphNode[], width: number) {
  const w = Math.max(240, width);
  const columns = w < 560 ? 2 : w < 900 ? 3 : 5;
  const gap = 16, nodeHeight = 48, pitch = 64;
  const nodeWidth = (w - gap * (columns + 1)) / columns;
  const areas = [...new Set(nodes.map((node) => node.area))];
  const positions = new Map<string, { x: number; y: number }>();
  let y = gap;
  for (let index = 0; index < areas.length; index += columns) {
    const row = areas.slice(index, index + columns);
    let rows = 1;
    row.forEach((area, column) => {
      const members = nodes.filter((node) => node.area === area);
      rows = Math.max(rows, members.length);
      members.forEach((node, slot) => positions.set(node.id, { x: gap + column * (nodeWidth + gap), y: y + slot * pitch }));
    });
    y += rows * pitch + gap;
  }
  return { positions, width: w, height: Math.max(144, y), nodeWidth, nodeHeight };
}

/**
 * The map's module for a finding's module or file path: its first two
 * directories, the same grouping the API uses for the module graph. A trailing
 * file name is dropped; null when there is nothing to point at.
 */
export function moduleOf(name: string): string | null {
  const parts = name.trim().replace(/\\/g, "/").split("/").filter(Boolean);
  if (parts.length === 0 || name.trim() === "—") return null;
  const directories = /\.[a-z0-9]+$/i.test(parts[parts.length - 1]) ? parts.slice(0, -1) : parts;
  return directories.length ? directories.slice(0, 2).join("/") : null;
}

/**
 * Controls for a map opened on one module (/graph?module=…): focused when the
 * module is on the map, otherwise searched for. Every edge weight is kept so a
 * lightly connected module is not filtered out before it can be shown.
 */
export function startingView(edges: ModuleEdge[], module?: string | null): Pick<GraphOptions, "minWeight" | "includeTests" | "query" | "focus"> {
  const name = module?.trim();
  if (!name) return { minWeight: 2, includeTests: false, query: "", focus: null };
  const known = edges.some((edge) => edge.from === name || edge.to === name);
  return { minWeight: 1, includeTests: areaOf(name) === "tests", query: known ? "" : name, focus: known ? name : null };
}
