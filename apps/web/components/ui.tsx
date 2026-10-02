import type { CSSProperties, ReactNode } from "react";
import type { Condition, JobStatus, LedgerEvent, Meter as MeterData, Tone } from "@/lib/types";
import { dataSource } from "@/lib/data";
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
  return <Chip tone={jobTone(status)}>{status}</Chip>;
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
        <p className="eyebrow">{eyebrow}</p>
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

// An action that cannot run must say why instead of pretending (component
// contract: a button that does nothing must not render as live).
export function ActionButton({
  label,
  hint,
  variant = "primary",
}: {
  label: string;
  hint?: string;
  variant?: "primary" | "neutral";
}) {
  const demo = dataSource === "demo";
  return (
    <span className="action">
      <button type="button" className={`btn btn--${variant}`} disabled={demo}>
        {label}
      </button>
      <span className="action__hint">{demo ? "Connect the API to run actions" : hint}</span>
    </span>
  );
}

export function Verdict({ condition }: { condition: Condition }) {
  return (
    <section className={`verdict verdict--${condition.tone}`} aria-labelledby="verdict-title">
      <div className="verdict__main">
        <p className="eyebrow">Condition</p>
        <h2 className="verdict__headline" id="verdict-title">
          <Rise text={condition.headline} delay={120} />
        </h2>
        <p className="verdict__detail">{condition.detail}</p>
        {condition.action && (
          <div className="verdict__action">
            <ActionButton label={condition.action.label} hint={condition.action.hint} />
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
    </section>
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

export function Ledger({ events, limit, compact }: { events: LedgerEvent[]; limit?: number; compact?: boolean }) {
  const rows = limit ? events.slice(0, limit) : events;
  return (
    <ol className={compact ? "ledger ledger--compact" : "ledger"}>
      {rows.map((e, i) => (
        <li className="ledger__row" key={`${e.at}-${i}`} style={{ "--i": Math.min(i, 12) } as CSSProperties}>
          <time className="ledger__time num">{compact ? e.at.slice(0, 5) : e.at}</time>
          <span className={`ledger__dot tone-${e.tone}`} aria-label={e.tone} role="img" />
          <span className="ledger__source">{e.source}</span>
          <span className="ledger__text">{e.text}</span>
          {e.ref && <span className="ledger__ref num">{e.ref}</span>}
        </li>
      ))}
    </ol>
  );
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

export function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <div className="empty">
      <p className="empty__title">{title}</p>
      <p className="empty__body">{body}</p>
    </div>
  );
}

export interface Column<T> {
  head: ReactNode;
  cell: (row: T) => ReactNode;
  align?: "right";
}

// Rows enter in sequence (capped at 12 so long tables never wait).
export function Table<T>({ rows, columns, rowKey, caption }: { rows: T[]; columns: Column<T>[]; rowKey: (row: T) => string; caption: string }) {
  return (
    <div className="table-wrap">
      <table className="table">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            {columns.map((c, i) => (
              <th key={i} className={c.align === "right" ? "r" : undefined} scope="col">
                {c.head}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={rowKey(r)} style={{ "--i": Math.min(i, 12) } as CSSProperties}>
              {columns.map((c, j) => (
                <td key={j} className={c.align === "right" ? "r num" : undefined}>
                  {c.cell(r)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
