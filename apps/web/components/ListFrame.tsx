"use client";
// One frame for every long list: search, a filter by status (with counts),
// a fixed page size and a pager — so no screen scrolls forever. The rows are
// rendered on the server; this only decides which of them to show.

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Icon } from "./Icon";
import "./list-frame.css";

export interface ListMeta {
  /** Lower-cased text the search box matches against. */
  q: string;
  /** Facet value for the filter row (e.g. a status). */
  f?: string;
}

export interface FacetSpec {
  /** What the filter is about, e.g. "Result". */
  label: string;
  /** Preferred order and display names; values not listed follow by count. */
  values?: { value: string; label?: string }[];
}

const SIZES = [20, 50, 100];

export function ListFrame({
  rows,
  meta,
  head,
  caption,
  listClass,
  facet,
  noun = ["item", "items"],
  pageSize = 20,
  searchable = true,
  placeholder,
  listTag = "ol",
}: {
  rows: ReactNode[];
  meta: ListMeta[];
  /** A table header row; when set the rows are table rows. */
  head?: ReactNode;
  caption?: string;
  /** For non-table lists: class of the <ol> that holds the rows. */
  listClass?: string;
  facet?: FacetSpec;
  noun?: [string, string];
  pageSize?: number;
  searchable?: boolean;
  placeholder?: string;
  /** "div" when the rows are not <li> (e.g. cards). */
  listTag?: "ol" | "div";
}) {
  const [q, setQ] = useState("");
  const [f, setF] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const [size, setSize] = useState(pageSize);
  const [touched, setTouched] = useState(false);
  const top = useRef<HTMLDivElement>(null);

  const facets = useMemo(() => {
    if (!facet) return [];
    const counts = new Map<string, number>();
    for (const m of meta) if (m.f) counts.set(m.f, (counts.get(m.f) ?? 0) + 1);
    const known = (facet.values ?? []).filter((v) => counts.has(v.value)).map((v) => ({ value: v.value, label: v.label ?? v.value, n: counts.get(v.value)! }));
    const rest = [...counts.entries()]
      .filter(([v]) => !known.some((k) => k.value === v))
      .sort((a, b) => b[1] - a[1])
      .map(([value, n]) => ({ value, label: value, n }));
    return [...known, ...rest];
  }, [facet, meta]);

  const needle = q.trim().toLowerCase();
  const hits = useMemo(() => {
    const words = needle.split(/\s+/).filter(Boolean);
    const out: number[] = [];
    meta.forEach((m, i) => {
      if (f && m.f !== f) return;
      if (words.length && !words.every((w) => m.q.includes(w))) return;
      out.push(i);
    });
    return out;
  }, [meta, needle, f]);

  const pages = Math.max(1, Math.ceil(hits.length / size));
  const at = Math.min(page, pages - 1);
  const shown = hits.slice(at * size, at * size + size);
  const filtered = Boolean(needle || f);

  // A different filter starts again from the first page.
  useEffect(() => setPage(0), [needle, f, size]);

  const go = (p: number) => {
    setTouched(true);
    setPage(Math.max(0, Math.min(pages - 1, p)));
    const el = top.current;
    if (el && el.getBoundingClientRect().top < 0) el.scrollIntoView({ block: "start", behavior: "smooth" });
  };
  const clear = () => {
    setQ("");
    setF(null);
  };

  const showBar = rows.length > 8 || facets.length > 1;
  const range = hits.length ? `${(at * size + 1).toLocaleString("en")}–${(at * size + shown.length).toLocaleString("en")} of ${hits.length.toLocaleString("en")}` : `0 of ${rows.length}`;

  const body = shown.map((i) => rows[i]);
  const empty = (
    <div className="lf__empty">
      <p>
        No {noun[1]} match{needle ? ` “${q.trim()}”` : ""}
        {f ? ` with ${facet?.label.toLowerCase() ?? "filter"} “${facets.find((x) => x.value === f)?.label ?? f}”` : ""}.
      </p>
      <button type="button" className="btn btn--neutral btn--sm" onClick={clear}>
        Clear filters
      </button>
    </div>
  );

  return (
    <div className={`lf${touched ? " lf--touched" : ""}${showBar ? " lf--bar" : ""}`} ref={top}>
      {showBar && (
        <div className="lf__bar" role="search">
          {searchable && (
            <label className="lf__search">
              <Icon name="search" size={15} />
              <input
                className="input"
                type="search"
                value={q}
                onChange={(e) => {
                  setTouched(true);
                  setQ(e.target.value);
                }}
                placeholder={placeholder ?? `Search ${rows.length.toLocaleString("en")} ${noun[1]}`}
                aria-label={`Search ${noun[1]}`}
              />
            </label>
          )}
          {facets.length > 1 && (
            <div className="seg lf__facets" role="group" aria-label={`Filter by ${facet?.label.toLowerCase()}`}>
              <button type="button" aria-pressed={!f} onClick={() => (setTouched(true), setF(null))}>
                All <span className="lf__n">{meta.length}</span>
              </button>
              {facets.map((x) => (
                <button key={x.value} type="button" aria-pressed={f === x.value} onClick={() => (setTouched(true), setF(f === x.value ? null : x.value))}>
                  {x.label} <span className="lf__n">{x.n}</span>
                </button>
              ))}
            </div>
          )}
          <span className="lf__range num" aria-live="polite">
            {range}
            {filtered && (
              <button type="button" className="lf__clear" onClick={clear}>
                clear
              </button>
            )}
          </span>
        </div>
      )}

      {head ? (
        hits.length ? (
          <div className="table-wrap lf__table">
            <table className="table">
              {caption && <caption className="sr-only">{caption}</caption>}
              <thead>{head}</thead>
              <tbody>{body}</tbody>
            </table>
          </div>
        ) : (
          empty
        )
      ) : hits.length ? (
        listTag === "div" ? (
          <div className={listClass}>{body}</div>
        ) : (
          <ol className={listClass}>{body}</ol>
        )
      ) : (
        empty
      )}

      {hits.length > SIZES[0] && (
        <nav className="lf__pager" aria-label="Pages">
          <button type="button" className="btn btn--neutral btn--sm" onClick={() => go(at - 1)} disabled={at === 0} aria-label="Previous page">
            <span className="lf__flip">
              <Icon name="arrow" size={14} />
            </span>
            Previous
          </button>
          <span className="lf__pages">
            {pageList(at, pages).map((p, i) =>
              p < 0 ? (
                <span key={`gap${i}`} className="lf__gap" aria-hidden="true">
                  …
                </span>
              ) : (
                <button key={p} type="button" className="lf__page num" aria-current={p === at ? "page" : undefined} onClick={() => go(p)}>
                  {p + 1}
                </button>
              ),
            )}
          </span>
          <button type="button" className="btn btn--neutral btn--sm" onClick={() => go(at + 1)} disabled={at >= pages - 1} aria-label="Next page">
            Next
            <Icon name="arrow" size={14} />
          </button>
          <label className="lf__size">
            <span>Per page</span>
            <select className="select" value={size} onChange={(e) => (setTouched(true), setSize(Number(e.target.value)))}>
              {SIZES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
        </nav>
      )}
    </div>
  );
}

/** 1 … 4 5 6 … 12 — always the ends, the current page and its neighbours. */
function pageList(at: number, pages: number): number[] {
  if (pages <= 7) return Array.from({ length: pages }, (_, i) => i);
  const out = new Set([0, pages - 1, at - 1, at, at + 1].filter((p) => p >= 0 && p < pages));
  const sorted = [...out].sort((a, b) => a - b);
  const res: number[] = [];
  sorted.forEach((p, i) => {
    if (i && p - sorted[i - 1] > 1) res.push(-1);
    res.push(p);
  });
  return res;
}
