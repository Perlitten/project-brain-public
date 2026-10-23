import { scopeMemberToProject } from "@/lib/member-scope";
import { restrictedMember } from "@/lib/team-session";
import type { CSSProperties, ReactNode } from "react";
import Link from "next/link";
import { Term } from "@/components/Term";
import { ClampText } from "@/components/ClampText";
import { ListFrame } from "@/components/ListFrame";
import { Chip, EmptyState, PageHead } from "@/components/ui";
import { moduleOf } from "@/lib/code-graph";
import { getPagedInsights } from "@/lib/data";
import { getListQuery } from "@/lib/list-query";
import type { Insight } from "@/lib/types";
import { SectionTabs } from "@/components/SectionNav";

export const metadata = { title: "Findings" };

const kindWords: Record<Insight["kind"], ReactNode> = {
  drift: <Term k="drift">Structure drift</Term>,
  coupling: <Term k="coupling">Tangled modules</Term>,
  hotspot: <Term k="hotspot">Hotspot</Term>,
  freshness: <><Term k="pack">Context pack</Term> for older code</>,
  other: "Observation",
};

export default async function Insights({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  await scopeMemberToProject("/insights", params);
  const [result, member] = await Promise.all([getPagedInsights(typeof params.repo === "string" ? params.repo : undefined, getListQuery(params)), restrictedMember()]);
  const insights = result.items;
  const hasQuery = Boolean(params.q || params.status || params.page || params.page_size);
  const repo = typeof params.repo === "string" ? params.repo : undefined;
  // A finding's module opens the code map focused on it.
  const mapLink = (name: string) => {
    const module = moduleOf(name);
    return module ? `/graph?${new URLSearchParams({ ...(repo ? { repo } : {}), module })}` : null;
  };

  // The same finding recurs dozens of times in a live Brain — one card per
  // distinct finding with a recurrence count reads better than a wall of
  // identical "Worth a look" cards. The card keeps the newest occurrence.
  const rank: Record<string, number> = { bad: 0, warn: 1, info: 2, ok: 3, idle: 4 };
  const groups: { rep: Insight; count: number }[] = [];
  const byKey = new Map<string, (typeof groups)[number]>();
  for (const f of insights) {
    const key = `${f.kind}\u0000${f.title}`;
    const g = byKey.get(key);
    if (!g) {
      const fresh = { rep: f, count: 1 };
      byKey.set(key, fresh);
      groups.push(fresh);
    } else {
      g.count++;
      if ((rank[f.tone] ?? 9) < (rank[g.rep.tone] ?? 9)) g.rep = { ...g.rep, tone: f.tone };
    }
  }
  return (
    <>
      <SectionTabs section="projects" active="/insights" />
      <PageHead
        title="Findings"
        lede={typeof params.repo === "string" ? "Places in this project’s code that are getting harder to change." : "Places in connected repositories that are getting harder to change, across every project."}
      />
      {result.available === false && (
        <EmptyState title="Findings unavailable" body="Brain didn’t return its findings just now. Reload in a moment; nothing here means the code is clean." />
      )}
      {result.available !== false && insights.length === 0 && !hasQuery && (
        <EmptyState title="No findings yet" body="Brain hasn’t flagged anything in this code. Findings appear after a repository is indexed — run `brain index` or connect a project in Setup." action={member ? undefined : <Link className="btn btn--neutral" href="/setup">Set up indexing</Link>} />
      )}
      {result.available !== false && (insights.length > 0 || hasQuery) && (
      <ListFrame
        listTag="div"
        listClass="cards"
        noun={["finding", "findings"]}
        meta={groups.map((g) => ({ q: `${g.rep.title} ${g.rep.evidence} ${g.rep.module} ${g.rep.kind}`.toLowerCase(), f: g.rep.tone }))}
        paging={result.paging}
        facet={{ label: "Severity", values: [{ value: "bad", label: "Rule broken" }, { value: "warn", label: "Worth a look" }, { value: "info", label: "FYI" }] }}
        rows={groups.map((g, i) => (
          <article key={g.rep.id} className={`card tone-${g.rep.tone}`} style={{ "--i": Math.min(i, 12) } as CSSProperties}>
            <div className="card__top">
              <Chip tone={g.rep.tone}>{g.rep.tone === "bad" ? "Rule broken" : g.rep.tone === "warn" ? "Worth a look" : "FYI"}</Chip>
              <span className="text-dim">{kindWords[g.rep.kind]}</span>
              {g.count > 1 && <Chip tone="idle">×{g.count}</Chip>}
            </div>
            <h2 className="card__title">{g.rep.title}</h2>
            <ClampText text={g.rep.evidence} className="card__body mono" lines={6} label="evidence" />
            <p className="card__meta">
              {mapLink(g.rep.module) ? (
                <Link className="mono link" href={mapLink(g.rep.module)!} title="Show this module on the code map">{g.rep.module}</Link>
              ) : (
                <span className="mono">{g.rep.module}</span>
              )}
              <span>found {g.rep.detectedAt}{g.count > 1 ? ` · seen ${g.count} times` : ""}</span>
            </p>
          </article>
        ))}
      />
      )}
    </>
  );
}
