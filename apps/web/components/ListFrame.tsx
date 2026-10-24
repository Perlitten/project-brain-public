"use client";
// One frame for every long list: search, a filter by status (with counts),
// a fixed page size and a pager — so no screen scrolls forever. The rows are
// rendered on the server; remote lists keep committed filters in the URL.

import { Suspense, useEffect, useMemo, useRef, useState, useTransition, type ReactNode } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Icon } from "./Icon";
import { LIST_SIZES, type ListPaging } from "@/lib/list-query";
export type { ListPaging } from "@/lib/list-query";
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

const SIZES: readonly number[] = LIST_SIZES;

type Props = {
  rows: ReactNode[];
  meta: ListMeta[];
  head?: ReactNode;
  caption?: string;
  listClass?: string;
  facet?: FacetSpec;
  noun?: [string, string];
  pageSize?: number;
  searchable?: boolean;
  placeholder?: string;
  listTag?: "ol" | "div";
  paging?: ListPaging;
};

export function ListFrame(props: Props) {
  return <Suspense fallback={<div aria-busy="true">Loading list…</div>}><ListBody {...props} /></Suspense>;
}

function ListBody({
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
  paging,
}: Props) {
  const params = useSearchParams();
  const pathname = usePathname();
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  // A debounced search can fire after another filter has already committed.
  const committedParams = useRef(params.toString());
  const pendingParams = useRef<string | null>(null);
  useEffect(() => {
    committedParams.current = params.toString();
    if (!pending) pendingParams.current = null;
  }, [pending, params]);
  const paramQ = params.get("q") ?? "";
  const [q, setQ] = useState(paramQ);
  const draftQuery = useRef<string | null>(null);
  const f = params.get("status") || null;
  // A remote list can only ask for the sizes the API serves; a local one also offers its own default.
  const baseSize = paging ? SIZES[0] : pageSize;
  const options = useMemo(() => [...new Set([baseSize, ...SIZES])].sort((a, b) => a - b), [baseSize]);
  // The chosen size is remembered per list, so switching tabs or coming back keeps it.
  const sizeKey = `lf:size:${pathname}:${noun[1]}`;
  const [saved, setSaved] = useState<number | null>(null);
  useEffect(() => {
    try {
      const v = Number(localStorage.getItem(sizeKey));
      setSaved(options.includes(v) ? v : null);
    } catch {
      setSaved(null);
    }
  }, [sizeKey, options]);
  const requestedSize = Number(params.get("page_size"));
  const size = options.includes(requestedSize) ? requestedSize : !paging && saved ? saved : paging?.size ?? pageSize;
  const requestedPage = Number(params.get("page"));
  const page = Math.max(0, Number.isSafeInteger(requestedPage) ? requestedPage - 1 : 0);
  const [touched, setTouched] = useState(false);
  const top = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // An older response must not overwrite text typed while it was in flight.
    if (draftQuery.current === null || draftQuery.current === paramQ) {
      setQ(paramQ);
      draftQuery.current = null;
    }
  }, [paramQ]);
  const navigate = (changes: Record<string, string | null>, push = false) => {
    const next = new URLSearchParams(pendingParams.current ?? committedParams.current);
    for (const [key, value] of Object.entries(changes)) {
      if (value === null || value === "") next.delete(key); else next.set(key, value);
    }
    const href = `${pathname}${next.size ? `?${next}` : ""}`;
    pendingParams.current = next.toString();
    setTouched(true);
    if (paging) startTransition(() => push ? router.push(href, { scroll: false }) : router.replace(href, { scroll: false }));
    else if (push) window.history.pushState(null, "", href);
    else window.history.replaceState(null, "", href);
  };
  useEffect(() => {
    // A remote list asks the server again for the remembered size, unless the link already names a page.
    if (paging && saved && saved !== paging.size && !params.get("page_size") && !params.get("page")) navigate({ page_size: String(saved) });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [saved]);
  const resize = (s: number) => {
    try {
      if (s === baseSize) localStorage.removeItem(sizeKey); else localStorage.setItem(sizeKey, String(s));
    } catch {}
    setSaved(s === baseSize ? null : s);
    navigate({ page_size: s === baseSize ? null : String(s), page: null });
  };
  useEffect(() => {
    if (q === paramQ) return;
    const timer = setTimeout(() => navigate({ q, page: null }), 300);
    return () => clearTimeout(timer);
    // URL is the source of committed state; typing stays immediate while the request is debounced.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, paramQ, pathname]);

  const facets = useMemo(() => {
    if (!facet) return [];
    const counts = new Map<string, number>(Object.entries(paging?.facets ?? {}));
    if (!paging) for (const m of meta) if (m.f) counts.set(m.f, (counts.get(m.f) ?? 0) + 1);
    const known = (facet.values ?? []).filter((v) => counts.has(v.value)).map((v) => ({ value: v.value, label: v.label ?? v.value, n: counts.get(v.value)! }));
    const rest = [...counts.entries()]
      .filter(([v]) => !known.some((k) => k.value === v))
      .sort((a, b) => b[1] - a[1])
      .map(([value, n]) => ({ value, label: value, n }));
    return [...known, ...rest];
  }, [facet, meta, paging]);

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

  const total = paging?.total ?? hits.length;
  const pages = Math.max(1, Math.ceil(total / size));
  useEffect(() => {
    if (!paging || paging.page <= pages) return;
    const next = new URLSearchParams(params.toString());
    next.set("page", String(pages));
    startTransition(() => router.replace(`${pathname}?${next}`, { scroll: false }));
  }, [paging?.page, pages, params, pathname, router]);
  const at = Math.min(paging ? paging.page - 1 : page, pages - 1);
  const shown = paging ? rows.map((_, i) => i) : hits.slice(at * size, at * size + size);
  const filtered = Boolean(needle || f);

  // A new page starts at its first row, even where the list scrolls inside its frame.
  useEffect(() => {
    top.current?.querySelectorAll<HTMLElement>(":scope > :not(.lf__bar, .lf__pager)").forEach((el) => { el.scrollTop = 0; });
  }, [at, size]);
  const go = (p: number) => {
    navigate({ page: String(Math.max(0, Math.min(pages - 1, p)) + 1) }, true);
    const el = top.current;
    if (el && el.getBoundingClientRect().top < 0) el.scrollIntoView({ block: "start", behavior: "smooth" });
  };
  const clear = () => {
    draftQuery.current = "";
    setQ("");
    navigate({ q: null, status: null, page: null });
  };

  const showBar = Boolean(paging) || rows.length > 8 || facets.length > 1 || filtered;
  const allCount = paging ? Object.values(paging.facets).reduce((a, b) => a + b, 0) || total : meta.length;
  const busy = pending || Boolean(paging && q !== paramQ);
  const range = total && shown.length ? `${(at * size + 1).toLocaleString("en")}–${(at * size + shown.length).toLocaleString("en")} of ${total.toLocaleString("en")}` : total ? `No items on this page · ${total.toLocaleString("en")} total` : "0 matches";

  const body = shown.map((i) => rows[i]);
  const empty = (
    <div className="lf__empty">
      <p>
        No {noun[1]} match{needle ? ` “${q.trim()}”` : ""}
        {f ? ` with ${facet?.label.toLowerCase() ?? "filter"} “${facets.find((x) => x.value === f)?.label ?? facet?.values?.find((x) => x.value === f)?.label ?? f}”` : ""}.
      </p>
      <button type="button" className="btn btn--neutral btn--sm" onClick={clear}>
        Clear filters
      </button>
    </div>
  );

  return (
    <div className={`lf${touched ? " lf--touched" : ""}${showBar ? " lf--bar" : ""}`} ref={top} aria-busy={busy}>
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
                  draftQuery.current = e.target.value;
                  setQ(e.target.value);
                }}
                placeholder={placeholder ?? `Search ${noun[1]}`}
                aria-label={`Search ${noun[1]}`}
              />
            </label>
          )}
          {(facets.length > 1 || f) && (
            <div className="seg lf__facets" role="group" aria-label={`Filter by ${facet?.label.toLowerCase()}`}>
              <button type="button" aria-pressed={!f} disabled={pending} onClick={() => navigate({ status: null, page: null })}>
                All <span className="lf__n">{allCount}</span>
              </button>
              {facets.map((x) => (
                <button key={x.value} type="button" aria-pressed={f === x.value} disabled={pending} onClick={() => navigate({ status: f === x.value ? null : x.value, page: null })}>
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
        shown.length ? (
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
      ) : shown.length ? (
        listTag === "div" ? (
          <div className={listClass}>{body}</div>
        ) : (
          <ol className={listClass}>{body}</ol>
        )
      ) : (
        empty
      )}

      {(pages > 1 || size !== baseSize || at > 0) && (
        <nav className="lf__pager" aria-label="Pages">
          <label className="lf__size">
            <span>Show</span>
            <select className="select" value={size} aria-label={`${noun[1]} per page`} disabled={pending} onChange={(e) => resize(Number(e.target.value))}>
              {sizeChoices(options, size, total).map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
            <span>per page</span>
          </label>
          {pages > 1 && (
            <span className="lf__pages">
              <button type="button" className="lf__page lf__step" onClick={() => go(at - 1)} disabled={at === 0 || busy} aria-label="Previous page" title="Previous page">
                <span className="lf__flip">
                  <Icon name="chevron" size={14} />
                </span>
              </button>
              {pageList(at, pages).map((p, i) =>
                p < 0 ? (
                  <span key={`gap${i}`} className="lf__gap" aria-hidden="true">
                    …
                  </span>
                ) : (
                  <button key={p} type="button" className="lf__page num" aria-current={p === at ? "page" : undefined} disabled={busy} onClick={() => go(p)}>
                    {p + 1}
                  </button>
                ),
              )}
              <button type="button" className="lf__page lf__step" onClick={() => go(at + 1)} disabled={at >= pages - 1 || busy} aria-label="Next page" title="Next page">
                <Icon name="chevron" size={14} />
              </button>
            </span>
          )}
        </nav>
      )}
    </div>
  );
}

/** Sizes up to the first that fits everything, which reads "All"; the current size always stays listed. */
function sizeChoices(options: number[], size: number, total: number): { value: number; label: string }[] {
  const fit = options.findIndex((s) => s >= total);
  const shown = fit < 0 ? options : options.slice(0, fit + 1);
  if (!shown.includes(size)) shown.push(size);
  const all = fit < 0 ? -1 : options[fit];
  return shown.sort((a, b) => a - b).map((s) => ({ value: s, label: s === all && fit > 0 ? `All ${total.toLocaleString("en")}` : String(s) }));
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
