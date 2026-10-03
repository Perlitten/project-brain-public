// The brand sprite, ported 1:1 from apps/api/templates/base.html.
// 24x24 grid, stroke 1.75, round caps and joins, never filled.
// layers, zap, code, key and flag reuse Feather Icons geometry
// (MIT, (c) 2013-2023 Cole Bemis) — see THIRD_PARTY_NOTICES.md.
const paths = {
  layers: <><polygon points="12 2 2 7 12 12 22 7 12 2" /><polyline points="2 17 12 22 22 17" /><polyline points="2 12 12 17 22 12" /></>,
  database: <><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5" /><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3" /></>,
  graph: <><circle cx="5" cy="6" r="2.4" /><circle cx="19" cy="6" r="2.4" /><circle cx="12" cy="18" r="2.4" /><path d="M6.8 7.3 10.6 16M17.2 7.3 13.4 16M7 6h10" /></>,
  file: <><path d="M14 3v4a1 1 0 0 0 1 1h4" /><path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2z" /></>,
  zap: <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />,
  search: <><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /></>,
  chip: <><rect x="6" y="6" width="12" height="12" rx="2" /><path d="M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3" /></>,
  code: <><polyline points="16 18 22 12 16 6" /><polyline points="8 6 2 12 8 18" /></>,
  automation: <><circle cx="6" cy="6" r="2.4" /><circle cx="18" cy="18" r="2.4" /><path d="M8.4 6H14a4 4 0 0 1 4 4v5.5" /></>,
  vectors: <><circle cx="12" cy="12" r="3" /><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1" /></>,
  key: <path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0 3 3L22 7l-3-3m-3.5 3.5L19 4" />,
  flag: <><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z" /><line x1="4" y1="22" x2="4" y2="15" /></>,
  // Drawn for the web app in the same geometry.
  trend: <><polyline points="3 17 9 11 13 15 21 7" /><polyline points="15 7 21 7 21 13" /></>,
  menu: <path d="M4 7h16M4 12h16M4 17h16" />,
  sidebar: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16" /></>,
  close: <path d="M6 6l12 12M18 6 6 18" />,
  arrow: <path d="M5 12h14M13 6l6 6-6 6" />,
  check: <polyline points="4 12.5 9.5 18 20 6" />,
  activity: <polyline points="2 12 6 12 9 4 15 20 18 12 22 12" />,
  play: <polygon points="7 4 19 12 7 20 7 4" />,
  refresh: <><path d="M20 11a8 8 0 0 0-14.8-4.2L3 9" /><path d="M3 3v6h6" /><path d="M4 13a8 8 0 0 0 14.8 4.2L21 15" /><path d="M21 21v-6h-6" /></>,
  plus: <path d="M12 5v14M5 12h14" />,
  gear: <><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" /></>,
  copy: <><rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" /></>,
} as const;

export type IconName = keyof typeof paths;

export function Icon({ name, size = 17, className }: { name: IconName; size?: number; className?: string }) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {paths[name]}
    </svg>
  );
}

// The mark: a brain built from lines of code. Seven rows of "tokens" grow out
// of a central fissure, left hemisphere right-aligned, right one left-aligned,
// so the outline reads as a brain seen from above and the fill reads as code.
// Teal tokens are what Brain remembers; on hover a signal runs through them.
// Each row: [left segments from the fissure outward], [right segments]; a
// segment is [length, teal].
type Seg = [number, 0 | 1];
const MARK_ROWS: [Seg[], Seg[]][] = [
  [[[16, 0]], [[10, 0], [4, 1]]],
  [[[8, 1], [13, 0]], [[23, 0]]],
  [[[27, 0]], [[14, 0], [11, 0]]],
  [[[12, 0], [14, 0]], [[6, 1], [20, 0]]],
  [[[18, 1], [7, 0]], [[27, 0]]],
  [[[23, 0]], [[9, 0], [12, 1]]],
  [[[5, 0], [8, 0]], [[15, 0]]],
];
const MID = 32;
const GAP = 2;

export const MARK_TOKENS = (() => {
  const out: { x: number; y: number; w: number; teal: boolean; side: "l" | "r"; row: number; k: number }[] = [];
  let signal = 0;
  MARK_ROWS.forEach(([left, right], row) => {
    const y = 4 + row * 8;
    let edge = MID - GAP / 2;
    left.forEach(([w, t], k) => {
      out.push({ x: edge - w, y, w, teal: !!t, side: "l", row, k: t ? signal++ : k });
      edge -= w + GAP;
    });
    edge = MID + GAP / 2;
    right.forEach(([w, t], k) => {
      out.push({ x: edge, y, w, teal: !!t, side: "r", row, k: t ? signal++ : k });
      edge += w + GAP;
    });
  });
  return out;
})();

export function BrandMark({ size = 24, className = "" }: { size?: number; className?: string }) {
  return (
    <svg className={`mark ${className}`} width={size} height={size} viewBox="0 0 64 64" aria-hidden="true">
      {MARK_TOKENS.map((t, i) => (
        <rect
          key={i}
          className={`mark__tok mark__tok--${t.side}${t.teal ? " mark__tok--teal" : ""}`}
          x={t.x}
          y={t.y}
          width={t.w}
          height={6}
          rx={1}
          style={{ "--row": t.row, "--k": t.k } as React.CSSProperties}
        />
      ))}
    </svg>
  );
}
