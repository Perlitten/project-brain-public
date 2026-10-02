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
  close: <path d="M6 6l12 12M18 6 6 18" />,
  arrow: <path d="M5 12h14M13 6l6 6-6 6" />,
  check: <polyline points="4 12.5 9.5 18 20 6" />,
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

export function BrandMark({ size = 24 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" aria-hidden="true">
      <rect x="14" y="12" width="9" height="40" fill="var(--text-1)" />
      <rect x="23" y="16" width="15" height="8" fill="var(--text-1)" />
      <rect x="23" y="28" width="22" height="8" fill="var(--teal-fg)" />
      <rect x="23" y="40" width="15" height="8" fill="var(--text-1)" />
      <path d="M45 24 L54 32 L45 40 Z" fill="var(--teal-fg)" />
    </svg>
  );
}
