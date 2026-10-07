import type { CSSProperties, ReactNode } from "react";
import { Term } from "@/components/Term";
import { ListFrame } from "@/components/ListFrame";
import { Chip, EmptyState, PageHead } from "@/components/ui";
import { getPagedInsights } from "@/lib/data";
import { getListQuery } from "@/lib/list-query";
import type { Insight } from "@/lib/types";

export const metadata = { title: "Findings" };

const kindWords: Record<Insight["kind"], ReactNode> = {
  drift: <Term k="drift">Structure drift</Term>,
  coupling: <Term k="coupling">Tangled modules</Term>,
  hotspot: <Term k="hotspot">Hotspot</Term>,
  freshness: "Outdated briefing",
  other: "Observation",
};

export default async function Insights({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  const result = await getPagedInsights(typeof params.repo === "string" ? params.repo : undefined, getListQuery(params));
  const insights = result.items;
  const hasQuery = Boolean(params.q || params.status || params.page || params.page_size);
  const serious = insights.filter((i) => i.tone === "bad").length;
  return (
    <>
      <PageHead
        eyebrow="Findings"
        title="Findings across the Brain service"
        lede="Brain watches all connected repositories and points out places that are getting harder to work with. These findings are global because the service does not attach ownership to them."
      />
      {insights.length === 0 && !hasQuery && (
        <EmptyState title="No findings yet" body="Brain hasn’t flagged anything in this code. Findings appear here as Brain notices risky changes." />
      )}
      {(insights.length > 0 || hasQuery) && (
      <ListFrame
        listTag="div"
        listClass="cards"
        noun={["finding", "findings"]}
        meta={insights.map((f) => ({ q: `${f.title} ${f.evidence} ${f.module} ${f.kind}`.toLowerCase(), f: f.tone }))}
        paging={result.paging}
        facet={{ label: "Severity", values: [{ value: "bad", label: "Rule broken" }, { value: "warn", label: "Worth a look" }, { value: "info", label: "FYI" }] }}
        rows={insights.map((f, i) => (
          <article key={f.id} className={`card tone-${f.tone}`} style={{ "--i": Math.min(i, 12) } as CSSProperties}>
            <div className="card__top">
              <Chip tone={f.tone}>{f.tone === "bad" ? "Rule broken" : f.tone === "warn" ? "Worth a look" : "FYI"}</Chip>
              <span className="text-dim">{kindWords[f.kind]}</span>
            </div>
            <h2 className="card__title">{f.title}</h2>
            <p className="card__body mono">{f.evidence}</p>
            <p className="card__meta">
              <span className="mono">{f.module}</span>
              <span>found {f.detectedAt}</span>
            </p>
          </article>
        ))}
      />
      )}
    </>
  );
}
