import type { CSSProperties, ReactNode } from "react";
import type { Condition, JobStatus, LedgerEvent, Meter as MeterData, Tone } from "@/lib/types";
import { Decode } from "./Decode";
import { ListFrame, type FacetSpec } from "./ListFrame";
import { Odometer, Rise } from "./motion";

export const fmt = (n: number) => n.toLocaleString("en-US");

// One mapping from job lifecycle to tone, shared by every screen
// (same contract as ui.job_tone in the legacy macros).
export function jobTone(status: JobStatus): Tone {
  switch (status) {
    case "completed":
      return "ok";
    case "failed":
    case "error":
      return "bad";
    case "running":
    case "retrying":
      return "info";
    default:
      return "warn";
  }
}

export function Chip({ tone, children, dot = true }: { tone: Tone; children: ReactNode; dot?: boolean }) {
  return (
    <span className={`chip chip--${tone}`}>
      {dot && <span className="chip__dot" aria-hidden="true" />}
      {children}
    </span>
  );
}

export function JobChip({ status }: { status: JobStatus }) {
  return <Chip tone={jobTone(status)}>{status.replace("_", " ")}</Chip>;
}

export function PageHead({
  eyebrow,
  title,
  lede,
  actions,
}: {
  eyebrow: string;
  title: string;
  lede?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="page-head">
      <div>
        <p className="eyebrow">
          <Decode text={eyebrow} />
        </p>
        <h1 className="page-head__title">
          <Rise text={title} />
        </h1>
        {lede && <p className="page-head__lede">{lede}</p>}
      </div>
      {actions && <div className="page-head__actions">{actions}</div>}
    </header>
  );
}

export function Panel({
  title,
  desc,
  actions,
  spine,
  flush,
  className = "",
  children,
  id,
}: {
  title?: ReactNode;
  desc?: ReactNode;
  actions?: ReactNode;
  spine?: Tone;
  flush?: boolean;
  className?: string;
  children: ReactNode;
  id?: string;
}) {
  return (
    <section className={`panel ${spine ? `panel--spine panel--${spine}` : ""} ${className}`} aria-labelledby={id}>
      {(title || actions) && (
        <div className="panel__head">
          <div>
            {title && (
              <h2 className="panel__title" id={id}>
                {title}
              </h2>
            )}
            {desc && <p className="panel__desc">{desc}</p>}
          </div>
          {actions && <div className="panel__actions">{actions}</div>}
        </div>
      )}
      <div className={flush ? "panel__body panel__body--flush" : "panel__body"}>{children}</div>
    </section>
  );
}

/** `action` is a live control (e.g. a RunButton) for the condition's next step. */
export function Verdict({ condition, action }: { condition: Condition; action?: ReactNode }) {
  return (
    <section className={`verdict verdict--${condition.tone}`} aria-labelledby="verdict-title">
      <div className="verdict__main">
        <p className="eyebrow">
          <Decode text="Condition" delay={80} />
        </p>
        <h2 className="verdict__headline" id="verdict-title">
          <Rise text={condition.headline} delay={120} />
        </h2>
        <p className="verdict__detail">{condition.detail}</p>
        {action && (
          <div className="verdict__action">
            {action}
            {condition.action?.hint && <span className="action__hint">{condition.action.hint}</span>}
          </div>
        )}
      </div>
      <dl className="tiles">
        {condition.tiles.map((t) => (
          <div className={`tile tile--${t.tone}`} key={t.label}>
            <dt className="tile__label">{t.label}</dt>
            <dd className="tile__value num">
              <Odometer value={t.value} />
            </dd>
          </div>
        ))}
      </dl>
      <Pulse tone={condition.tone} />
    </section>
  );
}

// A vital-signs trace along the bottom of the verdict. The rhythm is the
// condition: a calm beat when memory is current, quicker when it is falling
// behind, racing when it is badly out of date, flat when nothing is indexed.
const BEAT: Record<Tone, number> = { ok: 220, info: 180, warn: 140, bad: 92, idle: 0 };

function ecg(period: number, width = 1200, base = 24): string {
  if (!period) return `M 0 ${base} H ${width}`;
  let d = `M 0 ${base}`;
  for (let x = 0; x < width; x += period) {
    const u = period / 22;
    d +=
      ` H ${x + u * 6} q ${u} -4 ${u * 2} 0 H ${x + u * 10}` +
      ` l ${u * 0.6} 3 l ${u * 0.9} -21 l ${u} 27 l ${u * 0.7} -9` +
      ` H ${x + u * 15} q ${u * 1.6} -7 ${u * 3.2} 0 H ${x + period}`;
  }
  return d;
}

function Pulse({ tone }: { tone: Tone }) {
  const d = ecg(BEAT[tone]);
  const id = `pulse-${tone}`;
  return (
    <svg className="pulse" viewBox="0 0 1200 40" preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <linearGradient id={`${id}-g`}>
          <stop offset="0" stopColor="#000" />
          <stop offset="0.85" stopColor="#fff" />
          <stop offset="1" stopColor="#000" />
        </linearGradient>
        <mask id={`${id}-m`} maskUnits="userSpaceOnUse" x="0" y="0" width="1200" height="40">
          <rect className="pulse__sweep" x="-360" y="0" width="360" height="40" fill={`url(#${id}-g)`} />
        </mask>
      </defs>
      <path className="pulse__base" d={d} pathLength={1} />
      <path className="pulse__trace" d={d} mask={`url(#${id}-m)`} />
    </svg>
  );
}

// Twenty discrete cells instead of a smooth bar: the reader counts the missing
// bricks. Slots are allocated by largest remainder, and any non-zero part
// keeps at least one cell so a small gap never rounds away to nothing.
export function Meter({ meter, slots = 20 }: { meter: MeterData; slots?: number }) {
  const total = meter.parts.reduce((s, p) => s + p.count, 0) || 1;
  const raw = meter.parts.map((p) => (p.count / total) * slots);
  const alloc = raw.map((r, i) => (meter.parts[i].count > 0 ? Math.max(1, Math.floor(r)) : 0));
  let left = slots - alloc.reduce((s, a) => s + a, 0);
  const order = raw.map((r, i) => [r - Math.floor(r), i] as const).sort((a, b) => b[0] - a[0]);
  for (let k = 0; left > 0 && k < order.length * 2; k++) {
    const i = order[k % order.length][1];
    if (meter.parts[i].count > 0) {
      alloc[i]++;
      left--;
    }
  }
  while (left < 0) {
    const i = alloc.indexOf(Math.max(...alloc));
    alloc[i]--;
    left++;
  }
  const cells = meter.parts.flatMap((p, i) => Array.from({ length: alloc[i] }, () => p.tone));
  const aria = `${meter.label}: ${meter.parts.map((p) => p.text).join(", ")}`;
  return (
    <figure className="meter">
      <figcaption className="meter__head">
        <span className="meter__label">{meter.label}</span>
        <span className="meter__value num">{meter.value}</span>
      </figcaption>
      <div className="meter__cells" role="img" aria-label={aria}>
        {cells.map((tone, i) => (
          <span key={i} className={`meter__cell meter__cell--${tone}`} style={{ "--i": i } as CSSProperties} />
        ))}
      </div>
      <ul className="meter__key">
        {meter.parts.map((p) => (
          <li key={p.text}>
            <span className={`meter__swatch meter__cell--${p.tone}`} aria-hidden="true" />
            {p.text}
          </li>
        ))}
      </ul>
    </figure>
  );
}

const TONE_WORD: Record<string, string> = { bad: "Failed", warn: "Worth checking", ok: "Fine", info: "Info", idle: "Idle" };

/** `paged` adds search, a filter by outcome and a pager for the full log. */
export function Ledger({ events, limit, compact, paged }: { events: LedgerEvent[]; limit?: number; compact?: boolean; paged?: boolean }) {
  const rows = limit ? events.slice(0, limit) : events;
  const items = rows.map((e, i) => (
    <li className="ledger__row" key={`${e.at}-${i}`} style={{ "--i": Math.min(i, 12) } as CSSProperties}>
      <time className="ledger__time num">{compact ? e.at.slice(0, 5) : e.at}</time>
      <span className={`ledger__dot tone-${e.tone}`} aria-label={e.tone} role="img" />
      <span className="ledger__source">{e.source}</span>
      <span className="ledger__text">{e.text}</span>
      {e.ref && <span className="ledger__ref num">{e.ref}</span>}
    </li>
  ));
  const cls = compact ? "ledger ledger--compact" : "ledger";
  if (paged) {
    return (
      <ListFrame
        rows={items}
        listClass={cls}
        meta={rows.map((e) => ({ q: `${e.source} ${e.text} ${e.ref ?? ""} ${e.at}`.toLowerCase(), f: e.tone }))}
        facet={{ label: "Outcome", values: ["bad", "warn", "ok", "info", "idle"].map((v) => ({ value: v, label: TONE_WORD[v] })) }}
        noun={["event", "events"]}
        pageSize={50}
      />
    );
  }
  return <ol className={cls}>{items}</ol>;
}

export function Stat({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="stat">
      <span className="stat__label">{label}</span>
      <span className="stat__value num">
        <Odometer value={value} />
      </span>
      {note && <span className="stat__note">{note}</span>}
    </div>
  );
}

/** An empty screen says what would fill it and, when it can, offers the control that does. */
export function EmptyState({ title, body, action }: { title: string; body: string; action?: ReactNode }) {
  return (
    <div className="empty">
      <p className="empty__title">{title}</p>
      <p className="empty__body">{body}</p>
      {action && <div className="empty__action">{action}</div>}
    </div>
  );
}

export interface Column<T> {
  head: ReactNode;
  cell: (row: T) => ReactNode;
  align?: "right";
}

export interface TableFilter<T> {
  /** Text the search box matches (row title, ids, owner…). */
  search?: (row: T) => string;
  /** One value per row to filter by, e.g. its status. */
  facet?: { label: string; of: (row: T) => string | undefined; values?: FacetSpec["values"] };
  /** Singular and plural, for "Search 87 runs". */
  noun?: [string, string];
  pageSize?: number;
}

// Rows enter in sequence (capped at 12 so long tables never wait). Long or
// filterable tables get a search box, a filter and a pager (see ListFrame).
export function Table<T>({
  rows,
  columns,
  rowKey,
  caption,
  filter,
}: {
  rows: T[];
  columns: Column<T>[];
  rowKey: (row: T) => string;
  caption: string;
  filter?: TableFilter<T>;
}) {
  const head = (
    <tr>
      {columns.map((c, i) => (
        <th key={i} className={c.align === "right" ? "r" : undefined} scope="col">
          {c.head}
        </th>
      ))}
    </tr>
  );
  const body = rows.map((r, i) => (
    <tr key={rowKey(r)} style={{ "--i": Math.min(i, 12) } as CSSProperties}>
      {columns.map((c, j) => (
        <td key={j} className={c.align === "right" ? "r num" : undefined}>
          {c.cell(r)}
        </td>
      ))}
    </tr>
  ));
  if (filter || rows.length > 20) {
    return (
      <ListFrame
        rows={body}
        head={head}
        caption={caption}
        meta={rows.map((r) => ({ q: (filter?.search?.(r) ?? "").toLowerCase(), f: filter?.facet?.of(r) }))}
        facet={filter?.facet ? { label: filter.facet.label, values: filter.facet.values } : undefined}
        noun={filter?.noun}
        pageSize={filter?.pageSize}
        searchable={Boolean(filter?.search)}
      />
    );
  }
  return (
    <div className="table-wrap">
      <table className="table">
        <caption className="sr-only">{caption}</caption>
        <thead>{head}</thead>
        <tbody>{body}</tbody>
      </table>
    </div>
  );
}
