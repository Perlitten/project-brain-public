export const LIST_SIZES = [20, 50, 100] as const;

export type ListQuery = {
  page: number;
  size: number;
  q: string;
  status: string;
};

export type ListPaging = ListQuery & {
  total: number;
  facets: Record<string, number>;
};

export function getListQuery(params: Record<string, string | string[] | undefined> | Partial<ListQuery> | URLSearchParams): ListQuery {
  const read = (key: string) => params instanceof URLSearchParams ? params.get(key) ?? "" : Array.isArray((params as Record<string, unknown>)[key]) ? String(((params as Record<string, unknown>)[key] as unknown[])[0] ?? "") : String((params as Record<string, unknown>)[key] ?? "");
  const rawPage = Number(read("page"));
  const rawSize = Number(read("page_size") || read("size"));
  return {
    page: Number.isSafeInteger(rawPage) && rawPage > 0 ? rawPage : 1,
    size: (LIST_SIZES as readonly number[]).includes(rawSize) ? rawSize : 20,
    q: read("q").trim().slice(0, 500),
    status: read("status").trim(),
  };
}

export function listQueryParams(query: Partial<ListQuery>): string {
  const p = new URLSearchParams();
  if (query.q) p.set("q", query.q);
  if (query.status) p.set("status", query.status);
  if (query.page && query.page > 1) p.set("page", String(query.page));
  if (query.size && query.size !== 20) p.set("page_size", String(query.size));
  return p.toString();
}

export function pagingFrom(value: unknown, fallback: ListQuery): ListPaging {
  const o = value && typeof value === "object" ? value as Record<string, unknown> : {};
  const n = (v: unknown, d: number) => typeof v === "number" && Number.isFinite(v) ? v : d;
  const raw = o.facets && typeof o.facets === "object" ? o.facets as Record<string, unknown> : {};
  const nested = raw.severity ?? raw.freshness ?? raw.status;
  const source = nested && typeof nested === "object" ? nested as Record<string, unknown> : raw;
  const facets = Object.fromEntries(Object.entries(source).filter(([, v]) => typeof v === "number").map(([k, v]) => [k, n(v, 0)]));
  return { ...fallback, total: n(o.total, 0), page: n(o.page, fallback.page), size: n(o.page_size, fallback.size), facets };
}
